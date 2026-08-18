"""The 5m order block / imbalance detail inside a touched zone (D5, §2.2)."""

from datetime import datetime, timezone

from app.services.smc.fvg import find_fvgs, select_valid_fvg
from app.services.smc.models import Direction, FVG, Zone
from app.services.smc.notifier import format_zone_alert
from app.services.smc.plan import PlanScenario
from app.services.smc.structure import m5_marks, zone_touch_span
from tests.test_smc.helpers import candle, m5_long_trigger

TS = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


def _two_gap_excursion():
    """One excursion into the band 100-115 holding two bullish gaps: the
    earlier 107.0-110.5 (candle 6) and the later 112.0-114.0 (candle 7).
    Price leaves the band at candle 8, so both sit inside the excursion."""
    spec = [
        (120.0, 121.0, 118.0, 119.0),    # 0 above the band
        (119.0, 120.0, 112.0, 113.0),    # 1 enters the band
        (113.0, 114.0, 108.0, 109.0),    # 2
        (109.0, 110.0, 104.0, 105.0),    # 3
        (105.0, 107.0, 103.0, 106.0),    # 4 the low of the excursion
        (106.0, 112.0, 105.5, 111.0),    # 5 reversal
        (111.0, 116.0, 110.5, 115.0),    # 6 gap over candle 4's high
        (115.0, 117.0, 114.0, 116.0),    # 7 gap over candle 5's high
        (116.0, 118.0, 115.5, 117.0),    # 8 leaves the band
        (117.0, 119.0, 116.0, 118.0),    # 9
    ]
    return [candle(*row, index=i) for i, row in enumerate(spec)]


def _band(m5):
    """A band around the pullback low the fixture actually visits."""
    bottom = min(c.low for c in m5[-8:])
    top = bottom + (max(c.high for c in m5) - bottom) * 0.5
    return bottom, top


class TestM5Marks:
    def test_finds_something_before_any_choch_exists(self):
        """§2.2: the marks must be visible while the CHoCH is still pending —
        this is the zone-alert moment, not the setup moment."""
        m5 = m5_long_trigger()[:16]  # in the zone, no CHoCH yet
        bottom, top = _band(m5)
        block, gap = m5_marks(m5, Direction.LONG, bottom, top, min_size=0.01)
        assert block is not None or gap is not None

    def test_returns_none_none_when_price_never_entered_the_band(self):
        m5 = m5_long_trigger()
        far = max(c.high for c in m5) + 100.0
        assert m5_marks(
            m5, Direction.LONG, far, far + 10.0, min_size=0.01
        ) == (None, None)

    def test_returns_none_none_on_empty_candles(self):
        assert m5_marks([], Direction.LONG, 1.0, 2.0, min_size=0.01) == (None, None)

    def test_a_found_block_is_a_demand_zone_for_a_long(self):
        m5 = m5_long_trigger()[:16]
        bottom, top = _band(m5)
        block, _gap = m5_marks(m5, Direction.LONG, bottom, top, min_size=0.01)
        assert block is not None
        assert block.is_demand is True and block.bottom < block.top

    def test_an_absurd_min_size_rejects_the_gap(self):
        m5 = m5_long_trigger()[:16]
        bottom, top = _band(m5)
        _block, gap = m5_marks(m5, Direction.LONG, bottom, top, min_size=1e9)
        assert gap is None

    def test_a_gap_formed_after_the_excursion_ended_is_not_returned(self):
        """The gap search must stay bounded at `zone_touch_span`'s `end`,
        not just its `start` — a gap that only forms once price has already
        left the band is not one the owner is waiting at on this touch, even
        though `find_fvgs`/`reversed()` would otherwise surface it first as
        the most recent candidate (task-3 review finding 2)."""
        m5 = [
            candle(110.0, 111.0, 108.0, 109.0, index=0),  # above the band
            candle(109.0, 110.0, 103.0, 104.0, index=1),  # enters the band
            candle(104.0, 106.0, 101.0, 102.0, index=2),  # still inside
            candle(107.0, 112.0, 106.0, 111.0, index=3),  # exits above — end=2
            candle(111.0, 113.0, 110.0, 112.0, index=4),
            candle(112.0, 120.0, 118.0, 119.0, index=5),  # gap forms here
        ]
        _block, gap = m5_marks(m5, Direction.LONG, 100.0, 105.0, min_size=0.5)
        assert gap is None

    def test_the_gap_is_the_one_the_engine_would_pick(self):
        """Two qualifying gaps inside one excursion: the zone alert and the
        🚨 alert must name the SAME imbalance. `select_valid_fvg` takes the
        earliest gap of the impulse because that is the best entry price;
        taking the newest here made the two messages disagree about which
        gap the owner is looking at (final-review finding 5)."""
        m5 = _two_gap_excursion()
        band_bottom, band_top = 100.0, 115.0
        start = zone_touch_span(
            m5,
            Zone(bottom=band_bottom, top=band_top, is_demand=True,
                 pivot_index=0, timestamp=m5[0].timestamp),
        )[0]
        candidates = [
            f for f in find_fvgs(m5, Direction.LONG, start) if f.size >= 1.0
        ]
        assert len(candidates) >= 2  # otherwise the test proves nothing

        _block, gap = m5_marks(
            m5, Direction.LONG, band_bottom, band_top, min_size=1.0
        )
        engine_gap = select_valid_fvg(
            m5, Direction.LONG, start, min_size=1.0, same_day_scope=True
        )
        assert gap is not None and engine_gap is not None
        assert (gap.index, gap.bottom, gap.top) == (
            engine_gap.index, engine_gap.bottom, engine_gap.top,
        )
        assert gap.index == candidates[0].index  # the earliest, not the newest


def _scenario():
    return PlanScenario(
        direction=Direction.LONG, entry=3138.0, stop_loss=3129.0,
        take_profit=3158.0, rr=2.1, zone_bottom=3131.0, zone_top=3138.0,
        speculative=False,
    )


class TestZoneAlertMarksLine:
    def test_no_marks_no_extra_line(self):
        assert "🔎" not in format_zone_alert("ETHUSD", _scenario(), 2)

    def test_empty_marks_no_extra_line(self):
        text = format_zone_alert("ETHUSD", _scenario(), 2, marks=(None, None))
        assert "🔎" not in text

    def test_block_and_gap_both_render(self):
        block = Zone(bottom=3131.0, top=3133.0, is_demand=True,
                     pivot_index=1, timestamp=TS)
        gap = FVG(index=2, bottom=3134.0, top=3136.0, is_bullish=True,
                  timestamp=TS)
        text = format_zone_alert("ETHUSD", _scenario(), 2, marks=(block, gap))
        assert "🔎" in text
        assert "5m OB 3131.00–3133.00" in text
        assert "5m FVG 3134.00–3136.00" in text

    def test_only_one_mark_renders_alone(self):
        block = Zone(bottom=3131.0, top=3133.0, is_demand=True,
                     pivot_index=1, timestamp=TS)
        text = format_zone_alert("ETHUSD", _scenario(), 2, marks=(block, None))
        assert "5m OB" in text and "5m FVG" not in text

    def test_the_base_message_is_unchanged_by_the_marks(self):
        base = format_zone_alert("ETHUSD", _scenario(), 2)
        block = Zone(bottom=3131.0, top=3133.0, is_demand=True,
                     pivot_index=1, timestamp=TS)
        with_marks = format_zone_alert(
            "ETHUSD", _scenario(), 2, marks=(block, None)
        )
        assert with_marks.startswith(base)
