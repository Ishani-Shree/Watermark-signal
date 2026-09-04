from datetime import datetime, timedelta, timezone

from .base import PriceProvider, Quote

# Deterministic scripted price path per symbol, in minutes-since-start.
# Prices AND volumes are scaled to each symbol's REAL seeded baseline
# (backend/scripts/seed_baselines.py) -- a synthetic spike (or a quiet,
# boring stock) is only meaningful if it's significant relative to the
# real avg_volume_20d / wk52_high / wk52_low it gets scored against.
# Getting this wrong means every symbol looks like it's breaching its
# 52-week range on every tick, which would drown out the actual signal.
SCRIPTS: dict[str, list[tuple[int, float, int]]] = {
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
    # The rest are quiet by design -- flat, mid-range prices with volume at
    # their real baseline, so they correctly score near zero and stay out
    # of the digest (the "suppressed_count" / restraint story).
    "INFY.NS": [(0, 1450.0, 7_950_000)],
    "HDFCBANK.NS": [(0, 900.0, 26_700_000)],
    "ICICIBANK.NS": [(0, 1380.0, 8_770_000)],
    "AXISBANK.NS": [(0, 1300.0, 4_790_000)],
    "SBIN.NS": [(0, 1080.0, 8_340_000)],
    "ITC.NS": [(0, 358.0, 14_100_000)],
    "LT.NS": [(0, 4055.0, 1_300_000)],
    "WIPRO.NS": [(0, 234.0, 7_940_000)],
}
DEFAULT_SCRIPT = [(0, 1000.0, 100_000)]  # fallback only for symbols not listed above

_START = datetime.now(timezone.utc)


class ReplayProvider(PriceProvider):
    """Scripted, deterministic feed. Used when the live provider is down
    or when a demo needs to reproduce the same sequence on camera."""

    def get_latest(self, symbol: str) -> Quote | None:
        script = SCRIPTS.get(symbol, DEFAULT_SCRIPT)
        elapsed_min = (datetime.now(timezone.utc) - _START).total_seconds() / 60
        point = script[0]
        for candidate in script:
            if candidate[0] <= elapsed_min:
                point = candidate
        minute, price, volume = point
        prev_close = script[0][1]
        return Quote(
            symbol=symbol,
            price=price,
            volume=volume,
            prev_close=prev_close,
            source_ts=_START + timedelta(minutes=minute),
            source="replay",
        )

    def get_history(self, symbol: str, days: int) -> list[Quote]:
        script = SCRIPTS.get(symbol, DEFAULT_SCRIPT)
        return [
            Quote(
                symbol=symbol,
                price=price,
                volume=volume,
                prev_close=script[0][1],
                source_ts=_START - timedelta(days=days) + timedelta(minutes=minute),
                source="replay",
            )
            for minute, price, volume in script
        ]
