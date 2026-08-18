"""The 5m order block / imbalance detail inside a touched zone (D5, §2.2)."""

from datetime import datetime, timezone

from app.services.smc.models import Direction, FVG, Zone
from app.services.smc.notifier import format_zone_alert
from app.services.smc.plan import PlanScenario
from app.services.smc.structure import m5_marks
from tests.test_smc.helpers import m5_long_trigger

TS = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


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
        if block is not None:
            assert block.is_demand is True and block.bottom < block.top

    def test_an_absurd_min_size_rejects_the_gap(self):
        m5 = m5_long_trigger()[:16]
        bottom, top = _band(m5)
        _block, gap = m5_marks(m5, Direction.LONG, bottom, top, min_size=1e9)
        assert gap is None


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
