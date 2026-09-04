"""
Fetch historical price data locally (never from the deployed backend --
see BUILD_PLAN.md section 8) and seed symbols + baselines into Neon.

Run from backend/ with the venv active:
    python scripts/seed_baselines.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import engine  # noqa: E402

MARKET_INDEX = "^NSEI"  # NIFTY 50 -- market-wide fallback per BUILD_PLAN.md section 5

SYMBOLS = [
    ("RELIANCE.NS", "Reliance Industries"),
    ("TCS.NS", "Tata Consultancy Services"),
    ("INFY.NS", "Infosys"),
    ("HDFCBANK.NS", "HDFC Bank"),
    ("ICICIBANK.NS", "ICICI Bank"),
    ("AXISBANK.NS", "Axis Bank"),
    ("SBIN.NS", "State Bank of India"),
    ("ITC.NS", "ITC"),
    ("LT.NS", "Larsen & Toubro"),
    ("WIPRO.NS", "Wipro"),
]


def compute_baseline(hist):
    closes = hist["Close"]
    volumes = hist["Volume"]
    returns = closes.pct_change(fill_method=None).dropna()

    ret_stddev_30d = float(returns.tail(30).std()) if len(returns) >= 2 else None
    avg_volume_20d = float(volumes.tail(20).mean()) if len(volumes) >= 1 else None
    wk52_high = float(closes.tail(252).max()) if len(closes) >= 1 else None
    wk52_low = float(closes.tail(252).min()) if len(closes) >= 1 else None
    history_days = len(closes)

    return ret_stddev_30d, avg_volume_20d, wk52_high, wk52_low, history_days


def seed():
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO symbols (symbol, name, exchange, market_index_symbol, sector_index_symbol)
                VALUES (:symbol, :name, 'NSE', :market_index, NULL)
                ON CONFLICT (symbol) DO UPDATE SET name = EXCLUDED.name
                """
            ),
            [{"symbol": s, "name": n, "market_index": MARKET_INDEX} for s, n in SYMBOLS],
        )
        print(f"upserted {len(SYMBOLS)} symbols")

    for symbol, name in SYMBOLS:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1y")
        if hist.empty:
            print(f"WARNING: no history for {symbol}, skipping")
            continue

        ret_stddev_30d, avg_volume_20d, wk52_high, wk52_low, history_days = compute_baseline(hist)

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO baselines
                        (symbol, ret_stddev_30d, avg_volume_20d, wk52_high, wk52_low, history_days, computed_at)
                    VALUES
                        (:symbol, :ret_stddev_30d, :avg_volume_20d, :wk52_high, :wk52_low, :history_days, :computed_at)
                    ON CONFLICT (symbol) DO UPDATE SET
                        ret_stddev_30d = EXCLUDED.ret_stddev_30d,
                        avg_volume_20d = EXCLUDED.avg_volume_20d,
                        wk52_high = EXCLUDED.wk52_high,
                        wk52_low = EXCLUDED.wk52_low,
                        history_days = EXCLUDED.history_days,
                        computed_at = EXCLUDED.computed_at
                    """
                ),
                {
                    "symbol": symbol,
                    "ret_stddev_30d": ret_stddev_30d,
                    "avg_volume_20d": avg_volume_20d,
                    "wk52_high": wk52_high,
                    "wk52_low": wk52_low,
                    "history_days": history_days,
                    "computed_at": datetime.now(timezone.utc),
                },
            )
        print(f"{symbol}: {history_days} days, stddev={ret_stddev_30d:.5f}, "
              f"avg_vol_20d={avg_volume_20d:,.0f}, 52w=[{wk52_low:.2f}, {wk52_high:.2f}]")


if __name__ == "__main__":
    seed()
