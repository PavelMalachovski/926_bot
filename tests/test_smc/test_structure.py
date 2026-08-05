"""Tests for market structure analysis (pivots, trend, zones, CHoCH)."""

from datetime import datetime, timezone

from app.services.smc.models import Direction, Trend, Zone
from app.services.smc.structure import (
    detect_trend,
    find_choch,
    find_h1_zone,
    find_pivots,
    h4_choch_direction,
    zone_touch_span,
)
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_choch_still_in_zone,
    m5_long_trigger,
    make_candles,
)


def test_find_h1_zone_untested_only_by_default():
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    zone = find_h1_zone(h1, Direction.LONG)  # max_touches=0
    assert zone is not None
    assert zone.touches == 0


def test_h4_choch_direction_none_on_clean_uptrend():
    # a confirmed HH+HL uptrend has no unreclaimed break -> no CHoCH signal
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    assert h4_choch_direction(h4) is None


def test_h4_choch_direction_short_after_break_of_last_hl():
    # uptrend then a decisive body close below the last higher-low, held
    closes = H4_UPTREND_CLOSES + [3200, 3100, 3050, 3000, 2990]
    h4 = make_candles(closes, step_minutes=240)
    assert h4_choch_direction(h4) == Direction.SHORT


def _zone(bottom, top, is_demand=True):
    return Zone(bottom=bottom, top=top, is_demand=is_demand, pivot_index=0,
                timestamp=datetime(2026, 7, 6, tzinfo=timezone.utc))


def test_zone_touch_span_returns_first_and_last_of_last_excursion():
    # closes: above zone, then 8 candles inside 3130-3140, then back above
    closes = [3150, 3145, 3135, 3134, 3133, 3136, 3138, 3137, 3135, 3134, 3160]
    candles = make_candles(closes)
    span = zone_touch_span(candles, _zone(3130, 3140))
    assert span is not None
    start, end = span
    # the excursion starts at the first candle whose range enters the zone
    assert start < end
    # all candles in [start, end] intersect the zone; start-1 does not sit fully inside
    assert candles[start].low <= 3140 and candles[start].high >= 3130


def test_zone_touch_span_none_when_never_touched():
    candles = make_candles([3200, 3205, 3210, 3208])
    assert zone_touch_span(candles, _zone(3130, 3140)) is None


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

    def test_no_zone_when_structure_missing(self):
        flat = make_candles([3000] * 12)
        assert find_h1_zone(flat, Direction.LONG) is None


class TestM5Trigger:
    def test_zone_touch_and_choch(self):
        h1_zone = find_h1_zone(make_candles(H1_PULLBACK_CLOSES), Direction.LONG)
        m5 = m5_long_trigger()
        span = zone_touch_span(m5, h1_zone)
        assert span == (8, 14)  # first-to-last of the one contiguous excursion
        touch = span[0]
        choch = find_choch(m5, Direction.LONG, touch)
        assert choch == 16  # body close above the 3148 lower-high

    def test_no_choch_without_break(self):
        m5 = m5_long_trigger()[:16]  # cut off before the breaking candle
        h1_zone = find_h1_zone(make_candles(H1_PULLBACK_CLOSES), Direction.LONG)
        touch = zone_touch_span(m5, h1_zone)[0]
        assert find_choch(m5, Direction.LONG, touch) is None

    def test_span_start_surfaces_choch_that_last_touch_hides(self):
        # Regression for the zone-touch bug: the CHoCH forms and price stays
        # inside the zone to the end of the series. span[1] is exactly what the
        # old zone_touch_index returned (the LAST in-zone candle) — using it as
        # the search origin collapses find_choch's window past the CHoCH and
        # returns None. span[0] (the excursion START) keeps the CHoCH visible.
        m5 = m5_choch_still_in_zone()
        zone = _zone(3130, 3140)
        span = zone_touch_span(m5, zone)
        assert span == (5, 10)
        # OLD behaviour (last in-zone candle == span[1]): trigger invisible.
        assert find_choch(m5, Direction.LONG, span[1]) is None
        # NEW behaviour (excursion start): CHoCH at index 8 is found.
        assert find_choch(m5, Direction.LONG, span[0]) == 8
