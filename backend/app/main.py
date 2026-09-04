from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .config import settings
from .db import engine
from .detection import compute_score, upsert_event
from .providers import get_provider

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


@app.get("/digest")
def digest():
    """User-facing read path. Placeholder until ranking layer lands (hours 15-19)."""
    return {"events": [], "suppressed_count": 0}
