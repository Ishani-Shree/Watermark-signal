from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Quote:
    symbol: str
    price: float
    volume: int
    prev_close: float
    source_ts: datetime
    source: str


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
