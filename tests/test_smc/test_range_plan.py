"""The plan emits range scenarios, and the boundary alert names them
(spec §3.2/§3.3, D11, D12, Task 3).

D11 mirrors the engine's Rule 1 flat branch exactly: the range is only in
play when BOTH `detect_trend(h4)` and `detect_trend(h1)` read FLAT. D12: when
it is in play, the two boundary scenarios REPLACE the speculative both-way
brackets rather than joining them. There is no new alert path — a RANGE
scenario rides through the same `format_zone_alert` (and therefore the same
Delivery 1 dedup/mute machinery) as any other scenario kind.

The H1 series is the same ranging fixture `test_range_engine.py` proved out:
two clusters of two confirmed pivots each, top at 3200.8 (highs 3201.0,
3200.6), bottom at 3179.2 (lows 3179.0, 3179.4), against an ETHUSD tolerance
of 2.0 (the raw `instrument.min_fvg`). The last two highs descend and the
last two lows ascend, so `detect_trend` reads FLAT on both H4 and H1 — D11's
in-play state.
"""

from datetime import datetime, timezone

from app.services.smc.instruments import get_instrument
from app.services.smc.models import Direction, Trend, Zone
from app.services.smc.notifier import format_zone_alert, zone_alert_keyboard
from app.services.smc.plan import PlanScenario, build_plan
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    SESSION_BASE,
    candle,
    make_candles,
)

ETH = get_instrument("ETHUSD")

RANGE_H1_CLOSES = [
    3180.0, 3184.0, 3190.0, 3196.0, 3200.0, 3196.0, 3190.0, 3184.0, 3180.0,
    3184.0, 3190.0, 3196.0, 3199.6, 3196.0, 3190.0, 3184.0, 3180.4,
    3184.0, 3190.0, 3194.0,
]
RANGE_TOP = 3200.8
RANGE_BOTTOM = 3179.2

# Both H4 and H1 flat, and matching the trend detector's own flat fixture —
# used for the "flat but no range" control case.
FLAT_H4_CLOSES = [
    3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000,
]


def _range_h1():
    return make_candles(RANGE_H1_CLOSES, step_minutes=60)


def _range_h4():
    return make_candles(RANGE_H1_CLOSES, step_minutes=240)


def _h1_uptrend_over_a_range():
    """H1 trending UP while an unbroken range is still detectable — the D11
    regression: a confirmed higher high above the top cluster, wick 3201.5,
    while the last two lows still ascend."""
    extra = [
        (3194.0, 3201.5, 3193.0, 3196.0),
        (3196.0, 3197.0, 3192.0, 3193.0),
        (3193.0, 3194.0, 3190.0, 3191.0),
    ]
    return _range_h1() + [
        candle(*row, index=20 + i, start=SESSION_BASE, step_minutes=60)
        for i, row in enumerate(extra)
    ]


def _h1_ranging_swept():
    """The ranging H1 plus one closed candle whose wick pierced the top and
    whose body closed back inside (D9) — too recent to be a confirmed pivot,
    so the clusters and the FLAT trend are unchanged; only `swept_top`
    flips."""
    return _range_h1() + [
        candle(3194.0, 3204.0, 3193.0, 3196.0, index=20,
               start=SESSION_BASE, step_minutes=60)
    ]


def _build(h4=None, h1=None, price=3190.0, min_rr=1.0):
    m5 = make_candles([price], step_minutes=5)
    return build_plan(ETH, h4 or _range_h4(), h1 or _range_h1(), m5, min_rr=min_rr)


