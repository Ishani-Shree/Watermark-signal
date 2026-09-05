import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .auth import create_access_token, get_current_user_id, hash_password, verify_password
from .config import settings
from .db import engine
from .detection import compute_score, upsert_event
from .provider_health import ProviderUnavailable, health
from .ratelimit import rate_limit_auth
from .providers import get_provider
from .providers import replay_provider as replay_clock
from .providers.yfinance_provider import YFinanceProvider
from .ranking import acknowledge, build_digest

app = FastAPI(title="Watermark API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # Cloudflare Pages gives every preview build its own subdomain, so those
    # cannot be enumerated ahead of time. The regex is anchored on both ends
    # to the exact project -- an unanchored pattern would also match a
    # hostile `watermark-signal.pages.dev.attacker.com`.
    allow_origin_regex=r"^https://[a-z0-9-]+\.watermark-signal\.pages\.dev$",
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    # Auth travels as a Bearer header, never a cookie, so the browser never
    # needs to attach credentials cross-origin.
    allow_credentials=False,
)

# Symbols that are themselves market/sector indices -- they get snapshots
# and baselines like anything else, but no event scoring of their own
# (nothing to compare them against).
INDEX_SYMBOLS = {"^NSEI"}


MUTABLE_KINDS = {"z_move", "vol_spike", "relative_move", "level_breach", "target_hit"}

# bcrypt hashes at most 72 BYTES and raises beyond that -- so an over-long
# password is a 422 from validation, never a 500 from the hashing library.
MAX_PASSWORD_BYTES = 72
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=MAX_PASSWORD_BYTES)

    @field_validator("password")
    @classmethod
    def fits_bcrypt(cls, value: str) -> str:
        # Length in characters is not length in bytes once non-ASCII is in
        # play; bcrypt counts bytes.
        if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
        return value


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class WatchlistAddRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=280)
    target_price: float | None = Field(default=None, gt=0, lt=1e9)


class DigestAckRequest(BaseModel):
    cursor: datetime | None = None


class WatchlistUpdateRequest(BaseModel):
    note: str | None = Field(default=None, max_length=280)
    target_price: float | None = Field(default=None, gt=0, lt=1e9)
    muted_kinds: list[str] | None = Field(default=None, max_length=len(MUTABLE_KINDS))

    @field_validator("muted_kinds")
    @classmethod
    def known_kinds_only(cls, value: list[str] | None) -> list[str] | None:
        """Without this, `muted_kinds` is an arbitrary string array the
        client can write anything into -- unbounded junk in the database
        that silently mutes nothing."""
        if value is None:
            return None
        unknown = set(value) - MUTABLE_KINDS
        if unknown:
            raise ValueError(f"unknown signal kinds: {sorted(unknown)}")
        return sorted(set(value))


