from ..provider_health import ProviderUnavailable, health
from .base import PriceProvider, Quote


class GuardedProvider(PriceProvider):
    """Wraps a provider with the circuit breaker and the chaos switch.

    Deliberately does NOT substitute a different data source on failure.
    Falling back to another provider would hand the user a price that did
    not come from where they think it did; letting the last known snapshot
    age and be labelled stale is the honest degradation. See
    BUILD_PLAN.md section 9 -- "staleness visible, never smoothed".
    """

    def __init__(self, inner: PriceProvider):
        self.inner = inner
        self.source_name = inner.source_name

    def get_latest(self, symbol: str) -> Quote | None:
        if not health.should_attempt():
            raise ProviderUnavailable(
                f"circuit breaker open after {health.consecutive_failures} failures"
            )

        try:
            if health.chaos_enabled:
                raise RuntimeError("chaos: simulated provider outage")
            quote = self.inner.get_latest(symbol)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream is unofficial, anything can surface
            health.record_failure(f"{type(exc).__name__}: {exc}")
            raise

        health.record_success()
        return quote

    def get_latest_batch(self, symbols: list[str]) -> dict[str, Quote]:
        """The batch path has to carry the same guard as the single path --
        a breaker that only covers one of two entry points is not a breaker.
        A batch is one upstream call, so it counts as one success or one
        failure, not one per symbol."""
        if not health.should_attempt():
            raise ProviderUnavailable(
                f"circuit breaker open after {health.consecutive_failures} failures"
            )

        try:
            if health.chaos_enabled:
                raise RuntimeError("chaos: simulated provider outage")
            quotes = self.inner.get_latest_batch(symbols)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream is unofficial
            health.record_failure(f"{type(exc).__name__}: {exc}")
            raise

        # An empty result for a non-empty request means the upstream answered
        # with nothing usable. Treating that as success would leave the
        # breaker closed over a provider that is effectively down.
        if symbols and not quotes:
            health.record_failure("batch returned no usable quotes")
        else:
            health.record_success()
        return quotes

    def get_history(self, symbol: str, days: int) -> list[Quote]:
        return self.inner.get_history(symbol, days)