class TestRangeScenarios:
    def test_both_flat_and_unbroken_range_gives_two_scenarios(self):
        plan = build_plan(ETH, _range_h4(), _range_h1(), make_candles([3190.0]))
        assert plan.h4_trend == Trend.FLAT
        assert len(plan.scenarios) == 2
        assert all(s.kind == "RANGE" for s in plan.scenarios)
        dirs = {s.direction for s in plan.scenarios}
        assert dirs == {Direction.LONG, Direction.SHORT}

    def test_neither_range_scenario_is_speculative(self):
        """D12: the range replaces the speculative both-way brackets — it
        does not join them, so no scenario in play may still be
        speculative."""
        plan = _build()
        assert plan.scenarios
        assert all(not s.speculative for s in plan.scenarios)

    def test_both_flat_no_range_keeps_todays_speculative_brackets(self):
        """Same FLAT/FLAT state, but no cluster clears the two-touch/3x
        floor — today's both-way brackets must still appear unchanged."""
        h4 = make_candles(FLAT_H4_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3165.0], step_minutes=5)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        assert plan.h4_trend == Trend.FLAT
        assert plan.scenarios
        assert all(s.speculative for s in plan.scenarios)
        assert all(s.kind != "RANGE" for s in plan.scenarios)

    def test_h4_flat_h1_trending_keeps_the_h1_scenario_no_range(self):
        """D11: a trending H1 wins even though a range is still detectable
        underneath it — no existing H1-trend signal may be replaced."""
        plan = _build(h1=_h1_uptrend_over_a_range(), price=3190.0)
        assert plan.direction_note == "H4 flat — direction from H1 uptrend"
        assert len(plan.scenarios) == 1
        s = plan.scenarios[0]
        assert s.direction == Direction.LONG
        assert s.kind != "RANGE"
        assert not any(sc.kind == "RANGE" for sc in plan.scenarios)

    def test_short_boundary_geometry(self):
        plan = _build()
        short = next(s for s in plan.scenarios if s.direction == Direction.SHORT)
        assert short.entry == RANGE_TOP
        assert short.stop_loss == RANGE_TOP + ETH.sl_buffer
        assert short.take_profit == RANGE_BOTTOM + ETH.sl_buffer
        assert short.zone_top == RANGE_TOP
        assert short.zone_bottom == RANGE_TOP - ETH.min_fvg
        risk = short.stop_loss - short.entry
        reward = short.entry - short.take_profit
        assert short.rr == round(reward / risk, 2)

    def test_long_boundary_geometry(self):
        plan = _build()
        long_ = next(s for s in plan.scenarios if s.direction == Direction.LONG)
        assert long_.entry == RANGE_BOTTOM
        assert long_.stop_loss == RANGE_BOTTOM - ETH.sl_buffer
        assert long_.take_profit == RANGE_TOP - ETH.sl_buffer
        assert long_.zone_bottom == RANGE_BOTTOM
        assert long_.zone_top == RANGE_BOTTOM + ETH.min_fvg
        risk = long_.entry - long_.stop_loss
        reward = long_.take_profit - long_.entry
        assert long_.rr == round(reward / risk, 2)

    def test_market_range_still_replaces_choch_for_aggressive_profile(self):
        from app.services.smc.profiles import AGGRESSIVE

        plan = build_plan(
            ETH, _range_h4(), _range_h1(), make_candles([3190.0]),
            min_rr=1.0, profile=AGGRESSIVE,
        )
        assert len(plan.scenarios) == 2
        assert all(s.kind == "RANGE" for s in plan.scenarios)


class TestRangeSweptField:
    """Range.swept_top/swept_bottom (D9) are now surfaced on the plan
    message rather than sitting unread — a boundary already raided once is
    worth a note, since the pool behind it may be thinner than a fresh one."""

    def test_swept_boundary_is_flagged_on_its_scenario(self):
        plan = _build(h1=_h1_ranging_swept())
        short = next(s for s in plan.scenarios if s.direction == Direction.SHORT)
        long_ = next(s for s in plan.scenarios if s.direction == Direction.LONG)
        assert short.swept is True
        assert long_.swept is False

    def test_unswept_boundaries_are_not_flagged(self):
        plan = _build()
        assert all(not s.swept for s in plan.scenarios)

    def test_format_plan_notes_a_swept_boundary(self):
        # Distinct from the footer's unrelated "swept extreme" phrase
        # (Rule 6's re-anchor note), which is present on every plan with a
        # scenario regardless of this feature.
        from app.services.smc.notifier import format_plan

        plan = _build(h1=_h1_ranging_swept())
        text = format_plan(plan)
        assert "already been swept" in text.lower()

    def test_format_plan_is_silent_when_nothing_was_swept(self):
        from app.services.smc.notifier import format_plan

        plan = _build()
        text = format_plan(plan)
        assert "already been swept" not in text.lower()


