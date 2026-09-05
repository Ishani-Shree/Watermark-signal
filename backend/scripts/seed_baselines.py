"""
Fetch historical price data locally (never from the deployed backend --
see BUILD_PLAN.md section 8) and seed symbols + baselines into Neon.

Also emits app/providers/replay_baseline.json: a resting price and volume
per symbol, derived from that symbol's REAL history. The replay feed reads
it so every non-scripted symbol sits quietly inside its own 52-week range
instead of at some invented number. Hand-writing those would not scale past
a handful of symbols, and getting them wrong silently flags every stock as
breaching its range on every tick.

Run from backend/ with the venv active:
    python scripts/seed_baselines.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import engine  # noqa: E402

MARKET_INDEX = "^NSEI"  # NIFTY 50 -- market-wide fallback per BUILD_PLAN.md section 5

REPLAY_BASELINE_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "providers" / "replay_baseline.json"
)

# NIFTY 50 constituents plus the index itself. Any ticker that fails to
# resolve is skipped with a warning rather than half-inserted -- index
# composition changes, and a symbol row without a baseline is worse than
# no symbol row at all.
SYMBOLS = [
    (MARKET_INDEX, "NIFTY 50"),
    ("ADANIENT.NS", "Adani Enterprises"),
    ("ADANIPORTS.NS", "Adani Ports & SEZ"),
    ("APOLLOHOSP.NS", "Apollo Hospitals"),
    ("ASIANPAINT.NS", "Asian Paints"),
    ("AXISBANK.NS", "Axis Bank"),
    ("BAJAJ-AUTO.NS", "Bajaj Auto"),
    ("BAJFINANCE.NS", "Bajaj Finance"),
    ("BAJAJFINSV.NS", "Bajaj Finserv"),
    ("BEL.NS", "Bharat Electronics"),
    ("BHARTIARTL.NS", "Bharti Airtel"),
    ("CIPLA.NS", "Cipla"),
    ("COALINDIA.NS", "Coal India"),
    ("DRREDDY.NS", "Dr. Reddy's Laboratories"),
    ("EICHERMOT.NS", "Eicher Motors"),
    ("GRASIM.NS", "Grasim Industries"),
    ("HCLTECH.NS", "HCL Technologies"),
    ("HDFCBANK.NS", "HDFC Bank"),
    ("HDFCLIFE.NS", "HDFC Life Insurance"),
    ("HEROMOTOCO.NS", "Hero MotoCorp"),
    ("HINDALCO.NS", "Hindalco Industries"),
    ("HINDUNILVR.NS", "Hindustan Unilever"),
    ("ICICIBANK.NS", "ICICI Bank"),
    ("INDUSINDBK.NS", "IndusInd Bank"),
    ("INFY.NS", "Infosys"),
    ("ITC.NS", "ITC"),
    ("JSWSTEEL.NS", "JSW Steel"),
    ("KOTAKBANK.NS", "Kotak Mahindra Bank"),
    ("LT.NS", "Larsen & Toubro"),
    ("M&M.NS", "Mahindra & Mahindra"),
    ("MARUTI.NS", "Maruti Suzuki"),
    ("NESTLEIND.NS", "Nestle India"),
    ("NTPC.NS", "NTPC"),
    ("ONGC.NS", "Oil & Natural Gas Corp"),
    ("POWERGRID.NS", "Power Grid Corp"),
    ("RELIANCE.NS", "Reliance Industries"),
    ("SBILIFE.NS", "SBI Life Insurance"),
    ("SBIN.NS", "State Bank of India"),
    ("SHRIRAMFIN.NS", "Shriram Finance"),
    ("SUNPHARMA.NS", "Sun Pharmaceutical"),
    ("TATACONSUM.NS", "Tata Consumer Products"),
    ("TATASTEEL.NS", "Tata Steel"),
    ("TCS.NS", "Tata Consultancy Services"),
    ("TECHM.NS", "Tech Mahindra"),
    ("TITAN.NS", "Titan Company"),
    ("TRENT.NS", "Trent"),
    ("ULTRACEMCO.NS", "UltraTech Cement"),
    ("WIPRO.NS", "Wipro"),
]

FETCH_DELAY_SECONDS = 0.35  # be gentle; yfinance is unofficial scraping


def compute_baseline(hist):
    closes = hist["Close"]
    volumes = hist["Volume"]
    returns = closes.pct_change(fill_method=None).dropna()

    # The most recent row is often today's unclosed bar, whose Close is NaN.
    # Take the last row that actually has a close -- `.iloc[-1]` straight off
    # `closes` silently yields NaN, which then poisons every price derived
    # from it.
    settled = closes.dropna()

    return {
        "ret_stddev_30d": float(returns.tail(30).std()) if len(returns) >= 2 else None,
        "avg_volume_20d": float(volumes.tail(20).mean()) if len(volumes) >= 1 else None,
        "wk52_high": float(closes.tail(252).max()) if len(closes) >= 1 else None,
        "wk52_low": float(closes.tail(252).min()) if len(closes) >= 1 else None,
        "history_days": len(settled),
        "last_close": float(settled.iloc[-1]),
    }


def fetch_all():
    """Fetch first, write second. A symbol only reaches the database once we
    actually have a baseline for it."""
    fetched, failed = [], []

    for symbol, name in SYMBOLS:
        try:
            hist = yf.Ticker(symbol).history(period="1y")
        except Exception as exc:  # noqa: BLE001 - provider is unofficial, anything can surface
            failed.append((symbol, str(exc)[:60]))
            continue

        if hist.empty or hist["Close"].dropna().empty:
            failed.append((symbol, "no usable close prices"))
            continue

        baseline = compute_baseline(hist)
        if baseline["ret_stddev_30d"] is None or baseline["avg_volume_20d"] is None:
            failed.append((symbol, "insufficient history for a baseline"))
            continue

        fetched.append((symbol, name, baseline))
        time.sleep(FETCH_DELAY_SECONDS)

    return fetched, failed


def write_db(fetched):
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO symbols (symbol, name, exchange, market_index_symbol, sector_index_symbol)
                VALUES (:symbol, :name, 'NSE', :market_index, NULL)
                ON CONFLICT (symbol) DO UPDATE SET name = EXCLUDED.name
                """
            ),
            [
                {"symbol": s, "name": n, "market_index": MARKET_INDEX}
                for s, n, _ in fetched
            ],
        )

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
            [
                {
                    "symbol": s,
                    "ret_stddev_30d": b["ret_stddev_30d"],
                    "avg_volume_20d": b["avg_volume_20d"],
                    "wk52_high": b["wk52_high"],
                    "wk52_low": b["wk52_low"],
                    "history_days": b["history_days"],
                    "computed_at": now,
                }
                for s, _, b in fetched
            ],
        )