@app.get("/")
def root():
    """This is the API, not the app -- say so. Anyone who pastes the
    backend URL into a browser (a judge, most likely) should land on a
    signpost rather than a bare 404."""
    return {
        "service": "Watermark API",
        "what": "An attention filter for a market watchlist: what actually "
                "changed since you last looked, and why it mattered.",
        "app": "https://watermark-signal.pages.dev",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/diagnostics/live-provider")
def diagnostics_live_provider(
    symbol: str = "RELIANCE.NS", user_id: int = Depends(get_current_user_id)
):
    """Attempt a REAL upstream fetch from this host, whatever provider is
    configured.

    The decision to run on the replay feed rests on a claim -- that yfinance
    is rate-limited from a datacenter IP -- which deserves evidence rather
    than assumption. This answers it from the machine that would actually be
    doing the fetching. Deliberately bypasses GuardedProvider so the circuit
    breaker and chaos switch cannot colour the result.
    """
    started = time.perf_counter()
    try:
        quote = YFinanceProvider().get_latest(symbol)
    except Exception as exc:  # noqa: BLE001 - the failure mode IS the answer
        return {
            "reachable": False,
            "symbol": symbol,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if quote is None:
        return {
            "reachable": False,
            "symbol": symbol,
            "error": "provider returned no quote",
            "elapsed_ms": elapsed_ms,
        }

    return {
        "reachable": True,
        "symbol": quote.symbol,
        "price": quote.price,
        "volume": quote.volume,
        "prev_close": quote.prev_close,
        "source_ts": quote.source_ts.isoformat(),
        # False means the market timestamp was unavailable, so this quote
        # could not be deduped reliably -- see DECISIONS.md.
        "has_market_ts": quote.has_market_ts,
        "elapsed_ms": elapsed_ms,
    }


@app.get("/health")
def health_check():
    """Reports degradation rather than hiding it. `degraded` true means we
    are knowingly serving aged data -- the UI says so out loud."""
    provider_state = health.snapshot()
    return {
        "status": "ok",
        "env": settings.env,
        "provider": settings.provider,
        "degraded": provider_state["state"] != "closed" or provider_state["chaos_enabled"],
        "provider_health": provider_state,
    }


@app.post("/demo/chaos")
def demo_chaos(enabled: bool, user_id: int = Depends(get_current_user_id)):
    """Flip the upstream provider into a simulated outage. Exists so the
    degraded path can be demonstrated deliberately (BUILD_PLAN.md section
    13, step 4) instead of hoping Yahoo misbehaves during the demo."""
    _require_replay_provider()
    health.chaos_enabled = enabled
    if not enabled:
        # Turning chaos off also clears the breaker, so recovery is
        # immediate on stage rather than after a 60s cooldown.
        health.record_success()
    return {"chaos_enabled": health.chaos_enabled, "provider_health": health.snapshot()}


def run_ingest_cycle(conn) -> dict:
    """One full ingest + detection pass. Extracted so the cron endpoint and
    the demo scenario runner drive the exact same code path -- the demo
    fast-forwards the feed clock, it does not inject events."""
    provider = get_provider()
    confidence = "replay" if settings.provider == "replay" else "live"

    rows = conn.execute(
        text(
            """
            SELECT s.symbol, s.market_index_symbol, s.sector_index_symbol,
                   b.ret_stddev_30d, b.avg_volume_20d, b.wk52_high, b.wk52_low
            FROM symbols s
            LEFT JOIN baselines b ON b.symbol = s.symbol
            """
        )
    ).mappings().all()
    symbol_info = {row["symbol"]: dict(row) for row in rows}

    events_touched = 0
    quotes_by_symbol = {}
    quotes_out = []
    rows_to_write = []

    failed_symbols = []
    breaker_tripped = False

    for symbol in symbol_info:
        # One sick symbol must not abort the whole cycle. An open breaker,
        # on the other hand, should stop the run immediately rather than
        # retrying a provider we already know is down 48 more times.
        try:
            quote = provider.get_latest(symbol)
        except ProviderUnavailable:
            breaker_tripped = True
            break
        except Exception:  # noqa: BLE001 - upstream is unofficial
            failed_symbols.append(symbol)
            continue

        if quote is None:
            failed_symbols.append(symbol)
            continue

        # A live quote missing price/volume/prev_close is routine with an
        # unofficial upstream. Reject it HERE, per symbol -- letting a None
        # through means the float()/int() coercion below raises mid-batch and
        # aborts the insert for every other symbol too, defeating the
        # isolation this loop exists to provide.
        if quote.price is None or quote.volume is None or quote.prev_close is None:
            failed_symbols.append(symbol)
            continue

        quotes_by_symbol[symbol] = quote
        rows_to_write.append(
            {
                "symbol": quote.symbol,
                "source_ts": quote.source_ts,
                "fetched_at": quote.fetched_at,
                "price": quote.price,
                "volume": quote.volume,
                "prev_close": quote.prev_close,
                "source": quote.source,
                # A quote whose provider gave no market timestamp can't be
                # deduped reliably; label it rather than pretend otherwise.
                "confidence": confidence if quote.has_market_ts else "unverified_ts",
            }
        )
        quotes_out.append(
            {"symbol": quote.symbol, "price": quote.price, "source_ts": quote.source_ts.isoformat()}
        )

    # One batched round trip rather than one per symbol. The DB is remote,
    # so round trips -- not the inserts themselves -- are the cost that
    # grows with watchlist size.
    #
    # On conflict we refresh `fetched_at` only. The price row is immutable
    # (that is the idempotency guarantee); re-seeing the same quote is not a
    # new observation of a new price, but it IS evidence the pipeline is
    # alive, and that belongs in fetched_at.
    # Written as ONE statement over unnested arrays rather than executemany,
    # because RETURNING is not available on executemany -- and the
    # per-row insert/update flag is what makes the dedup guarantee
    # observable instead of merely asserted.
    ingested = 0
    refreshed = 0
    if rows_to_write:
        result = conn.execute(
            text(
                """
                INSERT INTO snapshots
                    (symbol, source_ts, fetched_at, price, volume, prev_close, source, confidence)
                SELECT * FROM unnest(
                    CAST(:symbols AS text[]),
                    CAST(:source_tss AS timestamptz[]),
                    CAST(:fetched_ats AS timestamptz[]),
                    CAST(:prices AS double precision[]),
                    CAST(:volumes AS bigint[]),
                    CAST(:prev_closes AS double precision[]),
                    CAST(:sources AS text[]),
                    CAST(:confidences AS text[])
                )
                ON CONFLICT (symbol, source_ts) DO UPDATE
                    SET fetched_at = EXCLUDED.fetched_at
                RETURNING (xmax = 0) AS inserted
                """
            ),
            {
                "symbols": [r["symbol"] for r in rows_to_write],
                "source_tss": [r["source_ts"] for r in rows_to_write],
                "fetched_ats": [r["fetched_at"] for r in rows_to_write],
                "prices": [float(r["price"]) for r in rows_to_write],
                "volumes": [int(r["volume"]) for r in rows_to_write],
                "prev_closes": [float(r["prev_close"]) for r in rows_to_write],
                "sources": [r["source"] for r in rows_to_write],
                "confidences": [r["confidence"] for r in rows_to_write],
            },
        )
        flags = [row[0] for row in result.fetchall()]
        ingested = sum(1 for f in flags if f)
        refreshed = len(flags) - ingested

    for symbol, quote in quotes_by_symbol.items():
        if symbol in INDEX_SYMBOLS:
            continue

        info = symbol_info[symbol]
        index_symbol = info["sector_index_symbol"] or info["market_index_symbol"]
        index_quote = quotes_by_symbol.get(index_symbol)

        index_pct_change = None
        index_label = None
        if index_quote and index_quote.prev_close:
            index_pct_change = (index_quote.price - index_quote.prev_close) / index_quote.prev_close
            # Honest labeling: only call it "sector" if it actually is one,
            # otherwise it's the NIFTY fallback -- see BUILD_PLAN.md section 5.
            index_label = "sector" if info["sector_index_symbol"] else "NIFTY"

        score = compute_score(
            symbol=symbol,
            price=quote.price,
            prev_close=quote.prev_close,
            volume=quote.volume,
            baseline=info,
            index_pct_change=index_pct_change,
            index_label=index_label,
        )
        cluster_key = upsert_event(conn, quote.source_ts, quote.price, score)
        if cluster_key:
            events_touched += 1

    return {
        "checked": len(symbol_info),
        "ingested": ingested,
        # Non-zero means dedup did its job: the same quote arrived again and
        # was not double-counted.
        "deduped": refreshed,
        "events_touched": events_touched,
        "failed": len(failed_symbols),
        "breaker_tripped": breaker_tripped,
        "provider_health": health.snapshot(),
        "quotes": quotes_out,
    }


@app.get("/ingest")
def ingest():
    """Cron target (hit by the Cloudflare Worker every 10 min).

    Idempotent on (symbol, source_ts) -- a redelivered fetch can't double
    count. Append-only: "latest" is derived by querying MAX(source_ts),
    never by insert order, so an out-of-order/delayed arrival can't corrupt
    what downstream code treats as current. See BUILD_PLAN.md section 9.

    After writing snapshots, runs the detection layer once per symbol
    (BUILD_PLAN.md section 3): compute a composite score against that
    symbol's baseline and the index, then open/extend an event if it's
    significant enough.
    """
    with engine.begin() as conn:
        return run_ingest_cycle(conn)


def _require_replay_provider():
    """Demo controls only exist for the scripted feed -- you cannot
    fast-forward a live market. This is the gate, not a config flag."""
    if settings.provider != "replay":
        raise HTTPException(
            status_code=403, detail="Demo controls are only available on the replay feed"
        )


@app.post("/demo/run-scenario")
def demo_run_scenario(user_id: int = Depends(get_current_user_id)):
    """Step the replay clock through the whole scripted timeline, running a
    real ingest+detection pass at each point. Nothing is injected: the
    scoring layer sees the same quotes it would have seen live and reaches
    its own conclusions -- this only removes the wait. Makes the 5-minute
    demo deterministic (BUILD_PLAN.md section 13)."""
    _require_replay_provider()

    minutes = replay_clock.scenario_minutes()
    span = float(max(minutes)) if minutes else 0.0
    # Land the scenario in the recent past, ending now -- so it reads as
    # "what happened while you were away", not as future-dated quotes.
    anchor_end = datetime.now(timezone.utc)

    steps = []
    try:
        for minute in minutes:
            replay_clock.pin_minute(minute, anchor_end=anchor_end, span=span)
            with engine.begin() as conn:
                result = run_ingest_cycle(conn)
            steps.append(
                {
                    "minute": minute,
                    "ingested": result["ingested"],
                    "events_touched": result["events_touched"],
                }
            )
    finally:
        # Always hand the clock back to real time, even if a step raised.
        replay_clock.pin_minute(None)

    # The scenario represents time that passed while you were away, so put
    # the read watermark back to before it started -- otherwise the events
    # land behind a watermark that was advanced when the page loaded, and
    # the demo can never show its own scenario.
    #
    # This is the one place that deliberately moves a watermark BACKWARD.
    # The normal path is monotonic (see ranking._advance_watermark); this
    # is a demo-only rewind, gated to the replay feed.
    scenario_start = anchor_end - timedelta(minutes=span)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO read_state (user_id, last_viewed_at)
                VALUES (:uid, :ts)
                ON CONFLICT (user_id) DO UPDATE SET last_viewed_at = EXCLUDED.last_viewed_at
                """
            ),
            {"uid": user_id, "ts": scenario_start},
        )

    return {"steps": steps, "rewound_watermark_to": scenario_start.isoformat()}


@app.post("/demo/reset")
def demo_reset(user_id: int = Depends(get_current_user_id)):
    """Clear all detected events and snapshots so a scenario can be re-run
    from a clean slate. Leaves users, watchlists and baselines intact.

    Requires a logged-in caller. Being gated to the replay feed is NOT a
    security control -- the deployed instance runs on the replay feed, so
    without authentication anyone holding the public URL could wipe the
    data this endpoint deletes.
    """
    _require_replay_provider()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM events"))
        conn.execute(text("DELETE FROM snapshots"))
    return {"ok": True}


@app.get("/snapshots/{symbol}/latest")
def latest_snapshot(symbol: str):
    """Read path for a single symbol's most recent known price -- derived by
    MAX(source_ts), independent of insert order (see /ingest docstring)."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT symbol, source_ts, price, volume, prev_close, source, confidence
                FROM snapshots
                WHERE symbol = :symbol
                ORDER BY source_ts DESC
                LIMIT 1
                """
            ),
            {"symbol": symbol},
        ).mappings().first()

    if row is None:
        return {"error": "no data for symbol"}
    return dict(row)


@app.get("/events")
def list_events():
    """Debug/verification endpoint for the detection layer, ahead of the
    real user-scoped ranking layer landing in hours 15-19."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT symbol, kind, score, reason_text, first_seen_ts, last_updated_ts,
                       cluster_key, peak_price, trough_price
                FROM events
                ORDER BY last_updated_ts DESC
                LIMIT 50
                """
            )
        ).mappings().all()
    return {"events": [dict(r) for r in rows]}


@app.get("/symbols")
def list_symbols():
    """Populates the 'add to watchlist' picker on the frontend."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT symbol, name, exchange FROM symbols WHERE symbol != '^NSEI' ORDER BY name")
        ).mappings().all()
    return {"symbols": [dict(r) for r in rows]}


