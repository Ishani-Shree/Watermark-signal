"""
Digest-shaping tests.

The database-backed parts of build_digest are covered by the manual flows;
what is tested here is the judgement that decides WHAT a user is shown --
whether a move counts as reverted, and whether the path summary tells the
truth about where the price ended up.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.ranking import REVERTED_OFF_PEAK_PCT, _aggregate_by_symbol, _has_reverted

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def event(symbol="RELIANCE.NS", peak=1639.60, trough=1560.0, score=100.0, minutes_ago=30):
    return {
        "symbol": symbol,
        "kind": "level_breach",
        "score": Decimal(str(score)),  # Postgres NUMERIC arrives as Decimal
        "reason_text": f"{symbol}  z=+4.5 move",
        "first_seen_ts": NOW - timedelta(minutes=minutes_ago + 15),
        "last_updated_ts": NOW - timedelta(minutes=minutes_ago),
        "peak_price": Decimal(str(peak)) if peak is not None else None,
        "trough_price": Decimal(str(trough)) if trough is not None else None,
    }


class TestRevertDetection:
    """The hero feature: a move that happened and came back."""

    def test_price_well_below_peak_counts_as_reverted(self):
        assert _has_reverted([event(peak=1639.60)], 1562.0) is True

    def test_price_still_at_the_peak_has_not_reverted(self):
        assert _has_reverted([event(peak=1639.60)], 1639.60) is False

    def test_price_above_the_peak_has_not_reverted(self):
        assert _has_reverted([event(peak=1639.60)], 1700.0) is False

    def test_threshold_is_respected_on_both_sides(self):
        peak = 1000.0
        just_under = peak * (1 - REVERTED_OFF_PEAK_PCT / 2)
        just_over = peak * (1 - REVERTED_OFF_PEAK_PCT * 2)
        assert _has_reverted([event(peak=peak)], just_under) is False
        assert _has_reverted([event(peak=peak)], just_over) is True

    def test_unknown_current_price_is_not_treated_as_a_revert(self):
        # No price is not evidence of a fall.
        assert _has_reverted([event()], None) is False

    def test_event_without_a_peak_cannot_have_reverted(self):
        assert _has_reverted([event(peak=None)], 100.0) is False

    def test_uses_the_highest_peak_across_several_events(self):
        events = [event(peak=1500.0), event(peak=1639.60)]
        # 1562 is off the higher peak but above the lower one.
        assert _has_reverted(events, 1562.0) is True


class TestPathSummary:
    def test_reports_where_the_price_actually_ended_up(self):
        [summary] = _aggregate_by_symbol([event()], {"RELIANCE.NS": 1562.0})
        assert summary["peak_price"] == 1639.60
        assert summary["current_price"] == 1562.0
        assert summary["reverted"] is True
        assert "peaked 1639.60" in summary["reason_text"]
        assert "now 1562.00" in summary["reason_text"]
        assert "off peak" in summary["reason_text"]

    def test_does_not_claim_a_revert_when_the_price_held(self):
        [summary] = _aggregate_by_symbol([event()], {"RELIANCE.NS": 1639.60})
        assert summary["reverted"] is False
        assert "off peak" not in summary["reason_text"]

    def test_handles_postgres_decimals_without_type_errors(self):
        # Mixing Decimal (from NUMERIC columns) with float has bitten here.
        [summary] = _aggregate_by_symbol([event()], {"RELIANCE.NS": Decimal("1562.00")})
        assert isinstance(summary["peak_price"], float)
        assert isinstance(summary["current_price"], float)

    def test_collapses_multiple_events_for_one_symbol(self):
        events = [event(peak=1600.0, trough=1580.0), event(peak=1639.60, trough=1560.0)]
        [summary] = _aggregate_by_symbol(events, {"RELIANCE.NS": 1562.0})
        assert summary["event_count"] == 2
        assert summary["peak_price"] == 1639.60  # widest extent, not the last one
        assert summary["trough_price"] == 1560.0
        assert "2 events" in summary["reason_text"]

    def test_keeps_symbols_separate(self):
        events = [event(symbol="RELIANCE.NS"), event(symbol="ITC.NS", peak=300.0, trough=290.0)]
        results = _aggregate_by_symbol(events, {"RELIANCE.NS": 1562.0, "ITC.NS": 295.0})
        assert {r["symbol"] for r in results} == {"RELIANCE.NS", "ITC.NS"}

    def test_missing_current_price_still_produces_a_usable_summary(self):
        [summary] = _aggregate_by_symbol([event()], {})
        assert summary["current_price"] is None
        assert summary["reverted"] is False
        assert "peaked" in summary["reason_text"]

    def test_score_is_the_strongest_event_not_the_latest(self):
        events = [event(score=60.0), event(score=100.0), event(score=40.0)]
        [summary] = _aggregate_by_symbol(events, {"RELIANCE.NS": 1562.0})
        assert summary["score"] == 100.0

    def test_reason_string_is_ascii_only(self):
        [summary] = _aggregate_by_symbol([event()], {"RELIANCE.NS": 1562.0})
        summary["reason_text"].encode("ascii")
