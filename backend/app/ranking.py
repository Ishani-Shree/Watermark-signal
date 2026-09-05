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

# How far off its peak a price must sit before the move counts as reverted.
# This is not just presentation: an event's headline describes the moment it
# fired ("breaking 52-week high"), which becomes false once the price falls
# back. A reverted move must be reported as a path, whatever the gap length,
# or the digest states something that is no longer true.
REVERTED_OFF_PEAK_PCT = 0.02


def build_digest(conn, user_id: int, now: datetime) -> dict:
    # Watermark and watchlist in ONE round trip. Measured: the database work
    # for a digest is ~0.1ms, while a single round trip to a hosted Postgres
    # costs ~400ms -- so the count of queries, not their cost, is what makes
    # this endpoint slow. The LEFT JOINs keep a row even when the user has
    # no watchlist, so the watermark still comes back.
    #
    # Read the existing watermark BEFORE advancing it -- this is the value
    # everything below diffs against. Advancing first would make every
    # visit look like a first visit.
    rows = conn.execute(
        text(
            """
            SELECT r.last_viewed_at AS watermark,
                   w.symbol, w.muted_kinds, w.target_price, w.note
            FROM (SELECT CAST(:uid AS bigint) AS uid) u
            LEFT JOIN read_state r ON r.user_id = u.uid
            LEFT JOIN watchlist_items w ON w.user_id = u.uid
            """
        ),
        {"uid": user_id},
    ).mappings().all()

    watermark_ts = rows[0]["watermark"] if rows else None
    watchlist_rows = [r for r in rows if r["symbol"] is not None]

    if not watchlist_rows:
        _advance_watermark(conn, user_id, now)
        return {
            "mode": "empty_watchlist",
            "gap_minutes": None,
            "events": [],
            "suppressed_count": 0,
            "watched_count": 0,
            "flagged_count": 0,
        }

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

    # Second and final round trip: the events, each symbol's current price,
    # and the watermark advance -- together. The advance rides along as a
    # data-modifying CTE because it must happen on every read anyway, and a
    # write that needs no result should not cost its own trip.
    #
    # Current price is joined per event rather than fetched for the whole
    # watchlist: only symbols WITH events ever need it, and a 40-symbol
    # watchlist with one event was previously fetching 39 prices to discard.
    event_rows = conn.execute(
        text(
            """
            WITH advance AS (
                INSERT INTO read_state (user_id, last_viewed_at)
                VALUES (:uid, :now)
                ON CONFLICT (user_id) DO UPDATE
                    SET last_viewed_at = GREATEST(read_state.last_viewed_at, EXCLUDED.last_viewed_at)
            )
            SELECT e.symbol, e.kind, e.score, e.reason_text,
                   e.first_seen_ts, e.last_updated_ts, e.peak_price, e.trough_price,
                   latest.price AS current_price
            FROM events e
            LEFT JOIN LATERAL (
                SELECT price FROM snapshots
                WHERE snapshots.symbol = e.symbol
                ORDER BY source_ts DESC LIMIT 1
            ) latest ON true
            WHERE e.symbol = ANY(:symbols) AND e.last_updated_ts >= :lookback_start
            ORDER BY e.score DESC
            """
        ),
        {
            "uid": user_id,
            "now": now,
            "symbols": symbols,
            "lookback_start": lookback_start,
        },
    ).mappings().all()

    def is_muted(row):
        muted = watchlist_by_symbol[row["symbol"]]["muted_kinds"] or []
        return row["kind"] in muted

    events = [e for e in event_rows if not is_muted(e)]

    # Current price came back joined to each event above. The revert story
    # needs where the price is NOW, not just where the event peaked --
    # "peaked 1639.60, now back at 1562" is the whole point
    # (BUILD_PLAN.md section 6).
    latest_prices = {
        e["symbol"]: e["current_price"] for e in events if e["current_price"] is not None
    }

    mode = "long_gap" if is_long_gap else "short_gap"

    # Decide per symbol, not globally: a move that round-tripped needs the
    # path story even over a short gap, because its headline no longer
    # describes where the price actually is.
    by_symbol: dict[str, list] = {}
    for e in events:
        by_symbol.setdefault(e["symbol"], []).append(e)

    candidates = []
    for symbol, symbol_events in by_symbol.items():
        if is_long_gap or _has_reverted(symbol_events, latest_prices.get(symbol)):
            summary = _aggregate_by_symbol(symbol_events, latest_prices)
            if is_long_gap:
                summary = [s for s in summary if s["score"] >= LONG_GAP_RAISE_THRESHOLD]
            candidates.extend(summary)
        else:
            candidates.extend(
                {
                    "symbol": e["symbol"],
                    "kind": e["kind"],
                    "score": float(e["score"]),
                    "reason_text": e["reason_text"],
                    "first_seen_ts": e["first_seen_ts"].isoformat(),
                    "last_updated_ts": e["last_updated_ts"].isoformat(),
                    "reverted": False,
                }
                for e in symbol_events
            )

    candidates.sort(key=lambda c: c["score"], reverse=True)
    surfaced = candidates[:DIGEST_CAP]
    suppressed_count = max(0, len(candidates) - DIGEST_CAP)

    return {
        "mode": mode,
        "gap_minutes": gap_minutes,
        "events": surfaced,
        "suppressed_count": suppressed_count,
        # Restraint is only visible against the size of what was checked:
        # "1 of 40" says something that a bare event count cannot.
        "watched_count": len(watchlist_rows),
        "flagged_count": len({c["symbol"] for c in candidates}),
    }


