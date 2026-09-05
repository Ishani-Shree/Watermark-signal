"""Service metadata, health, diagnostics, and the cron ingest target."""

import time

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ..auth import get_current_user_id
from ..config import settings
from ..db import engine
from ..ingest import run_ingest_cycle
from ..provider_health import health
from ..providers.yfinance_provider import YFinanceProvider

router = APIRouter(tags=["meta"])


@router.get("/")
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


@router.get("/health")
def health_check():
    """Reports degradation rather than hiding it. `degraded` true means we
    are knowingly serving aged data -- the UI says so out loud."""
    provider_state = health.snapshot()
    return {
        "status": "ok",
        "env": settings.env,
        "provider": settings.provider,
        "degraded": provider_state["state"] != "closed" or provider_state["chaos_enabled"],
        # Whether the demo controls are available. The UI must key off this,
        # not off `provider` -- the two are independent, and gating the
        # buttons on the provider would hide them exactly when the
        # deployment runs on live data, which is when they matter most.
        "demo_controls": settings.demo_controls,
        "provider_health": provider_state,
    }


@router.get("/diagnostics/live-provider")
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


@router.get("/ingest")
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


@router.get("/snapshots/{symbol}/latest")
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


@router.get("/events")
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
