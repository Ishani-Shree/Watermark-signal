"""
Provider health: a circuit breaker around an unreliable upstream, plus a
chaos switch to prove it works.

yfinance is unofficial Yahoo scraping (BUILD_PLAN.md section 14 names it as
a risk, and it has already broken once during this build). The failure mode
that matters is not a single bad fetch -- it is hammering a dead provider
every cycle and, worse, letting that failure surface to the user as a blank
screen or a confidently wrong price.

Policy here:
  * failures are counted, not swallowed silently
  * after FAILURE_THRESHOLD consecutive failures the breaker OPENS and we
    stop calling the provider for COOLDOWN_SECONDS
  * while open, ingestion writes nothing -- existing snapshots simply age,
    and the UI shows them as stale. Never invent a price to fill the gap.
  * one HALF-OPEN probe is allowed after the cooldown; success closes the
    breaker, failure re-opens it
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 60


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    opened_at: datetime | None = None
    last_error: str | None = None
    last_success_at: datetime | None = None
    total_failures: int = 0

    # Chaos switch: makes every fetch raise, so the degraded path can be
    # demonstrated on demand instead of waiting for Yahoo to have a bad day.
    chaos_enabled: bool = False
    _listeners: list = field(default_factory=list, repr=False)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if datetime.now(timezone.utc) - self.opened_at >= timedelta(seconds=COOLDOWN_SECONDS):
            return "half_open"
        return "open"

    def should_attempt(self) -> bool:
        """Open means: do not call the provider at all. Half-open allows a
        single probe through to see whether it has recovered."""
        return self.state != "open"

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_error = None
        self.last_success_at = datetime.now(timezone.utc)

    def record_failure(self, error: str) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = error[:200]
        # A failure while half-open re-opens immediately: the cooldown has
        # already been served and the provider is still unwell.
        if self.state == "half_open" or self.consecutive_failures >= FAILURE_THRESHOLD:
            self.opened_at = datetime.now(timezone.utc)

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "chaos_enabled": self.chaos_enabled,
        }


health = ProviderHealth()


class ProviderUnavailable(RuntimeError):
    """Raised when the breaker is open, so callers degrade deliberately
    rather than treating an absent price as a real one."""
