"""
Resilience tests: the circuit breaker's state machine, and the replay
feed's determinism.

The replay tests matter more than they look. `source_ts` being stable for
an unchanged quote is the precondition that makes `ON CONFLICT (symbol,
source_ts)` a real dedup guarantee -- if it drifts, ingestion silently
double-counts and nothing else notices.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.provider_health import (
    COOLDOWN_SECONDS,
    FAILURE_THRESHOLD,
    ProviderHealth,
    ProviderUnavailable,
)
from app.providers.guarded import GuardedProvider
from app.providers.replay_provider import ReplayProvider, pin_minute


class TestCircuitBreaker:
    def test_starts_closed_and_willing(self):
        h = ProviderHealth()
        assert h.state == "closed"
        assert h.should_attempt() is True

    def test_a_single_failure_does_not_open_it(self):
        h = ProviderHealth()
        h.record_failure("blip")
        assert h.state == "closed"
        assert h.should_attempt() is True

    def test_opens_at_the_threshold(self):
        h = ProviderHealth()
        for _ in range(FAILURE_THRESHOLD):
            h.record_failure("down")
        assert h.state == "open"
        assert h.should_attempt() is False

    def test_success_resets_the_streak(self):
        h = ProviderHealth()
        h.record_failure("blip")
        h.record_failure("blip")
        h.record_success()
        h.record_failure("blip")
        assert h.state == "closed"  # streak restarted, not resumed

    def test_goes_half_open_after_the_cooldown(self):
        h = ProviderHealth()
        for _ in range(FAILURE_THRESHOLD):
            h.record_failure("down")
        h.opened_at = datetime.now(timezone.utc) - timedelta(seconds=COOLDOWN_SECONDS + 1)
        assert h.state == "half_open"
        assert h.should_attempt() is True  # one probe allowed through

    def test_failing_the_probe_reopens_immediately(self):
        h = ProviderHealth()
        for _ in range(FAILURE_THRESHOLD):
            h.record_failure("down")
        h.opened_at = datetime.now(timezone.utc) - timedelta(seconds=COOLDOWN_SECONDS + 1)
        assert h.state == "half_open"
        h.record_failure("still down")
        assert h.state == "open"  # cooldown already served; do not serve it again

    def test_passing_the_probe_closes_it(self):
        h = ProviderHealth()
        for _ in range(FAILURE_THRESHOLD):
            h.record_failure("down")
        h.opened_at = datetime.now(timezone.utc) - timedelta(seconds=COOLDOWN_SECONDS + 1)
        h.record_success()
        assert h.state == "closed"
        assert h.consecutive_failures == 0

    def test_snapshot_is_json_safe(self):
        import json

        h = ProviderHealth()
        h.record_failure("boom")
        json.dumps(h.snapshot())


class TestGuardedProvider:
    def test_chaos_makes_every_fetch_fail(self, monkeypatch):
        from app import provider_health

        monkeypatch.setattr(provider_health, "health", ProviderHealth())
        import app.providers.guarded as guarded_mod

        monkeypatch.setattr(guarded_mod, "health", provider_health.health)

        provider = GuardedProvider(ReplayProvider())
        guarded_mod.health.chaos_enabled = True

        with pytest.raises(RuntimeError, match="chaos"):
            provider.get_latest("RELIANCE.NS")

        # The failure must be counted, not merely raised -- otherwise the
        # breaker never learns the provider is unwell.
        assert guarded_mod.health.consecutive_failures == 1

    def test_open_breaker_raises_rather_than_returning_a_stale_price(self, monkeypatch):
        """The critical contract: a down provider yields NO price, so callers
        must degrade deliberately. Returning last-known-good from inside the
        provider would let a stale price masquerade as a fresh one."""
        import app.providers.guarded as guarded_mod

        fake = ProviderHealth()
        for _ in range(FAILURE_THRESHOLD):
            fake.record_failure("down")
        monkeypatch.setattr(guarded_mod, "health", fake)

        provider = GuardedProvider(ReplayProvider())
        with pytest.raises(ProviderUnavailable):
            provider.get_latest("RELIANCE.NS")


class TestReplayFeedDeterminism:
    def teardown_method(self):
        pin_minute(None)  # never leak a pinned clock into another test

    def test_same_pinned_moment_gives_the_same_quote(self):
        provider = ReplayProvider()
        anchor = datetime.now(timezone.utc)

        pin_minute(20, anchor_end=anchor, span=65)
        first = provider.get_latest("RELIANCE.NS")
        pin_minute(20, anchor_end=anchor, span=65)
        second = provider.get_latest("RELIANCE.NS")

        assert first.price == second.price
        # The dedup key must be identical, or ON CONFLICT protects nothing.
        assert first.source_ts == second.source_ts

    def test_source_ts_is_stable_while_the_price_is_unchanged(self):
        """A quiet symbol polled at two different moments reports the same
        source_ts (the price has not changed) but a later fetched_at."""
        provider = ReplayProvider()
        anchor = datetime.now(timezone.utc)

        pin_minute(10, anchor_end=anchor, span=65)
        early = provider.get_latest("ITC.NS")
        pin_minute(60, anchor_end=anchor, span=65)
        late = provider.get_latest("ITC.NS")

        assert early.price == late.price
        assert early.source_ts == late.source_ts  # dedups
        assert late.fetched_at > early.fetched_at  # but we did look again

    def test_the_scripted_spike_actually_spikes(self):
        provider = ReplayProvider()
        anchor = datetime.now(timezone.utc)

        pin_minute(0, anchor_end=anchor, span=65)
        before = provider.get_latest("RELIANCE.NS")
        pin_minute(20, anchor_end=anchor, span=65)
        peak = provider.get_latest("RELIANCE.NS")
        pin_minute(65, anchor_end=anchor, span=65)
        after = provider.get_latest("RELIANCE.NS")

        assert peak.price > before.price * 1.04
        assert peak.volume > before.volume * 3
        assert after.price == pytest.approx(before.price, rel=0.01)  # reverted

    def test_scenario_timestamps_never_land_in_the_future(self):
        """A future source_ts is never 'since you last looked' -- it would
        resurface on every refresh no matter how often it was read."""
        provider = ReplayProvider()
        anchor = datetime.now(timezone.utc)

        for minute in (0, 20, 35, 65):
            pin_minute(minute, anchor_end=anchor, span=65)
            quote = provider.get_latest("RELIANCE.NS")
            assert quote.source_ts <= anchor
            assert quote.fetched_at <= anchor

    def test_unscripted_symbols_rest_inside_their_real_range(self):
        """Generated resting prices come from real last closes; a symbol
        priced at the 1000.0 fallback means the baseline file is missing."""
        provider = ReplayProvider()
        quote = provider.get_latest("ITC.NS")
        assert quote is not None
        assert quote.price != 1000.0