def _has_reverted(symbol_events, current_price) -> bool:
    """True when the price has fallen back meaningfully from the peak the
    event recorded -- i.e. the move happened and came back."""
    if current_price is None:
        return False
    peaks = [float(e["peak_price"]) for e in symbol_events if e["peak_price"] is not None]
    if not peaks:
        return False
    peak = max(peaks)
    if peak <= 0:
        return False
    return (float(current_price) - peak) / peak <= -REVERTED_OFF_PEAK_PCT


def _aggregate_by_symbol(events, latest_prices: dict[str, float]) -> list[dict]:
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

        # Postgres NUMERIC arrives as Decimal -- normalise to float on the
        # way in so nothing downstream mixes the two types. Compare with
        # `is None`, not truthiness: a legitimate 0.0 must not read as absent.
        if e["peak_price"] is not None:
            peak = float(e["peak_price"])
            agg["peak_price"] = peak if agg["peak_price"] is None else max(agg["peak_price"], peak)
        if e["trough_price"] is not None:
            trough = float(e["trough_price"])
            agg["trough_price"] = (
                trough if agg["trough_price"] is None else min(agg["trough_price"], trough)
            )

        agg["first_seen_ts"] = min(agg["first_seen_ts"], e["first_seen_ts"])
        agg["last_updated_ts"] = max(agg["last_updated_ts"], e["last_updated_ts"])

    results = []
    for agg in by_symbol.values():
        peak = agg["peak_price"]
        trough = agg["trough_price"]
        current = latest_prices.get(agg["symbol"])
        current = float(current) if current is not None else None

        parts = []
        reverted = False
        if peak is not None:
            parts.append(f"peaked {peak:.2f}")
        if current is not None:
            parts.append(f"now {current:.2f}")
        # Distance from the peak is what makes a revert visible: the price
        # can be unchanged since you left and still have travelled.
        if peak is not None and peak > 0 and current is not None:
            off_peak = (current - peak) / peak
            if off_peak <= -0.005:
                parts.append(f"{off_peak:+.1%} off peak")
            reverted = off_peak <= -REVERTED_OFF_PEAK_PCT
        count = agg["event_count"]
        parts.append(f"{count} event{'' if count == 1 else 's'} while away")
        parts.append(f"last active {agg['last_updated_ts'].strftime('%H:%M')}")

        results.append(
            {
                "symbol": agg["symbol"],
                "kind": "path_summary",
                "score": agg["score"],
                "reason_text": f"{agg['symbol']}  " + " | ".join(parts),
                "first_seen_ts": agg["first_seen_ts"].isoformat(),
                "last_updated_ts": agg["last_updated_ts"].isoformat(),
                "peak_price": peak,
                "trough_price": trough,
                "current_price": current,
                "event_count": count,
                "reverted": reverted,
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
