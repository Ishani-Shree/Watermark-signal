"""
Ranking layer (user-scoped, computed at read time -- see BUILD_PLAN.md
section 3). Takes the symbol-scoped events the detection layer already
computed and turns them into one user's personalized, capped digest:
applies their mute settings, and applies time-scaled materiality (section
7) -- a short gap since last visit means the current delta is the story,
a long gap means the endpoint is nearly meaningless and the path (peak,
trough, event count) is the story instead.
"""

from datetime import datetime, timedelta

from sqlalchemy import text

SHORT_GAP_MINUTES = 120  # under this, show individual events at the normal bar
LONG_GAP_RAISE_THRESHOLD = 65  # above SHORT_GAP_MINUTES, raise the bar so a
# week of ordinary drift doesn't generate a wall of flags
FIRST_VISIT_LOOKBACK_HOURS = 24  # a brand-new user has no watermark to diff
# against -- look back a bounded window rather than the symbol's entire history
DIGEST_CAP = 3


def build_digest(conn, user_id: int, now: datetime) -> dict:
    # Read the existing watermark BEFORE advancing it -- this is the value
    # everything below diffs against. Advancing first would make every
    # visit look like a first visit.
    read_state = conn.execute(
        text("SELECT last_viewed_at FROM read_state WHERE user_id = :uid"),
        {"uid": user_id},
    ).mappings().first()
    watermark_ts = read_state["last_viewed_at"] if read_state else None

    watchlist_rows = conn.execute(
        text(
            "SELECT symbol, muted_kinds, target_price, note FROM watchlist_items WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).mappings().all()

    _advance_watermark(conn, user_id, now)

    if not watchlist_rows:
        return {"mode": "empty_watchlist", "gap_minutes": None, "events": [], "suppressed_count": 0}

    watchlist_by_symbol = {row["symbol"]: row for row in watchlist_rows}
    symbols = list(watchlist_by_symbol.keys())
    is_first_visit = watermark_ts is None

    if is_first_visit:
        gap_minutes = None
        lookback_start = now - timedelta(hours=FIRST_VISIT_LOOKBACK_HOURS)
        is_long_gap = True
    else:
        gap_minutes = (now - watermark_ts).total_seconds() / 60
        lookback_start = watermark_ts
        is_long_gap = gap_minutes > SHORT_GAP_MINUTES

    events = conn.execute(
        text(
            """
            SELECT symbol, kind, score, reason_text, first_seen_ts, last_updated_ts,
                   peak_price, trough_price
            FROM events
            WHERE symbol = ANY(:symbols) AND last_updated_ts >= :lookback_start
            ORDER BY score DESC
            """
        ),
        {"symbols": symbols, "lookback_start": lookback_start},
    ).mappings().all()

    def is_muted(row):
        muted = watchlist_by_symbol[row["symbol"]]["muted_kinds"] or []
        return row["kind"] in muted

    events = [e for e in events if not is_muted(e)]

    if is_long_gap:
        candidates = _aggregate_by_symbol(events)
        candidates = [c for c in candidates if c["score"] >= LONG_GAP_RAISE_THRESHOLD]
        mode = "long_gap"
    else:
        candidates = [
            {
                "symbol": e["symbol"],
                "kind": e["kind"],
                "score": float(e["score"]),
                "reason_text": e["reason_text"],
                "first_seen_ts": e["first_seen_ts"].isoformat(),
                "last_updated_ts": e["last_updated_ts"].isoformat(),
            }
            for e in events
        ]
        mode = "short_gap"

    candidates.sort(key=lambda c: c["score"], reverse=True)
    surfaced = candidates[:DIGEST_CAP]
    suppressed_count = max(0, len(candidates) - DIGEST_CAP)

    return {
        "mode": mode,
        "gap_minutes": gap_minutes,
        "events": surfaced,
        "suppressed_count": suppressed_count,
    }


def _aggregate_by_symbol(events) -> list[dict]:
    by_symbol: dict[str, dict] = {}
    for e in events:
        agg = by_symbol.setdefault(
            e["symbol"],
            {
                "symbol": e["symbol"],
                "score": 0.0,
                "event_count": 0,
                "peak_price": None,
                "trough_price": None,
                "first_seen_ts": e["first_seen_ts"],
                "last_updated_ts": e["last_updated_ts"],
            },
        )
        agg["score"] = max(agg["score"], float(e["score"]))
        agg["event_count"] += 1
        if e["peak_price"] is not None:
            agg["peak_price"] = max(agg["peak_price"] or e["peak_price"], float(e["peak_price"]))
        if e["trough_price"] is not None:
            agg["trough_price"] = min(agg["trough_price"] or e["trough_price"], float(e["trough_price"]))
        agg["first_seen_ts"] = min(agg["first_seen_ts"], e["first_seen_ts"])
        agg["last_updated_ts"] = max(agg["last_updated_ts"], e["last_updated_ts"])

    results = []
    for agg in by_symbol.values():
        peak = agg["peak_price"]
        trough = agg["trough_price"]
        range_pct = (peak - trough) / trough if (peak and trough) else None
        reason = f"{agg['symbol']}  {agg['event_count']} event(s)"
        if peak is not None and trough is not None:
            reason += f" | peak {peak:.2f} / trough {trough:.2f}"
            if range_pct is not None:
                reason += f" ({range_pct:+.1%} range)"
        reason += f" | last active {agg['last_updated_ts'].strftime('%H:%M')}"

        results.append(
            {
                "symbol": agg["symbol"],
                "kind": "path_summary",
                "score": agg["score"],
                "reason_text": reason,
                "first_seen_ts": agg["first_seen_ts"].isoformat(),
                "last_updated_ts": agg["last_updated_ts"].isoformat(),
                "peak_price": peak,
                "trough_price": trough,
                "event_count": agg["event_count"],
            }
        )
    return results


def _advance_watermark(conn, user_id: int, now: datetime) -> None:
    """Monotonic: two devices reading concurrently only ever move this
    forward, never backward. Must be called AFTER build_digest has already
    read the prior watermark value -- see the ordering note there."""
    conn.execute(
        text(
            """
            INSERT INTO read_state (user_id, last_viewed_at)
            VALUES (:uid, :now)
            ON CONFLICT (user_id) DO UPDATE
            SET last_viewed_at = GREATEST(read_state.last_viewed_at, EXCLUDED.last_viewed_at)
            """
        ),
        {"uid": user_id, "now": now},
    )
