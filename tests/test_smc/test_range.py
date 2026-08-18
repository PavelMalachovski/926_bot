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

# A staircase of four rallies, each peak 0.9 higher than the last (tolerance
# below is 1.0, so every step is within tolerance of its neighbor), with
# three tight dips back to ~79.0 between/after them. find_pivots resolves
# this to confirmed highs 100.0, 100.9, 101.8, 102.7 (indices 4, 12, 20, 28)
# and confirmed lows 79.0, 79.0, 79.0 (indices 8, 16, 24) — verified while
# building this fixture. The highs are each a neighbor-step apart but span
# 2.7 end to end (nearly three tolerances): a chain/transitive clustering
# bug would merge all four into one "boundary" that is not a real level.
STAIRCASE = [
    80.0, 85.0, 90.0, 95.0, 99.0,
    95.0, 90.0, 85.0, 80.0,
    85.0, 90.0, 95.0, 99.9,
    95.0, 90.0, 85.0, 80.0,
    85.0, 90.0, 95.0, 100.8,
    95.0, 90.0, 85.0, 80.0,
    85.0, 90.0, 95.0, 101.7,
    95.0, 90.0, 85.0,
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

    def test_a_genuine_near_equal_cluster_still_forms_one_boundary(self):
        """The span-based clustering fix must not over-tighten: two real
        highs 0.4 apart (well inside tolerance=1.0) still merge into a
        single two-touch boundary, not two singletons."""
        rng = _range()
        assert rng is not None
        assert rng.touches_top == 2
        assert abs(rng.top - 119.8) < 0.01  # mean of the two merged highs

    def test_a_staircase_does_not_collapse_into_one_boundary(self):
        """D7: pivots each within tolerance of their neighbor, but drifting
        several tolerances end to end, are a staircase, not one level.

        STAIRCASE's highs are 100.0, 100.9, 101.8, 102.7 (tolerance=1.0):
        every adjacent pair is a neighbor-step apart, but the whole run
        spans 2.7 -- almost three tolerances. Chain/transitive clustering
        (comparing only to the tail already accepted) would merge all four
        into one cluster spanning 2.7, mean 101.35, touches_top=4 -- not a
        level any real liquidity pool would sit at. The span-bounded fix
        must instead split it into two-pivot runs and keep only the
        largest (tied, so the most recent wins): (101.8, 102.7).
        """
        candles = make_candles(STAIRCASE, step_minutes=60)
        rng = detect_range(candles, 1.0, 120)
        assert rng is not None
        assert rng.touches_top == 2  # not 4 -- the chain-merge bug's count
        assert 101.8 <= rng.top <= 102.7  # span-bounded mean, not 101.35

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

    def test_clean_boundaries_are_not_swept(self):
        """No candle ever pierced either edge on the base fixture — the
        control that makes the sweep assertions below meaningful."""
        rng = _range()
        assert rng is not None
        assert rng.swept_top is False
        assert rng.swept_bottom is False

    def test_a_wick_through_the_top_that_closes_back_inside_does_not_break_it(self):
        """D9: a wick beyond the top whose body closes back inside is
        liquidity taken, not a breakout — it must not break the range, and
        it must be flagged as swept.

        The appended candle opens at the last RANGING close (114.0) and
        closes at 119.5 (bullish, so make_candles wicks +1.0 above the
        higher of open/close): high 120.5 pierces top (119.8) while the
        body (114.0-119.5) stays entirely inside it.
        """
        candles = make_candles(RANGING + [119.5], step_minutes=60)
        pierced = candles[-1]
        rng = detect_range(candles, 1.0, 120)
        assert rng is not None
        assert pierced.high > rng.top  # the wick genuinely exceeds the boundary
        assert pierced.body_high <= rng.top  # but the body stays inside
        assert rng.broken is False
        assert rng.swept_top is True

    def test_a_wick_through_the_bottom_that_closes_back_inside_does_not_break_it(self):
        """Mirror of the top case: a wick below the bottom whose body
        closes back inside is swept liquidity, not a breakdown.

        The appended candle opens at 114.0 and closes at 100.3 (bearish, so
        make_candles wicks -1.0 below the lower of open/close): low 99.3
        pierces bottom (99.7) while the body (100.3-114.0) stays inside it.
        """
        candles = make_candles(RANGING + [100.3], step_minutes=60)
        pierced = candles[-1]
        rng = detect_range(candles, 1.0, 120)
        assert rng is not None
        assert pierced.low < rng.bottom  # the wick genuinely exceeds the boundary
        assert pierced.body_low >= rng.bottom  # but the body stays inside
        assert rng.broken is False
        assert rng.swept_bottom is True

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
