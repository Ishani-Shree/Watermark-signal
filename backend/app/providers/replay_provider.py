from datetime import datetime, timedelta, timezone

from .base import PriceProvider, Quote

# Deterministic scripted price path per symbol, in minutes-since-start.
# RELIANCE.NS is seeded with a spike-and-revert: up 5.1% on heavy volume,
# then back down to baseline -- proves revert detection on demand.
SCRIPTS: dict[str, list[tuple[int, float, int]]] = {
    "RELIANCE.NS": [
        (0, 2900.0, 500_000),
        (10, 2905.0, 520_000),
        (20, 3048.0, 1_600_000),  # +5.1% spike, 3.2x volume
        (35, 3010.0, 900_000),
        (50, 2907.0, 480_000),  # reverted
        (65, 2903.0, 460_000),
    ],
    "TCS.NS": [
        (0, 3800.0, 300_000),
        (30, 3812.0, 310_000),
        (60, 3795.0, 295_000),
    ],
}
DEFAULT_SCRIPT = [(0, 1000.0, 100_000), (30, 1005.0, 105_000), (60, 998.0, 98_000)]

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