class TestFormatZoneAlertRange:
    def _scenario(self, direction, swept=False):
        if direction == Direction.SHORT:
            return PlanScenario(
                direction=Direction.SHORT,
                entry=159.478,
                stop_loss=159.55,
                take_profit=158.687,
                rr=2.3,
                zone_bottom=159.278,
                zone_top=159.478,
                speculative=False,
                kind="RANGE",
                swept=swept,
            )
        return PlanScenario(
            direction=Direction.LONG,
            entry=158.687,
            stop_loss=158.6,
            take_profit=159.478,
            rr=2.3,
            zone_bottom=158.687,
            zone_top=158.887,
            speculative=False,
            kind="RANGE",
            swept=swept,
        )

    def test_names_the_boundary_and_the_opposite_boundary(self):
        text = format_zone_alert("USDJPY", self._scenario(Direction.SHORT), 3)
        assert "price is at the range HIGH 159.478" in text
        assert "target the range LOW 158.687" in text
        assert "SL 159.550" in text
        assert "Watching M5 for a bearish CHoCH + FVG." in text
        # Not the OB/FVG wording at all.
        assert "zone" not in text.lower()

    def test_long_names_the_boundary_and_the_opposite_boundary(self):
        text = format_zone_alert("USDJPY", self._scenario(Direction.LONG), 3)
        assert "price is at the range LOW 158.687" in text
        assert "target the range HIGH 159.478" in text
        assert "Watching M5 for a bullish CHoCH + FVG." in text

    def test_still_carries_the_mute_keyboard(self):
        """The whole point of Task 3: a RANGE scenario rides the existing
        zone-alert path, so it gets the same 🔕 keyboard for free."""
        kb = zone_alert_keyboard("USDJPY", "14:00", "block1")
        assert "🔕" in kb["inline_keyboard"][0][0]["text"]
        assert kb["inline_keyboard"][0][0]["callback_data"] == "zmute_USDJPY_block1"

    def test_pair_is_escaped(self):
        text = format_zone_alert(
            "US<D>JPY", self._scenario(Direction.SHORT), 3,
        )
        assert "<D>" not in text
        assert "&lt;D&gt;" in text


class TestFormatZoneAlertPinnedForNonRange:
    """format_zone_alert on an OB/FVG scenario must stay byte-identical to
    before Task 3 — proof the RANGE branch didn't disturb it."""

    def _scenario(self):
        return PlanScenario(
            direction=Direction.LONG,
            entry=3138.0,
            stop_loss=3129.0,
            take_profit=3219.0,
            rr=9.0,
            zone_bottom=3131.0,
            zone_top=3138.0,
            speculative=False,
            kind="OB",
        )

    def test_pinned_text(self):
        text = format_zone_alert("ETHUSD", self._scenario(), 2)
        assert text == (
            "🔔 <b>ETHUSD</b>: price reached the Demand zone 3131.00–3138.00\n"
            "📋 Plan: LONG — Buy Limit 3138.00 | 🛑 SL 3129.00 "
            "| 🎯 TP 3219.00 | ~1:9.0\n"
            "Watching M5 for a bullish CHoCH + FVG."
        )

    def test_pinned_text_with_marks(self):
        ts = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
        block = Zone(
            bottom=3132.0, top=3136.0, is_demand=True, pivot_index=0,
            timestamp=ts, kind="OB",
        )
        gap = Zone(
            bottom=3133.0, top=3135.0, is_demand=True, pivot_index=0,
            timestamp=ts, kind="FVG",
        )
        text = format_zone_alert(
            "ETHUSD", self._scenario(), 2, marks=(block, gap),
        )
        assert text.endswith(
            "🔎 5m OB 3132.00–3136.00 · 5m FVG 3133.00–3135.00"
        )
