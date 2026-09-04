from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .auth import create_access_token, get_current_user_id, hash_password, verify_password
from .config import settings
from .db import engine
from .detection import compute_score, upsert_event
from .providers import get_provider
from .ranking import build_digest

app = FastAPI(title="Watermark API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before submission
    allow_methods=["*"],
    allow_headers=["*"],
)

# Symbols that are themselves market/sector indices -- they get snapshots
# and baselines like anything else, but no event scoring of their own
# (nothing to compare them against).
INDEX_SYMBOLS = {"^NSEI"}


class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class WatchlistAddRequest(BaseModel):
    symbol: str
    note: str | None = None
    target_price: float | None = None


class WatchlistUpdateRequest(BaseModel):
    note: str | None = None
    target_price: float | None = None
    muted_kinds: list[str] | None = None


@app.get("/health")
def health():
    return {"status": "ok", "env": settings.env, "provider": settings.provider}


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
    provider = get_provider()
    confidence = "replay" if settings.provider == "replay" else "live"

    with engine.begin() as conn:
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

        ingested = 0
        events_touched = 0
        quotes_by_symbol = {}
        quotes_out = []

        for symbol in symbol_info:
            quote = provider.get_latest(symbol)
            if quote is None:
                continue
            quotes_by_symbol[symbol] = quote

            result = conn.execute(
                text(
                    """
                    INSERT INTO snapshots
                        (symbol, source_ts, price, volume, prev_close, source, confidence)
                    VALUES
                        (:symbol, :source_ts, :price, :volume, :prev_close, :source, :confidence)
                    ON CONFLICT (symbol, source_ts) DO NOTHING
                    """
                ),
                {
                    "symbol": quote.symbol,
                    "source_ts": quote.source_ts,
                    "price": quote.price,
                    "volume": quote.volume,
                    "prev_close": quote.prev_close,
                    "source": quote.source,
                    "confidence": confidence,
                },
            )
            if result.rowcount:
                ingested += 1
            quotes_out.append(
                {"symbol": quote.symbol, "price": quote.price, "source_ts": quote.source_ts.isoformat()}
            )

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
        "events_touched": events_touched,
        "quotes": quotes_out,
    }


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


@app.post("/auth/signup")
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


@app.post("/auth/login")
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
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT w.symbol, s.name, w.note, w.target_price, w.muted_kinds, w.added_at
                FROM watchlist_items w
                JOIN symbols s ON s.symbol = w.symbol
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
        return {"ok": True}

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
        conn.execute(
            text("DELETE FROM watchlist_items WHERE user_id = :uid AND symbol = :symbol"),
            {"uid": user_id, "symbol": symbol},
        )
    return {"ok": True}


@app.get("/digest")
def digest(user_id: int = Depends(get_current_user_id)):
    """User-facing read path (BUILD_PLAN.md sections 3 and 7): joins the
    detection layer's events against this user's watchlist, applies mute
    settings, and applies time-scaled materiality -- individual events for
    a short gap since last visit, an aggregated peak/trough/event-count
    path summary for a long one. Also advances this user's read watermark."""
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        return build_digest(conn, user_id, now)
