from ..config import settings
from .base import PriceProvider
from .guarded import GuardedProvider
from .replay_provider import ReplayProvider
from .yfinance_provider import YFinanceProvider


def get_provider() -> PriceProvider:
    """Every caller gets the guarded provider -- the breaker and the chaos
    switch are not optional extras a code path can forget to apply."""
    inner = YFinanceProvider() if settings.provider == "yfinance" else ReplayProvider()
    return GuardedProvider(inner)
