from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Quote:
    """Two timestamps, deliberately.

    `source_ts` is when the PRICE is from -- the provider's own market
    timestamp. It is the dedup key: re-fetching an unchanged quote must
    yield the same value, or `ON CONFLICT (symbol, source_ts)` protects
    nothing.

    `fetched_at` is when WE polled. It answers a different question: is the
    pipeline alive? A stock that has not traded for an hour has an old
    `source_ts` and a fresh `fetched_at`, and that is healthy -- not stale
    data. Collapsing the two loses the distinction between "the price has
    not moved" and "we have not looked".
    """

    symbol: str
    price: float
    volume: int
    prev_close: float
    source_ts: datetime
    fetched_at: datetime
    source: str
    # False when the provider gave us no market timestamp and source_ts had
    # to fall back to poll time. Surfaced rather than hidden, because it
    # means dedup is not guaranteed for this row.
    has_market_ts: bool = True


class PriceProvider(ABC):
    """Adapter boundary: nothing outside this module talks to a real data
    source directly. Callers get a Quote or a list of Quotes regardless of
    whether the data came from yfinance or the scripted replay feed."""

    @abstractmethod
    def get_latest(self, symbol: str) -> Quote | None:
        ...

    @abstractmethod
    def get_history(self, symbol: str, days: int) -> list[Quote]:
        ...
