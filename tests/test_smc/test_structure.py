"""Tests for market structure analysis (pivots, trend, zones, CHoCH)."""

from datetime import datetime, timezone

from app.services.smc.models import Direction, Trend, Zone
from app.services.smc.structure import (
    detect_trend,
    find_choch,
    find_h1_zone,
    find_order_block,
    find_pivots,
    h4_choch_direction,
    zone_ladder,
    zone_touch_span,
)
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    candle,
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


class TestOrderBlock:
    """The M5 order block: the last candle opposing the trade before the
    impulse that broke structure (owner definition, 2026-08-06), bounded to
    that impulse window and kept only when it really is the deeper entry."""

    LONG_SPEC = [
        (100.0, 101.0, 99.0, 100.5),   # 0 bullish
        (100.5, 101.0, 99.5, 100.0),   # 1 bearish
        (100.0, 100.5, 99.0, 100.2),   # 2 bullish
        (100.2, 100.4, 98.0, 98.5),    # 3 bearish  <- the block
        (98.5, 103.0, 98.4, 102.5),    # 4 impulse
        (102.5, 105.0, 102.0, 104.5),  # 5 impulse
    ]

    def _long(self):
        return [candle(*row, index=i) for i, row in enumerate(self.LONG_SPEC)]

    def test_long_takes_the_last_bearish_candle_wick_to_body(self):
        ob = find_order_block(
            self._long(), Direction.LONG, before_index=5, since_index=0,
            entry=102.0, stop_loss=97.5,
        )
        # demand convention matches build_zone: low -> body_high
        assert (ob.bottom, ob.top) == (98.0, 100.2)
        assert ob.is_demand is True

    def test_short_takes_the_last_bullish_candle_body_to_wick(self):
        spec = [
            (100.0, 101.0, 99.5, 100.5),
            (100.5, 101.0, 100.0, 100.2),
            (100.2, 102.5, 100.1, 102.0),   # 2 bullish  <- the block
            (102.0, 102.2, 98.0, 98.5),     # 3 impulse down
            (98.5, 98.8, 96.0, 96.5),
        ]
        m5 = [candle(*row, index=i) for i, row in enumerate(spec)]
        ob = find_order_block(
            m5, Direction.SHORT, before_index=4, since_index=0,
            entry=99.0, stop_loss=103.0,
        )
        # supply convention: body_low -> high
        assert (ob.bottom, ob.top) == (100.2, 102.5)
        assert ob.is_demand is False

    def test_none_when_no_opposing_candle_exists(self):
        spec = [(100.0 + i, 101.0 + i, 99.5 + i, 100.5 + i) for i in range(5)]
        m5 = [candle(*row, index=i) for i, row in enumerate(spec)]
        assert find_order_block(
            m5, Direction.LONG, before_index=4, since_index=0,
            entry=110.0, stop_loss=95.0,
        ) is None

    def test_none_when_the_only_opposing_candle_is_before_the_window(self):
        """The walk is floored at the zone touch. The bearish candle at index
        3 is the block; with the excursion starting at index 4 the window
        holds only impulse candles and the answer is "no block", not a
        candle from some earlier leg."""
        assert find_order_block(
            self._long(), Direction.LONG, before_index=6, since_index=4,
            entry=106.0, stop_loss=97.5,
        ) is None

    def test_candidate_that_is_not_deeper_than_the_entry_is_rejected(self):
        """The block is advertised as "← deeper entry" and its RR column is
        computed on a smaller risk. A candle whose body sits ABOVE the FVG
        entry is neither, so it is dropped rather than printed as the better
        price. (Body high 100.2 vs a 100.0 entry.)"""
        assert find_order_block(
            self._long(), Direction.LONG, before_index=5, since_index=0,
            entry=100.0, stop_loss=97.5,
        ) is None

    def test_candidate_beyond_the_stop_is_rejected(self):
        """Pathological case the ladder cannot render: an entry past the stop
        collapses `ob_risk` and turns "RR from OB" into a large positive
        nonsense number on a line the owner reads for money."""
        assert find_order_block(
            self._long(), Direction.LONG, before_index=5, since_index=0,
            entry=102.0, stop_loss=100.5,
        ) is None


