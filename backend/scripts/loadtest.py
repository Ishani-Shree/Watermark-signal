"""
Load test for the read path.

Seeds synthetic users and watchlist items, measures digest latency at that
scale, and tears everything down. The point is not to prove the app is fast
-- it is to find the query that stops being fast first, while there is
still time to fix it.

    python scripts/loadtest.py seed      # create synthetic load
    python scripts/loadtest.py measure   # time the digest, show query plans
    python scripts/loadtest.py clean     # remove every trace

Everything it creates is prefixed `loadtest_` so cleanup is exact.
"""

import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import engine  # noqa: E402
from app.ranking import build_digest  # noqa: E402

USERS = 500
SYMBOLS_PER_USER = 20
EMAIL_PREFIX = "loadtest_"
EVENTS_PER_SYMBOL = 5


def seed():
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        symbols = conn.execute(
            text("SELECT symbol FROM symbols WHERE symbol <> '^NSEI' ORDER BY symbol")
        ).scalars().all()
        if not symbols:
            print("no symbols seeded; run seed_baselines.py first")
            return

        conn.execute(
            text(
                """
                INSERT INTO users (email, password_hash)
                SELECT :prefix || g, 'x'
                FROM generate_series(1, :n) AS g
                ON CONFLICT (email) DO NOTHING
                """
            ),
            {"prefix": EMAIL_PREFIX, "n": USERS},
        )

        user_ids = conn.execute(
            text("SELECT id FROM users WHERE email LIKE :p"), {"p": EMAIL_PREFIX + "%"}
        ).scalars().all()

        rows = []
        for i, uid in enumerate(user_ids):
            # Overlapping but not identical watchlists, as real users would
            # have -- a uniform slice would flatter the cache.
            for j in range(SYMBOLS_PER_USER):
                rows.append({"uid": uid, "symbol": symbols[(i + j) % len(symbols)]})

        conn.execute(
            text(
                """
                INSERT INTO watchlist_items (user_id, symbol)
                VALUES (:uid, :symbol)
                ON CONFLICT (user_id, symbol) DO NOTHING
                """
            ),
            rows,
        )

        conn.execute(
            text(
                """
                INSERT INTO read_state (user_id, last_viewed_at)
                SELECT id, :ts FROM users WHERE email LIKE :p
                ON CONFLICT (user_id) DO UPDATE SET last_viewed_at = EXCLUDED.last_viewed_at
                """
            ),
            {"ts": now - timedelta(hours=6), "p": EMAIL_PREFIX + "%"},
        )

        # Give the digest real work: several events per symbol in the window.
        event_rows = []
        for symbol in symbols:
            for k in range(EVENTS_PER_SYMBOL):
                ts = now - timedelta(minutes=30 * (k + 1))
                event_rows.append(
                    {
                        "symbol": symbol,
                        "kind": "vol_spike",
                        "score": 55 + k,
                        "reason": f"{symbol}  loadtest_ synthetic event",
                        "ts": ts,
                        "cluster": f"loadtest_{symbol}_{k}",
                    }
                )
        conn.execute(
            text(
                """
                INSERT INTO events
                    (symbol, kind, score, reason_text, first_seen_ts, last_updated_ts,
                     cluster_key, peak_price, trough_price)
                VALUES (:symbol, :kind, :score, :reason, :ts, :ts, :cluster, 100, 90)
                """
            ),
            event_rows,
        )

    print(f"seeded {len(user_ids)} users x {SYMBOLS_PER_USER} symbols "
          f"= {len(rows)} watchlist items, {len(event_rows)} events")


def measure(samples: int = 25):
    with engine.connect() as conn:
        uids = conn.execute(
            text("SELECT id FROM users WHERE email LIKE :p ORDER BY id LIMIT :n"),
            {"p": EMAIL_PREFIX + "%", "n": samples},
        ).scalars().all()

        counts = {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            for t in ("users", "watchlist_items", "events", "snapshots")
        }

    print("table sizes:", ", ".join(f"{k}={v:,}" for k, v in counts.items()))

    timings = []
    for uid in uids:
        now = datetime.now(timezone.utc)
        start = time.perf_counter()
        with engine.begin() as conn:
            build_digest(conn, uid, now)
        timings.append((time.perf_counter() - start) * 1000)

    timings.sort()
    print(f"\ndigest latency over {len(timings)} calls (includes network RTT to Neon):")
    print(f"  min    {timings[0]:7.1f} ms")
    print(f"  median {statistics.median(timings):7.1f} ms")
    print(f"  p95    {timings[int(len(timings) * 0.95) - 1]:7.1f} ms")
    print(f"  max    {timings[-1]:7.1f} ms")

    _explain()


def _explain():
    """The digest's hot query. What matters is whether it uses an index or
    scans the table -- the timing above is dominated by network latency and
    would hide a bad plan."""
    with engine.connect() as conn:
        symbols = conn.execute(
            text("SELECT symbol FROM symbols WHERE symbol <> '^NSEI' LIMIT 20")
        ).scalars().all()
        plan = conn.execute(
            text(
                """
                EXPLAIN (ANALYZE, BUFFERS)
                SELECT symbol, kind, score, reason_text, first_seen_ts, last_updated_ts,
                       peak_price, trough_price
                FROM events
                WHERE symbol = ANY(:symbols) AND last_updated_ts >= :since
                ORDER BY score DESC
                """
            ),
            {"symbols": symbols, "since": datetime.now(timezone.utc) - timedelta(hours=6)},
        ).scalars().all()

    print("\nquery plan for the digest's event lookup:")
    for line in plan:
        print("  " + line)


def clean():
    with engine.begin() as conn:
        uids = conn.execute(
            text("SELECT id FROM users WHERE email LIKE :p"), {"p": EMAIL_PREFIX + "%"}
        ).scalars().all()
        if uids:
            conn.execute(
                text("DELETE FROM watchlist_items WHERE user_id = ANY(:u)"), {"u": uids}
            )
            conn.execute(text("DELETE FROM read_state WHERE user_id = ANY(:u)"), {"u": uids})
            conn.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": uids})
        deleted = conn.execute(
            text("DELETE FROM events WHERE cluster_key LIKE 'loadtest_%'")
        ).rowcount
    print(f"removed {len(uids)} users and {deleted} synthetic events")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "measure"
    {"seed": seed, "measure": measure, "clean": clean}[command]()