@app.post("/auth/signup", dependencies=[Depends(rate_limit_auth)])
def signup(body: SignupRequest):
    with engine.begin() as conn:
        try:
            row = conn.execute(
                text(
                    "INSERT INTO users (email, password_hash) VALUES (:email, :hash) RETURNING id"
                ),
                {"email": body.email, "hash": hash_password(body.password)},
            ).mappings().first()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="Email already registered")
    return {"access_token": create_access_token(row["id"]), "token_type": "bearer"}


@app.post("/auth/login", dependencies=[Depends(rate_limit_auth)])
def login(body: LoginRequest):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, password_hash FROM users WHERE email = :email"),
            {"email": body.email},
        ).mappings().first()

    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"access_token": create_access_token(row["id"]), "token_type": "bearer"}


@app.get("/watchlist")
def get_watchlist(user_id: int = Depends(get_current_user_id)):
    """Joins each watchlist item against its latest snapshot -- current
    price, staleness (source/confidence), and observation time -- so the
    frontend never has to make N extra calls to show live state."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT w.symbol, s.name, w.note, w.target_price, w.muted_kinds, w.added_at,
                       latest.price, latest.source, latest.confidence,
                       latest.source_ts, latest.fetched_at
                FROM watchlist_items w
                JOIN symbols s ON s.symbol = w.symbol
                LEFT JOIN LATERAL (
                    SELECT price, source, confidence, source_ts, fetched_at
                    FROM snapshots
                    WHERE snapshots.symbol = w.symbol
                    ORDER BY source_ts DESC
                    LIMIT 1
                ) latest ON true
                WHERE w.user_id = :uid
                ORDER BY w.added_at
                """
            ),
            {"uid": user_id},
        ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@app.post("/watchlist")
