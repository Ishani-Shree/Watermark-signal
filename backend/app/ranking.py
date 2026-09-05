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
# A brand-new user has no watermark to diff against, so look back a bounded
# window rather than the symbol's whole history. 72 hours, not 24, because
# markets close: on a Sunday the last session was Friday, and a 24-hour
# window shows a new user an empty digest while real Friday moves sit just
# outside it. Three days covers a weekend and most single-day holidays.
FIRST_VISIT_LOOKBACK_HOURS = 72
DIGEST_CAP = 3

# A target price is the one signal that cannot live in the detection layer.
# Detection is symbol-scoped and computed once for everyone; a target is
# different for every user watching the same stock, so crossing one is
# evaluated here, at read time, against that user's own number.
TARGET_HIT_SCORE = 80.0

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

    # Clock skew: a watermark ahead of now would make lookback_start a future
    # instant, so `last_updated_ts >= lookback_start` matches nothing and the
    # digest reports "still quiet" forever. And because the advance is
    # GREATEST(...), the bad value can never be written back down -- the user
    # would be stuck until wall-clock caught up. Treat it as no watermark at
    # all. (BUILD_PLAN.md section 10 names this edge case.)
    if watermark_ts is not None and watermark_ts > now:
        watermark_ts = None

    if not watchlist_rows:
        return {
            "mode": "empty_watchlist",
            "gap_minutes": None,
            "events": [],
            "suppressed_count": 0,
            "muted_count": 0,
            "watched_count": 0,
            "flagged_count": 0,
            "cursor": now.isoformat(),
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

    # Second and final round trip: the events plus each symbol's current
    # price. Reading is now side-effect free -- see the module docstring on
    # why the watermark advance moved to an explicit ack.
    #
    # Current price is joined per event rather than fetched for the whole
    # watchlist: only symbols WITH events ever need it, and a 40-symbol
    # watchlist with one event was previously fetching 39 prices to discard.
    event_rows = conn.execute(
        text(
            """
            SELECT e.symbol, e.kind, e.score, e.reason_text,
                   e.first_seen_ts, e.last_updated_ts, e.peak_price, e.trough_price,
                   e.direction, latest.price AS current_price
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
            "symbols": symbols,
            "lookback_start": lookback_start,
        },
    ).mappings().all()

    def is_muted(row):
        muted = watchlist_by_symbol[row["symbol"]]["muted_kinds"] or []
        return row["kind"] in muted

    events = [e for e in event_rows if not is_muted(e)]
    # Counted, not just dropped. Reporting a silenced stock as one that
    # "stayed quiet" is a lie, and honesty about what was filtered is the
    # entire pitch of this product.
    muted_symbols = {e["symbol"] for e in event_rows if is_muted(e)}

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

    # Every symbol that actually produced a signal, counted BEFORE the
    # long-gap threshold trims the list. Counting after would report a stock
    # that genuinely fired -- just below the raised bar -- as one that
    # "stayed quiet", which is the same lie as the muted case.
    fired_symbols = set(by_symbol)
    below_bar = 0

    candidates = []
    for symbol, symbol_events in by_symbol.items():
        if is_long_gap or _has_reverted(symbol_events, latest_prices.get(symbol)):
            summary = _aggregate_by_symbol(symbol_events, latest_prices)
            if is_long_gap:
                kept = [s for s in summary if s["score"] >= LONG_GAP_RAISE_THRESHOLD]
                below_bar += len(summary) - len(kept)
                summary = kept
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

    crossings = _target_crossings(conn, watchlist_rows, lookback_start)
    candidates.extend(crossings)
    fired_symbols.update(c["symbol"] for c in crossings)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    surfaced = candidates[:DIGEST_CAP]
    suppressed_count = max(0, len(candidates) - DIGEST_CAP) + below_bar

    return {
        "mode": mode,
        "gap_minutes": gap_minutes,
        "events": surfaced,
        "suppressed_count": suppressed_count,
        # Silenced by the user, not silent on its own. Kept distinct from
        # suppressed_count so the UI never calls a muted stock "quiet".
        "muted_count": len(muted_symbols),
        # Restraint is only visible against the size of what was checked:
        # "1 of 40" says something that a bare event count cannot.
        "watched_count": len(watchlist_rows),
        "flagged_count": len(fired_symbols),
        # The instant this digest reflects. The client acks this value, so a
        # signal arriving mid-render is not skipped over.
        "cursor": now.isoformat(),
    }


def classify_crossing(
    target: float, then_px: float, now_px: float, high: float, low: float
) -> tuple[str, float, bool] | None:
    """Did the price cross `target` during the window?

    Judged against the window's extremes, never its endpoints -- a stock
    that shot past the target and came back has still hit it, and comparing
    start to end is precisely the blindness this product exists to remove.

    Returns (direction, extreme reached, whether it has since pulled back),
    or None if no crossing happened.
    """
    if then_px < target <= high:
        return "rose through", high, now_px < target
    if then_px > target >= low:
        return "fell through", low, now_px > target
    return None


def _target_crossings(conn, watchlist_rows, lookback_start) -> list[dict]:
    """Surface a target price the stock has crossed since the last visit.

    Costs a round trip only for users who actually set a target -- the
    feature is opt-in, so people who never use it never pay for it.

    A *crossing* is required, not merely "price is past the target". A stock
    that has sat above your target for a week is not news every time you
    open the app; it was news the day it got there.

    Crucially, the crossing is detected against the price EXTREMES over the
    window, not its endpoints. Comparing where the price started to where it
    ended would miss a stock that shot through your target and came back --
    which is the exact failure this whole product exists to fix. It would be
    absurd to reintroduce it here.
    """
    targets = {
        row["symbol"]: float(row["target_price"])
        for row in watchlist_rows
        if row["target_price"] is not None
        # Mute applies here too -- an attention filter you cannot turn down
        # eventually becomes noise like everything else.
        and "target_hit" not in (row["muted_kinds"] or [])
    }
    if not targets:
        return []

    rows = conn.execute(
        text(
            """
            SELECT s.symbol,
                   now_px.price  AS price_now,
                   then_px.price AS price_then,
                   window_px.high_price,
                   window_px.low_price
            FROM unnest(CAST(:symbols AS text[])) AS s(symbol)
            LEFT JOIN LATERAL (
                SELECT price FROM snapshots
                WHERE snapshots.symbol = s.symbol
                ORDER BY source_ts DESC LIMIT 1
            ) now_px ON true
            LEFT JOIN LATERAL (
                SELECT price FROM snapshots
                WHERE snapshots.symbol = s.symbol AND source_ts <= :since
                ORDER BY source_ts DESC LIMIT 1
            ) then_px ON true
            LEFT JOIN LATERAL (
                SELECT max(price) AS high_price, min(price) AS low_price
                FROM snapshots
                WHERE snapshots.symbol = s.symbol AND source_ts >= :since
            ) window_px ON true
            """
        ),
        {"symbols": list(targets), "since": lookback_start},
    ).mappings().all()

    crossings = []
    for row in rows:
        target = targets[row["symbol"]]
        if row["price_now"] is None or row["price_then"] is None:
            # No before-price to compare against (brand-new user, or a
            # symbol with no history yet). Claiming a crossing here would be
            # inventing an event that may have happened long ago.
            continue

        now_px = float(row["price_now"])
        then_px = float(row["price_then"])
        high = float(row["high_price"]) if row["high_price"] is not None else now_px
        low = float(row["low_price"]) if row["low_price"] is not None else now_px

        crossing = classify_crossing(target, then_px, now_px, high, low)
        if crossing is None:
            continue
        direction, extreme, pulled_back = crossing
        detail = f"reached {extreme:.2f}" if pulled_back else f"was {then_px:.2f}"

        crossings.append(
            {
                "symbol": row["symbol"],
                "kind": "target_hit",
                "score": TARGET_HIT_SCORE,
                "reason_text": (
                    f"{row['symbol']}  {direction} your target {target:.2f} | "
                    f"{detail} | now {now_px:.2f}"
                    + (" | since pulled back" if pulled_back else "")
                ),
                "first_seen_ts": None,
                "last_updated_ts": None,
                "current_price": now_px,
                "target_price": target,
                "reverted": False,
            }
        )
    return crossings


def _has_reverted(symbol_events, current_price) -> bool:
    """True when the price has come back meaningfully from the extreme the
    event reached -- i.e. the move happened and undid itself.

    Direction matters, and peak/trough alone cannot supply it: a rise still
    running sits at its peak and far above its trough, and so does a fall
    that has fully recovered. Without the recorded direction, checking both
    ends would flag every healthy rally as "reverted". So an upward move is
    judged against its peak, a downward one against its trough.
    """
    if current_price is None:
        return False
    current = float(current_price)

    # Any constituent event opening downward makes this a down move.
    went_down = any(e.get("direction") == "down" for e in symbol_events)

    if went_down:
        troughs = [
            float(e["trough_price"]) for e in symbol_events if e["trough_price"] is not None
        ]
        if not troughs:
            return False
        trough = min(troughs)
        if trough <= 0:
            return False
        return (current - trough) / trough >= REVERTED_OFF_PEAK_PCT

    peaks = [float(e["peak_price"]) for e in symbol_events if e["peak_price"] is not None]
    if not peaks:
        return False
    peak = max(peaks)
    if peak <= 0:
        return False
    return (current - peak) / peak <= -REVERTED_OFF_PEAK_PCT


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


def acknowledge(conn, user_id: int, cursor: datetime, now: datetime) -> None:
    """Mark the digest as read, up to the instant it described.

    Split out from build_digest because reading must be side-effect free: a
    GET that advances the watermark consumes itself on any double fetch --
    React StrictMode's double-invoke, a retry, a second tab -- and the
    second, empty response is the one that lands on screen.

    Advancing to the digest's own cursor rather than to `now` means a signal
    that arrived between rendering and acknowledging is not silently skipped.
    """
    _advance_watermark(conn, user_id, min(cursor, now))


def _advance_watermark(conn, user_id: int, now: datetime) -> None:
    """Monotonic: two devices reading concurrently only ever move this
    forward, never backward."""
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