class TestZoneLadder:
    """Owner confirmation 2026-08-06: the ladder shows alternative deeper
    entries — untested zones on the trade's OWN side, further out than the
    entry — not the opposite-side obstacles on the way to target."""

    # Two untested demand zones below: (95, 100) from the swing low at
    # index 6, (85, 90) from the deeper one at index 2.
    DEMAND_SPEC = [
        (110.0, 111.0, 109.0, 110.0),
        (105.0, 106.0, 104.0, 104.5),
        (90.0, 91.0, 85.0, 86.0),      # 2 swing low  -> zone (85, 90)
        (86.0, 93.0, 85.5, 92.0),
        (92.0, 99.0, 96.0, 98.0),
        (98.0, 103.0, 97.0, 102.0),
        (100.0, 101.0, 95.0, 96.0),    # 6 swing low  -> zone (95, 100)
        (96.0, 102.0, 95.6, 101.0),
        (101.0, 106.0, 100.5, 105.0),
        (105.0, 110.0, 104.0, 109.0),
        (109.0, 114.0, 108.0, 113.0),
    ]

    def _demand(self):
        return [candle(*row, index=i) for i, row in enumerate(self.DEMAND_SPEC)]

    def test_long_returns_demand_zones_below_the_entry_nearest_first(self):
        zones = zone_ladder(self._demand(), Direction.LONG, beyond=110.0)
        assert [(z.bottom, z.top) for z in zones] == [(95.0, 100.0), (85.0, 90.0)]
        assert all(z.is_demand for z in zones)

    def test_the_setups_own_zone_is_not_a_rung(self):
        """With the side flipped the live H1 entry zone qualifies by price —
        it is on this side and (for a LONG) below the FVG entry. The ladder
        is the zones OTHER than the one the setup formed in."""
        candles = self._demand()
        own = zone_ladder(candles, Direction.LONG, beyond=110.0)[0]
        assert (own.bottom, own.top) == (95.0, 100.0)
        zones = zone_ladder(candles, Direction.LONG, beyond=110.0, exclude=own)
        assert [(z.bottom, z.top) for z in zones] == [(85.0, 90.0)]

    def test_short_returns_untested_supply_above_the_entry(self):
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        zones = zone_ladder(h1, Direction.SHORT, beyond=3140.0, limit=5)
        # H1_PULLBACK_CLOSES has exactly one untested supply above 3140: the
        # pivot @ index 12 (rally to 3220, high 3221 -> zone 3200-3221). The
        # earlier pivot @ index 4 (high 3151, zone 3135-3151) is invalidated
        # by index 9's close at 3160 (body close beyond its top), so it is
        # never a candidate regardless of `beyond`.
        assert [(z.bottom, z.top) for z in zones] == [(3200.0, 3221.0)]

    def test_limit_is_respected(self):
        # H1_PULLBACK_CLOSES yields exactly one qualifying zone for SHORT at
        # any beyond below 3200 (see above) -- there is nothing further to
        # truncate here, so this pins the fixture's real (single-zone) shape
        # rather than a hypothetical multi-zone one.
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        assert len(zone_ladder(h1, Direction.SHORT, 3000.0, limit=1)) == 1

    def test_limit_truncates_to_the_nearest_zones(self):
        # A dedicated fixture with two untested, non-invalidated supply
        # zones -- (99, 104) nearer, (108.5, 110) farther -- to pin `limit`
        # as genuine truncation (N>1 -> limit), which H1_PULLBACK_CLOSES
        # cannot demonstrate (it only ever has one qualifying zone).
        spec = [
            (100.0, 101.0, 99.0, 100.5),
            (101.0, 102.0, 100.0, 101.5),
            (108.5, 110.0, 95.0, 109.5),   # peak 1 -> farther zone
            (109.0, 109.0, 104.0, 105.0),
            (105.0, 105.0, 100.0, 101.0),
            (101.0, 101.5, 97.0, 98.0),
            (98.0, 99.0, 94.0, 95.0),
            (95.0, 100.0, 94.0, 99.5),
            (99.0, 104.0, 90.0, 103.0),    # peak 2 -> nearer zone
            (103.0, 103.5, 98.0, 99.0),
            (99.0, 100.0, 95.0, 96.0),
        ]
        m5 = [candle(*row, index=i) for i, row in enumerate(spec)]
        full = zone_ladder(m5, Direction.SHORT, beyond=0.0, limit=5)
        assert [(z.bottom, z.top) for z in full] == [
            (99.0, 104.0), (108.5, 110.0)
        ]
        truncated = zone_ladder(m5, Direction.SHORT, beyond=0.0, limit=1)
        assert [(z.bottom, z.top) for z in truncated] == [(99.0, 104.0)]