def add_to_watchlist(body: WatchlistAddRequest, user_id: int = Depends(get_current_user_id)):
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM symbols WHERE symbol = :symbol"), {"symbol": body.symbol}
        ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Unknown symbol")

        # Idempotent add: UNIQUE (user_id, symbol) -- adding twice is a no-op,
        # not an error.
        conn.execute(
            text(
                """
                INSERT INTO watchlist_items (user_id, symbol, note, target_price)
                VALUES (:uid, :symbol, :note, :target_price)
                ON CONFLICT (user_id, symbol) DO UPDATE
                SET note = COALESCE(EXCLUDED.note, watchlist_items.note),
                    target_price = COALESCE(EXCLUDED.target_price, watchlist_items.target_price)
                """
            ),
            {
                "uid": user_id,
                "symbol": body.symbol,
                "note": body.note,
                "target_price": body.target_price,
            },
        )
    return {"ok": True}


@app.patch("/watchlist/{symbol}")
def update_watchlist_item(
    symbol: str, body: WatchlistUpdateRequest, user_id: int = Depends(get_current_user_id)
):
    fields = {}
    if body.note is not None:
        fields["note"] = body.note
    if body.target_price is not None:
        fields["target_price"] = body.target_price
    if body.muted_kinds is not None:
        fields["muted_kinds"] = body.muted_kinds

    if not fields:
        # Nothing to change is still a claim about a row that must exist.
        # Returning ok here would report success for a symbol the caller
        # does not have -- and for one nobody has.
        with engine.connect() as conn:
            exists = conn.execute(
                text(
                    "SELECT 1 FROM watchlist_items WHERE user_id = :uid AND symbol = :symbol"
                ),
                {"uid": user_id, "symbol": symbol},
            ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Not in watchlist")
        return {"ok": True}

    # Column names are interpolated into SQL, so they must never come from
    # user input. They are literals above, but assert it: a later edit that
    # builds `fields` from a request body would otherwise turn this into an
    # injection point silently.
    assert set(fields) <= {"note", "target_price", "muted_kinds"}
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                f"UPDATE watchlist_items SET {set_clause} WHERE user_id = :uid AND symbol = :symbol"
            ),
            {**fields, "uid": user_id, "symbol": symbol},
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not in watchlist")
    return {"ok": True}


