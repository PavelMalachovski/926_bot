"""Liquidity levels: unswept swings and EQH/EQL clusters (Rule 7 targets)."""

from app.services.smc.liquidity import (
    LiquidityLevel,
    find_liquidity,
    nearest_liquidity,
)
from app.services.smc.models import Direction
from tests.test_smc.helpers import candle

# One high pivot at index 2 (3160) and one low pivot at index 5 (3140),
# neither taken out by any later candle.
_BASE = [
    (3140.0, 3142.0, 3138.0, 3141.0),
    (3141.0, 3148.0, 3140.0, 3147.0),
    (3147.0, 3160.0, 3146.0, 3158.0),   # high pivot 3160
    (3158.0, 3159.0, 3150.0, 3151.0),
    (3151.0, 3152.0, 3144.0, 3145.0),
    (3145.0, 3146.0, 3140.0, 3141.0),   # low pivot 3140
    (3141.0, 3147.0, 3140.5, 3146.0),
    (3146.0, 3150.0, 3145.0, 3149.0),
    (3149.0, 3151.0, 3148.0, 3150.0),
]


def _series(spec):
    return [candle(*row, index=i) for i, row in enumerate(spec)]


class TestFindLiquidity:
    def test_unswept_swings_become_levels(self):
        levels = find_liquidity(_series(_BASE), "H1", tolerance=2.0)
        highs = [lv for lv in levels if lv.is_high]
        lows = [lv for lv in levels if not lv.is_high]
        assert [lv.price for lv in highs] == [3160.0]
        assert [lv.price for lv in lows] == [3140.0]
        assert highs[0].timeframe == "H1"
        assert highs[0].equal_count == 1

    def test_wick_beyond_tolerance_sweeps_the_level(self):
        # A later candle wicks to 3163 (> 3160 + 2.0) but closes far below:
        # the pool is taken even though no body closed above it.
        swept = _series(_BASE + [(3150.0, 3163.0, 3149.0, 3150.0)])
        highs = [lv for lv in find_liquidity(swept, "H1", 2.0) if lv.is_high]
        assert highs == []

    def test_poke_inside_tolerance_does_not_sweep(self):
        # 3161.5 is only 1.5 above 3160 — less than the 2.0 tolerance, so the
        # pool survives. Without this, two "equal" highs could never coexist.
        poked = _series(_BASE + [(3150.0, 3161.5, 3149.0, 3150.0)])
        highs = [lv for lv in find_liquidity(poked, "H1", 2.0) if lv.is_high]
        assert [lv.price for lv in highs] == [3160.0]


class TestEqualHighsClustering:
    # A second attempt at the same area stalls 0.5 short of the first high:
    # two unswept highs 0.5 apart = one EQH pool.
    _EQH = _BASE + [
        (3150.0, 3159.5, 3149.0, 3158.0),   # high pivot 3159.5
        (3158.0, 3157.0, 3150.0, 3151.0),
        (3151.0, 3152.0, 3146.0, 3147.0),
        (3147.0, 3148.0, 3145.0, 3146.0),
        (3146.0, 3147.0, 3144.0, 3145.0),
    ]

    def test_two_close_highs_form_one_cluster(self):
        highs = [lv for lv in find_liquidity(_series(self._EQH), "H1", 2.0)
                 if lv.is_high]
        assert len(highs) == 1
        assert highs[0].equal_count == 2

    def test_cluster_price_is_the_near_side(self):
        # Price approaches highs from below, so the pool starts at the LOWER
        # of the two equal highs.
        highs = [lv for lv in find_liquidity(_series(self._EQH), "H1", 2.0)
                 if lv.is_high]
        assert highs[0].price == 3159.5

    def test_highs_beyond_tolerance_stay_separate(self):
        levels = find_liquidity(_series(self._EQH), "H1", tolerance=0.2)
        highs = sorted(
            (lv for lv in levels if lv.is_high), key=lambda lv: lv.price
        )
        assert [lv.price for lv in highs] == [3159.5, 3160.0]
        assert all(lv.equal_count == 1 for lv in highs)


class TestEqualLowsClustering:
    # A second attempt at the same area stalls 0.5 above the first low:
    # two unswept lows 0.5 apart = one EQL pool.
    _EQL = _BASE + [
        (3150.0, 3151.0, 3140.5, 3141.0),   # low pivot 3140.5
        (3141.0, 3147.0, 3140.6, 3146.0),
        (3146.0, 3150.0, 3145.0, 3149.0),
        (3149.0, 3151.0, 3148.0, 3150.0),
        (3150.0, 3152.0, 3149.0, 3151.0),
    ]

    def test_two_close_lows_form_one_cluster(self):
        lows = [lv for lv in find_liquidity(_series(self._EQL), "H1", 2.0)
                if not lv.is_high]
        assert len(lows) == 1
        assert lows[0].equal_count == 2

    def test_cluster_price_is_the_near_side(self):
        # Price approaches lows from above, so the pool starts at the HIGHER
        # of the two equal lows — a SHORT meets the highest low first.
        lows = [lv for lv in find_liquidity(_series(self._EQL), "H1", 2.0)
                if not lv.is_high]
        assert lows[0].price == 3140.5