def write_replay_baseline(fetched):
    """Resting price/volume per symbol for the replay feed, taken from real
    history: the last close, and the 20-day average volume. A symbol resting
    at its own last close has a 0% change and a 1.0x volume ratio, so it
    scores near zero and stays out of the digest -- which is what a quiet
    stock should do."""
    payload = {
        symbol: {
            "price": round(b["last_close"], 2),
            "volume": int(b["avg_volume_20d"] or 0),
        }
        for symbol, _, b in fetched
    }
    # allow_nan=False turns a NaN price into a loud failure here rather than
    # a file that json.dumps happily writes, strict parsers reject, and
    # Python quietly reads back as a real float.
    REPLAY_BASELINE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8"
    )
    return len(payload)


def seed():
    print(f"fetching {len(SYMBOLS)} symbols...")
    fetched, failed = fetch_all()

    write_db(fetched)
    written = write_replay_baseline(fetched)

    for symbol, _, b in fetched:
        print(
            f"  {symbol:16s} {b['history_days']:3d}d  last={b['last_close']:9,.2f}  "
            f"stddev={b['ret_stddev_30d']:.5f}  vol20={b['avg_volume_20d']:>12,.0f}  "
            f"52w=[{b['wk52_low']:,.2f}, {b['wk52_high']:,.2f}]"
        )

    print(f"\nseeded {len(fetched)} symbols; replay baseline written for {written}")
    if failed:
        print(f"skipped {len(failed)} (not inserted, so no symbol lacks a baseline):")
        for symbol, reason in failed:
            print(f"  {symbol:16s} {reason}")


if __name__ == "__main__":
    seed()