@app.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str, user_id: int = Depends(get_current_user_id)):
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM watchlist_items WHERE user_id = :uid AND symbol = :symbol"),
            {"uid": user_id, "symbol": symbol},
        )
    # Deleting nothing is not the same as deleting something. Reporting ok
    # for a symbol the caller never watched hides real client bugs -- the UI
    # would show the removal "working" while the row it meant to remove is
    # still there under a different user.
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not in watchlist")
    return {"ok": True}


@app.get("/digest")
def digest(user_id: int = Depends(get_current_user_id)):
    """User-facing read path (BUILD_PLAN.md sections 3 and 7): joins the
    detection layer's events against this user's watchlist, applies mute
    settings, and applies time-scaled materiality -- individual events for
    a short gap since last visit, an aggregated peak/trough/event-count
    path summary for a long one.

    Side-effect free. Marking the digest read is a separate POST to
    /digest/ack -- a GET that advanced the watermark consumed itself on any
    double fetch (a StrictMode double-invoke, a retry, a second tab), and
    the second, empty response was the one that reached the screen.
    """
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        return build_digest(conn, user_id, now)


@app.post("/digest/ack")
def digest_ack(body: DigestAckRequest, user_id: int = Depends(get_current_user_id)):
    """Mark the digest read, up to the instant the client actually saw.

    Advancing to the client's cursor rather than to `now` means a signal
    that arrived between rendering and acknowledging is not skipped over.
    """
    now = datetime.now(timezone.utc)
    cursor = body.cursor or now
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)
    with engine.begin() as conn:
        acknowledge(conn, user_id, cursor, now)
    return {"ok": True}
