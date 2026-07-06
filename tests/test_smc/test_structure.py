"""Tests for market structure analysis (pivots, trend, zones, CHoCH)."""

from app.services.smc.models import Direction, Trend
from app.services.smc.structure import (
    detect_trend,
    find_choch,
    find_h1_zone,
    find_pivots,
    find_target_zone,
    last_protective_pivot,
    zone_touch_index,
)
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_trigger,
    make_candles,
)


class TestPivots:
    def test_finds_confirmed_swing_points(self):
        candles = make_candles([100, 105, 110, 105, 100, 105, 110, 115, 112, 110])
        pivots = find_pivots(candles)
        highs = [p for p in pivots if p.is_high]
        lows = [p for p in pivots if not p.is_high]
        assert any(p.index == 2 for p in highs)  # peak at 110
        assert any(p.index == 4 for p in lows)  # trough at 100

    def test_unconfirmed_extremum_is_not_a_pivot(self):
        # Peak at the last-but-one candle: no 2 closed candles after it.
        candles = make_candles([100, 105, 110, 115, 120, 118])
        assert all(p.index != 4 for p in find_pivots(candles))


class TestTrend:
    def test_uptrend_detected(self):
        assert detect_trend(make_candles(H4_UPTREND_CLOSES)) == Trend.UP

    def test_downtrend_detected(self):
        closes = [6300 - (c - 3000) for c in H4_UPTREND_CLOSES]  # mirrored
        assert detect_trend(make_candles(closes)) == Trend.DOWN

    def test_flat_market_detected(self):
        closes = [3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000]
        assert detect_trend(make_candles(closes)) == Trend.FLAT

    def test_choch_against_uptrend_downgrades_to_flat(self):
        # Uptrend, then a body close below the last HL (3119) breaks it.
        closes = H4_UPTREND_CLOSES + [3200, 3150, 3100, 3050]
        assert detect_trend(make_candles(closes)) == Trend.FLAT


class TestZones:
    def test_h1_demand_zone_found_untested(self):
        zone = find_h1_zone(make_candles(H1_PULLBACK_CLOSES), Direction.LONG)
        assert zone is not None
        assert zone.is_demand
        assert zone.bottom == 3131.0  # pivot candle low
        assert zone.top == 3138.0  # pivot candle body high
        assert not zone.tested and not zone.invalidated

    def test_target_supply_zone_above_entry(self):
        target = find_target_zone(
            make_candles(H1_PULLBACK_CLOSES), Direction.LONG, entry=3139.5
        )
        assert target is not None
        assert not target.is_demand
        assert target.bottom == 3200.0  # pivot candle body low

    def test_no_zone_when_structure_missing(self):
        flat = make_candles([3000] * 12)
        assert find_h1_zone(flat, Direction.LONG) is None


class TestM5Trigger:
    def test_zone_touch_and_choch(self):
        h1_zone = find_h1_zone(make_candles(H1_PULLBACK_CLOSES), Direction.LONG)
        m5 = m5_long_trigger()
        touch = zone_touch_index(m5, h1_zone)
        assert touch == 14
        choch = find_choch(m5, Direction.LONG, touch)
        assert choch == 16  # body close above the 3148 lower-high

    def test_no_choch_without_break(self):
        m5 = m5_long_trigger()[:16]  # cut off before the breaking candle
        h1_zone = find_h1_zone(make_candles(H1_PULLBACK_CLOSES), Direction.LONG)
        touch = zone_touch_index(m5, h1_zone)
        assert find_choch(m5, Direction.LONG, touch) is None

    def test_protective_pivot_for_stop_loss(self):
        m5 = m5_long_trigger()
        pivot = last_protective_pivot(m5, Direction.LONG, before_index=16)
        assert pivot is not None
        assert pivot.price == 3130.0
