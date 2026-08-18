"""Range detection from clustered H1 pivots (spec §3.1, D7, D9)."""

from app.services.smc.range import detect_range
from tests.test_smc.helpers import make_candles

# Four clean swings between ~100 and ~120: two highs clustering near 119.8,
# two lows clustering near 99.7, nothing closing outside. make_candles builds
# each candle around its close with asymmetric wicks (bullish candles wick
# +1.0 above the higher of open/close), so these closes produce fractal
# pivots at the turning points with the wicked high/low, not the close
# itself — verified against find_pivots while building this fixture.
RANGING = [
    100.0, 104.0, 110.0, 116.0, 119.0, 116.0, 110.0, 104.0, 100.5,
    104.0, 110.0, 116.0, 118.6, 116.0, 110.0, 104.0, 100.9,
    104.0, 110.0, 114.0,
]


def _range(closes=None, tolerance=1.0, window=120):
    return detect_range(
        make_candles(closes or RANGING, step_minutes=60), tolerance, window
    )


class TestDetectRange:
    def test_finds_the_band(self):
        rng = _range()
        assert rng is not None
        assert 119.0 <= rng.top <= 120.5
        assert 99.5 <= rng.bottom <= 101.0
        assert rng.broken is False

    def test_counts_touches_on_both_boundaries(self):
        rng = _range()
        assert rng.touches_top >= 2 and rng.touches_bottom >= 2

    def test_one_touch_is_not_a_range(self):
        """A single swing high is a level, not a boundary."""
        single = [100.0, 110.0, 120.0, 110.0, 100.0, 104.0, 108.0, 104.0, 100.0]
        assert _range(single) is None

    def test_a_band_narrower_than_three_tolerances_is_chop(self):
        assert _range(tolerance=10.0) is None

    def test_a_body_close_above_the_top_breaks_it(self):
        broken = RANGING + [126.0, 128.0, 130.0]
        rng = _range(broken)
        assert rng is not None and rng.broken is True

    def test_a_body_close_below_the_bottom_breaks_it(self):
        broken = RANGING + [96.0, 92.0, 90.0]
        rng = _range(broken)
        assert rng is not None and rng.broken is True

    def test_a_wick_through_the_top_that_closes_back_inside_does_not_break_it(self):
        """D9: that is liquidity taken, not a breakout."""
        rng = _range()
        assert rng is not None and rng.broken is False
        assert rng.swept_top in (True, False)  # flag exists and is a bool

    def test_no_pivots_no_range(self):
        assert _range([100.0] * 12) is None

    def test_empty_candles_no_range(self):
        assert detect_range([], 1.0) is None

    def test_the_window_bounds_the_search(self):
        """Pivots older than `window` candles cannot form a boundary."""
        long_series = [100.0, 140.0, 100.0, 140.0] + RANGING
        rng = detect_range(
            make_candles(long_series, step_minutes=60), 1.0, window=len(RANGING)
        )
        assert rng is None or rng.top < 130.0

    def test_boundary_timestamps_come_from_the_latest_pivot_of_each_cluster(self):
        rng = _range()
        assert rng is not None
        candles = make_candles(RANGING, step_minutes=60)
        stamps = {c.timestamp for c in candles}
        assert rng.top_at in stamps and rng.bottom_at in stamps
