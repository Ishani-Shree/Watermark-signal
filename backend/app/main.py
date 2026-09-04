from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .config import settings
from .db import engine
from .providers import get_provider

app = FastAPI(title="Watermark API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before submission
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    """
    provider = get_provider()
    confidence = "replay" if settings.provider == "replay" else "live"

    with engine.begin() as conn:
        symbols = conn.execute(text("SELECT symbol FROM symbols")).scalars().all()

        ingested = 0
        quotes = []
        for symbol in symbols:
            quote = provider.get_latest(symbol)
            if quote is None:
                continue

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
            quotes.append(
                {"symbol": quote.symbol, "price": quote.price, "source_ts": quote.source_ts.isoformat()}
            )

    return {"checked": len(symbols), "ingested": ingested, "quotes": quotes}


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


@app.get("/digest")
def digest():
    """User-facing read path. Placeholder until ranking layer lands (hours 15-19)."""
    return {"events": [], "suppressed_count": 0}
