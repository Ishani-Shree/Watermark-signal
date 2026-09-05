import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import PriceProvider, Quote

# Deterministic scripted price path per symbol, in minutes-since-start.
# Prices AND volumes are scaled to each symbol's REAL seeded baseline
# (backend/scripts/seed_baselines.py) -- a synthetic spike (or a quiet,
# boring stock) is only meaningful if it's significant relative to the
# real avg_volume_20d / wk52_high / wk52_low it gets scored against.
# Getting this wrong means every symbol looks like it's breaching its
# 52-week range on every tick, which would drown out the actual signal.
#
# ACTORS are hand-authored: the few symbols that actually do something in
# the demo scenario. Every other symbol gets a flat, resting script built
# from its own real last close and average volume, loaded from the
# generated replay_baseline.json -- so the universe can grow to any size
# without anyone hand-picking plausible numbers for each new ticker.
ACTORS: dict[str, list[tuple[int, float, int]]] = {
    # Real wk52 range [1258.80, 1584.97], avg_volume_20d ~10.5M.
    # Spike-and-revert: baseline sits just under the 52w high, the spike
    # genuinely breaches it on ~3.2x volume, then it reverts back under --
    # proves revert detection on demand (BUILD_PLAN.md section 6).
    "RELIANCE.NS": [
        (0, 1560.0, 10_500_000),
        (10, 1565.0, 10_800_000),
        (20, 1639.6, 33_600_000),  # +5.1% from baseline, breaches wk52_high
        (35, 1610.0, 18_000_000),
        (50, 1566.0, 9_800_000),  # reverted, back under the high
        (65, 1562.0, 9_500_000),
    ],
    # Real wk52 range [1971.79, 3204.28], avg_volume_20d ~2.6M.
    # Deliberately boring -- the contrast case that should stay suppressed.
    "TCS.NS": [
        (0, 3100.0, 2_600_000),
        (30, 3112.0, 2_700_000),
        (60, 3095.0, 2_500_000),
    ],
    # Market barely moves while RELIANCE spikes -- makes the relative-move
    # signal (stock-specific vs market-wide) obvious in the demo.
    # Real wk52 range [22331.40, 26328.55], avg_volume_20d ~291k.
    "^NSEI": [
        (0, 24500.0, 300_000),
        (20, 24512.0, 310_000),
        (35, 24521.0, 305_000),
        (50, 24507.0, 295_000),
        (65, 24495.0, 302_000),
    ],
}

_BASELINE_PATH = Path(__file__).resolve().parent / "replay_baseline.json"


def _load_scripts() -> dict[str, list[tuple[int, float, int]]]:
    """Hand-authored actors win; everything else rests at its real last
    close. A symbol resting at its own last close has 0% change and a 1.0x
    volume ratio, so it scores near zero and correctly stays out of the
    digest -- which is what a quiet stock should do."""
    scripts: dict[str, list[tuple[int, float, int]]] = {}

    try:
        resting = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing or unreadable: the actors alone still drive the scenario.
        # Better to run with a smaller universe than to invent prices.
        resting = {}

    for symbol, row in resting.items():
        scripts[symbol] = [(0, float(row["price"]), int(row["volume"]))]

    scripts.update(ACTORS)
    return scripts


SCRIPTS = _load_scripts()

# Only reached by a symbol with neither an actor script nor a seeded
# baseline -- which should not happen, since seeding writes both together.
DEFAULT_SCRIPT = [(0, 1000.0, 100_000)]

_START = datetime.now(timezone.utc)

# Demo clock. Normally None -- the feed advances with real elapsed time.
# Pinning it to a script minute lets a demo step through the scenario on
# command instead of waiting an hour for it, while still driving the REAL
# detection pipeline: nothing is injected, the scoring layer sees the same
# quotes it would have seen live and reaches its own conclusions.
#
# `anchor_end` maps the scripted timeline onto REAL wall-clock time ending
# now, so a replayed scenario lands in the recent past. Stamping it forward
# from server start would put quotes up to an hour in the future, and a
# future timestamp is never "since you last looked" -- the event would
# resurface on every refresh no matter how many times you read it.
_clock: dict[str, object] = {"pinned_minute": None, "anchor_end": None, "span": 0.0}


def pin_minute(minute: float | None, anchor_end: datetime | None = None, span: float | None = None) -> None:
    _clock["pinned_minute"] = minute
    if anchor_end is not None:
        _clock["anchor_end"] = anchor_end
    if span is not None:
        _clock["span"] = span


def pinned_minute() -> float | None:
    return _clock["pinned_minute"]


def scenario_minutes() -> list[int]:
    """Every distinct script minute across all symbols, in order -- the
    full timeline a demo run should step through."""
    minutes = {m for script in SCRIPTS.values() for m, _, _ in script}
    return sorted(minutes)


class ReplayProvider(PriceProvider):
    """Scripted, deterministic feed. Used when the live provider is down
    or when a demo needs to reproduce the same sequence on camera."""

    source_name = "replay"

    def get_latest(self, symbol: str) -> Quote | None:
        script = SCRIPTS.get(symbol, DEFAULT_SCRIPT)
        pinned = _clock["pinned_minute"]
        now = datetime.now(timezone.utc)

        if pinned is None:
            elapsed_min = (now - _START).total_seconds() / 60
            observed_at = _START + timedelta(minutes=elapsed_min)
        else:
            elapsed_min = pinned
            anchor_end = _clock["anchor_end"] or now
            span = float(_clock["span"] or 0.0)
            # Scenario minute -> real time, ending at the anchor.
            observed_at = anchor_end - timedelta(minutes=span - elapsed_min)

        point = script[0]
        for candidate in script:
            if candidate[0] <= elapsed_min:
                point = candidate
        point_minute, price, volume = point
        prev_close = script[0][1]

        # source_ts is the market time of the SCRIPT POINT -- when this price
        # came into being. Polling the same unchanged quote ten times yields
        # the same source_ts ten times, which is exactly what makes the
        # `(symbol, source_ts)` dedup meaningful. fetched_at carries the
        # poll time separately.
        if pinned is None:
            price_at = _START + timedelta(minutes=point_minute)
        else:
            anchor_end = _clock["anchor_end"] or now
            span = float(_clock["span"] or 0.0)
            price_at = anchor_end - timedelta(minutes=span - point_minute)

        return Quote(
            symbol=symbol,
            price=price,
            volume=volume,
            prev_close=prev_close,
            source_ts=price_at,
            fetched_at=observed_at,
            source="replay",
        )

    def get_history(self, symbol: str, days: int) -> list[Quote]:
        script = SCRIPTS.get(symbol, DEFAULT_SCRIPT)
        now = datetime.now(timezone.utc)
        return [
            Quote(
                symbol=symbol,
                price=price,
                volume=volume,
                prev_close=script[0][1],
                source_ts=_START - timedelta(days=days) + timedelta(minutes=minute),
                fetched_at=now,
                source="replay",
            )
            for minute, price, volume in script
        ]
