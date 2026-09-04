from ..config import settings
from .base import PriceProvider
from .replay_provider import ReplayProvider
from .yfinance_provider import YFinanceProvider


def get_provider() -> PriceProvider:
    if settings.provider == "yfinance":
        return YFinanceProvider()
    return ReplayProvider()
