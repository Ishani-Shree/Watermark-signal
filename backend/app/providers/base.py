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

    #: Identifies the data source in stored rows. A caller that instead
    #: reads the configured provider name would mislabel any cycle run
    #: with an overridden provider.
    source_name: str = "unknown"

    @abstractmethod
    def get_latest(self, symbol: str) -> Quote | None:
        ...

    def get_latest_batch(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch many symbols at once.

        The default loops, which is right for an in-memory feed. A network
        provider should override it: with a remote upstream the request
        count, not the parsing, is what grows with the universe -- and it is
        also what gets you rate-limited.

        A symbol that fails is simply absent from the result rather than
        raising, so one bad ticker cannot cost the whole batch.
        """
        quotes: dict[str, Quote] = {}
        for symbol in symbols:
            quote = self.get_latest(symbol)
            if quote is not None:
                quotes[symbol] = quote
        return quotes

    @abstractmethod
    def get_history(self, symbol: str, days: int) -> list[Quote]:
        ...
