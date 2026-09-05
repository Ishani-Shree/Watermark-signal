"""
Scoring tests.

These target the judgement calls, not the arithmetic: that a quiet stock
stays quiet, that a real move clears the bar, that a missing input degrades
instead of crashing, and that the reason string never claims something the
data does not support.
"""

import pytest

from app.detection import CLOSE_THRESHOLD, OPEN_THRESHOLD, compute_score

# Real seeded values for RELIANCE.NS, so the numbers mean something.
BASELINE = {
    "ret_stddev_30d": 0.01172,
    "avg_volume_20d": 10_498_450,
    "wk52_high": 1584.97,
    "wk52_low": 1258.80,
}
RESTING_PRICE = 1400.0  # comfortably inside the 52-week range


def score(**overrides):
    kwargs = {
        "symbol": "RELIANCE.NS",
        "price": RESTING_PRICE,
        "prev_close": RESTING_PRICE,
        "volume": int(BASELINE["avg_volume_20d"]),
        "baseline": BASELINE,
        "index_pct_change": 0.0,
        "index_label": "NIFTY",
    }
    kwargs.update(overrides)
    return compute_score(**kwargs)


class TestQuietIsQuiet:
    """The digest is only useful if boring stocks stay out of it."""

    def test_unchanged_price_scores_below_the_bar(self):
        result = score()
        assert result.composite_score < CLOSE_THRESHOLD

    def test_move_within_normal_volatility_stays_quiet(self):
        # +1% on a stock whose daily stddev is ~1.17% is under 1 sigma.
        result = score(price=RESTING_PRICE * 1.01)
        assert result.composite_score < CLOSE_THRESHOLD

    def test_whole_market_moving_together_is_not_stock_specific_news(self):
        # Stock +2%, index +2%: the relative component contributes nothing.
        moved = score(price=RESTING_PRICE * 1.02, index_pct_change=0.02)
        alone = score(price=RESTING_PRICE * 1.02, index_pct_change=0.0)
        assert moved.rel_move == pytest.approx(0.0, abs=1e-9)
        assert moved.composite_score < alone.composite_score


class TestRealMovesSurface:
    def test_large_volatility_relative_move_opens_an_event(self):
        # ~+5% against a 1.17% stddev is over 4 sigma.
        result = score(price=RESTING_PRICE * 1.05, volume=30_000_000)
        assert result.composite_score >= OPEN_THRESHOLD
        assert result.z_move > 4

    def test_volume_spike_is_scored_even_without_a_price_move(self):
        quiet = score()
        spike = score(volume=int(BASELINE["avg_volume_20d"] * 3))
        assert spike.composite_score > quiet.composite_score

    def test_breaching_the_52_week_high_is_flagged_and_named(self):
        result = score(price=BASELINE["wk52_high"] + 1)
        assert result.breach_high is True
        assert result.kind == "level_breach"
        assert "52-week high" in result.reason_text

    def test_breaching_the_52_week_low_is_flagged(self):
        result = score(price=BASELINE["wk52_low"] - 1, prev_close=BASELINE["wk52_low"])
        assert result.breach_low is True
        assert "52-week low" in result.reason_text

    def test_sitting_exactly_at_the_52_week_high_is_not_a_breach(self):
        # Strictly greater-than: touching the high is not breaking it.
        result = score(price=BASELINE["wk52_high"])
        assert result.breach_high is False


class TestDegradesRatherThanCrashes:
    """Named edge cases from BUILD_PLAN.md section 10."""

    def test_no_baseline_at_all(self):
        result = score(baseline=None)
        assert result.z_move is None
        assert result.vol_ratio is None
        assert result.composite_score >= 0

    def test_thin_history_gives_no_z_score_instead_of_a_garbage_one(self):
        thin = dict(BASELINE, ret_stddev_30d=None)
        result = score(baseline=thin, price=RESTING_PRICE * 1.05)
        assert result.z_move is None
        assert "z=" not in result.reason_text

    def test_zero_previous_close_does_not_divide_by_zero(self):
        result = score(prev_close=0)
        assert result.pct_change == 0.0

    def test_zero_average_volume_does_not_divide_by_zero(self):
        result = score(baseline=dict(BASELINE, avg_volume_20d=0))
        assert result.vol_ratio is None

    def test_market_closed_no_index_quote_available(self):
        result = score(index_pct_change=None, index_label=None)
        assert result.rel_move is None
        assert "vs" not in result.reason_text


class TestReasonStringHonesty:
    """The reason string is shown to users verbatim; it must not overclaim."""

    def test_nifty_fallback_is_labelled_nifty_not_sector(self):
        result = score(price=RESTING_PRICE * 1.05, index_label="NIFTY")
        assert "vs NIFTY" in result.reason_text
        assert "sector" not in result.reason_text

    def test_sector_comparison_is_labelled_sector(self):
        result = score(price=RESTING_PRICE * 1.05, index_label="sector")
        assert "vs sector" in result.reason_text

    def test_is_ascii_only(self):
        # A stray sigma or middot has crashed a Windows console here before.
        result = score(price=RESTING_PRICE * 1.05, volume=30_000_000)
        result.reason_text.encode("ascii")

    def test_names_every_signal_that_contributed(self):
        result = score(price=BASELINE["wk52_high"] + 1, volume=30_000_000)
        for fragment in ["z=", "avg volume", "52-week high"]:
            assert fragment in result.reason_text


class TestScoreBounds:
    def test_never_exceeds_100(self):
        result = score(price=RESTING_PRICE * 10, volume=10**12)
        assert result.composite_score <= 100

    def test_never_negative(self):
        result = score(price=RESTING_PRICE * 0.5, prev_close=RESTING_PRICE)
        assert result.composite_score >= 0

    def test_a_fall_scores_like_an_equivalent_rise(self):
        # Direction is carried by the reason text, not by the magnitude.
        up = score(price=RESTING_PRICE * 1.05)
        down = score(price=RESTING_PRICE * 0.95)
        assert up.composite_score == pytest.approx(down.composite_score, abs=0.01)
        assert up.z_move > 0 and down.z_move < 0
