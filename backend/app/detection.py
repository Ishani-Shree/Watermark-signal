"""
Detection layer (symbol-scoped, computed once per ingest cycle -- see
BUILD_PLAN.md section 3). Turns a raw quote + baseline into a composite
significance score, and turns a sustained score into an event with
hysteresis and clustering so a stock oscillating at the boundary doesn't
flap, and one continuous move reads as a single evolving event rather
than forty separate flags.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

# A score must reach OPEN_THRESHOLD to open a new event, but only needs to
# stay above CLOSE_THRESHOLD to keep an already-open event extending. The
# gap between them is the hysteresis margin.
OPEN_THRESHOLD = 50.0
CLOSE_THRESHOLD = 35.0

# If the last update to a symbol's most recent event is older than this,
# treat a new significant reading as a fresh event rather than an
# extension of the old one.
EXTEND_WINDOW_MINUTES = 30

# Caps beyond which a component contributes its full weight -- an extreme
# reading in one dimension doesn't require extreme readings in all of them.
Z_CAP = 3.0
VOL_RATIO_CAP = 3.0
REL_MOVE_CAP = 0.03  # 3 percentage points of index-relative divergence


@dataclass
class ScoreResult:
    symbol: str
    pct_change: float
    z_move: float | None
    vol_ratio: float | None
    rel_move: float | None
    rel_vs_label: str | None
    breach_high: bool
    breach_low: bool
    composite_score: float
    kind: str
    reason_text: str


def compute_score(
    symbol: str,
    price: float,
    prev_close: float,
    volume: int,
    baseline: dict | None,
    index_pct_change: float | None,
    index_label: str | None,
) -> ScoreResult:
    pct_change = (price - prev_close) / prev_close if prev_close else 0.0

    z_move = None
    if baseline and baseline.get("ret_stddev_30d"):
        z_move = pct_change / float(baseline["ret_stddev_30d"])

    vol_ratio = None
    if baseline and baseline.get("avg_volume_20d"):
        vol_ratio = volume / float(baseline["avg_volume_20d"])

    rel_move = None
    rel_vs_label = None
    if index_pct_change is not None:
        # Deliberate simplification: plain subtraction, not a beta-adjusted
        # residual -- estimating beta on 30 days of data is noisier than
        # the correction it buys. See BUILD_PLAN.md section 5.
        rel_move = pct_change - index_pct_change
        rel_vs_label = index_label

    breach_high = bool(baseline and baseline.get("wk52_high") and price > float(baseline["wk52_high"]))
    breach_low = bool(baseline and baseline.get("wk52_low") and price < float(baseline["wk52_low"]))

    z_component = 40 * min(abs(z_move) / Z_CAP, 1) if z_move is not None else 0.0
    vol_component = 30 * min(vol_ratio / VOL_RATIO_CAP, 1) if vol_ratio is not None else 0.0
    rel_component = 20 * min(abs(rel_move) / REL_MOVE_CAP, 1) if rel_move is not None else 0.0
    breach_component = 10.0 if (breach_high or breach_low) else 0.0

    composite_score = z_component + vol_component + rel_component + breach_component

    if breach_high or breach_low:
        kind = "level_breach"
    else:
        contributions = {
            "vol_spike": vol_component,
            "relative_move": rel_component,
            "z_move": z_component,
        }
        kind = max(contributions, key=contributions.get)

    reason_text = _build_reason(symbol, z_move, vol_ratio, breach_high, breach_low, rel_move, rel_vs_label)

    return ScoreResult(
        symbol=symbol,
        pct_change=pct_change,
        z_move=z_move,
        vol_ratio=vol_ratio,
        rel_move=rel_move,
        rel_vs_label=rel_vs_label,
        breach_high=breach_high,
        breach_low=breach_low,
        composite_score=composite_score,
        kind=kind,
        reason_text=reason_text,
    )


def _build_reason(symbol, z_move, vol_ratio, breach_high, breach_low, rel_move, rel_vs_label) -> str:
    parts = []
    if z_move is not None:
        parts.append(f"z={z_move:+.1f} move")
    if vol_ratio is not None:
        parts.append(f"{vol_ratio:.1f}x avg volume")
    if breach_high:
        parts.append("breaking 52-week high")
    elif breach_low:
        parts.append("breaking 52-week low")
    if rel_move is not None and abs(rel_move) >= 0.01 and rel_vs_label:
        parts.append(f"vs {rel_vs_label} {rel_move:+.1%}")

    if not parts:
        return f"{symbol}  no significant signal"
    return f"{symbol}  " + " | ".join(parts)


def upsert_event(conn, now: datetime, price: float, score: ScoreResult) -> str | None:
    """Apply hysteresis + clustering. Returns the cluster_key touched, or
    None if this reading wasn't significant enough to open or extend
    anything. Deliberately never marks an event 'closed' -- last_updated_ts
    simply stops advancing once the score drops below CLOSE_THRESHOLD,
    which is itself the signal that a move reverted (see BUILD_PLAN.md
    section 6, the revert-detection hero feature)."""

    if score.composite_score < CLOSE_THRESHOLD:
        return None

    recent = conn.execute(
        text(
            """
            SELECT id, cluster_key, last_updated_ts, peak_price, trough_price,
                   score, reason_text, kind
            FROM events
            WHERE symbol = :symbol
            ORDER BY last_updated_ts DESC
            LIMIT 1
            """
        ),
        {"symbol": score.symbol},
    ).mappings().first()

    is_extend = recent is not None and (
        (now - recent["last_updated_ts"]).total_seconds() <= EXTEND_WINDOW_MINUTES * 60
    )

    if is_extend:
        peak = max(float(recent["peak_price"] or price), price)
        trough = min(float(recent["trough_price"] or price), price)

        # An event's headline is its PEAK reading, not its latest. A move
        # that spiked hard and is now easing off should still be reported
        # by what made it significant -- overwriting on every extension
        # quietly waters the story down as the move decays.
        keeps_old = float(recent["score"]) >= score.composite_score
        headline_score = float(recent["score"]) if keeps_old else score.composite_score
        headline_reason = recent["reason_text"] if keeps_old else score.reason_text
        headline_kind = recent["kind"] if keeps_old else score.kind

        conn.execute(
            text(
                """
                UPDATE events
                SET score = :score, reason_text = :reason, last_updated_ts = :now,
                    peak_price = :peak, trough_price = :trough, kind = :kind
                WHERE id = :id
                """
            ),
            {
                "score": headline_score,
                "reason": headline_reason,
                "now": now,
                "peak": peak,
                "trough": trough,
                "kind": headline_kind,
                "id": recent["id"],
            },
        )
        return recent["cluster_key"]

    if score.composite_score >= OPEN_THRESHOLD:
        cluster_key = f"{score.symbol}-{now.isoformat()}"
        conn.execute(
            text(
                """
                INSERT INTO events
                    (symbol, kind, score, reason_text, first_seen_ts, last_updated_ts,
                     cluster_key, peak_price, trough_price)
                VALUES
                    (:symbol, :kind, :score, :reason, :now, :now, :cluster_key, :price, :price)
                """
            ),
            {
                "symbol": score.symbol,
                "kind": score.kind,
                "score": score.composite_score,
                "reason": score.reason_text,
                "now": now,
                "cluster_key": cluster_key,
                "price": price,
            },
        )
        return cluster_key

    return None