class TestClusterSpanExceedsTolerance:
    # Three successive attempts at resistance, each stalling a bit short of
    # the last: 3163.0 -> 3161.5 -> 3160.0. Each neighbor pair is within the
    # 2.0 tolerance (1.5 apart), but the span from the lowest (3160.0) to the
    # highest (3163.0) is 3.0 > tolerance. _cluster groups from the first
    # (lowest-price) member outward, so this must split into a pair
    # (3160.0/3161.5) plus a singleton (3163.0), not one pool of 3.
    _SPAN3 = [
        (3140.0, 3142.0, 3138.0, 3141.0),
        (3141.0, 3148.0, 3140.0, 3147.0),
        (3147.0, 3163.0, 3146.0, 3161.0),   # high pivot 3163.0
        (3161.0, 3162.0, 3150.0, 3151.0),
        (3151.0, 3152.0, 3144.0, 3145.0),
        (3145.0, 3146.0, 3140.0, 3141.0),
        (3141.0, 3147.0, 3140.5, 3146.0),
        (3146.0, 3161.5, 3145.0, 3160.0),   # high pivot 3161.5
        (3160.0, 3161.0, 3150.0, 3151.0),
        (3151.0, 3152.0, 3144.0, 3145.0),
        (3145.0, 3146.0, 3140.0, 3141.0),
        (3141.0, 3147.0, 3140.5, 3146.0),
        (3146.0, 3160.0, 3145.0, 3159.0),   # high pivot 3160.0
        (3159.0, 3159.5, 3150.0, 3151.0),
        (3151.0, 3152.0, 3144.0, 3145.0),
        (3145.0, 3146.0, 3140.0, 3141.0),
        (3141.0, 3147.0, 3140.5, 3146.0),
        (3146.0, 3150.0, 3145.0, 3149.0),
    ]

    def test_span_beyond_tolerance_splits_into_pair_and_singleton(self):
        highs = sorted(
            (lv for lv in find_liquidity(_series(self._SPAN3), "H1", 2.0)
             if lv.is_high),
            key=lambda lv: lv.price,
        )
        assert len(highs) == 2
        assert highs[0].price == 3160.0 and highs[0].equal_count == 2
        assert highs[1].price == 3163.0 and highs[1].equal_count == 1


class TestNearestLiquidity:
    def _level(self, price, is_high=True, tf="H1", count=1):
        return LiquidityLevel(
            price=price, is_high=is_high, timeframe=tf,
            equal_count=count, timestamp=None,
        )

    def test_long_ignores_levels_at_or_below_entry(self):
        levels = [self._level(3100.0), self._level(3210.0), self._level(3180.0)]
        best = nearest_liquidity(levels, Direction.LONG, entry=3150.0)
        assert best.price == 3180.0

    def test_long_ignores_lows(self):
        levels = [self._level(3180.0, is_high=False), self._level(3210.0)]
        best = nearest_liquidity(levels, Direction.LONG, entry=3150.0)
        assert best.price == 3210.0

    def test_short_takes_the_highest_low_below_entry(self):
        levels = [
            self._level(3100.0, is_high=False),
            self._level(3140.0, is_high=False),
        ]
        best = nearest_liquidity(levels, Direction.SHORT, entry=3150.0)
        assert best.price == 3140.0

    def test_equal_distance_prefers_the_cluster(self):
        levels = [
            self._level(3180.0, tf="M5", count=1),
            self._level(3180.0, tf="H1", count=3),
        ]
        best = nearest_liquidity(levels, Direction.LONG, entry=3150.0)
        assert best.equal_count == 3
        assert best.timeframe == "H1"

    def test_equal_distance_and_equal_count_breaks_on_timeframe(self):
        # equal_count ties too, so this only resolves via the timeframe rank
        # (H4 > H1 > M5) — exercised on its own, not shadowed by cluster size.
        levels = [
            self._level(3180.0, tf="M5", count=2),
            self._level(3180.0, tf="H4", count=2),
            self._level(3180.0, tf="H1", count=2),
        ]
        best = nearest_liquidity(levels, Direction.LONG, entry=3150.0)
        assert best.timeframe == "H4"

    def test_no_level_beyond_entry_returns_none(self):
        levels = [self._level(3100.0)]
        assert nearest_liquidity(levels, Direction.LONG, entry=3150.0) is None
