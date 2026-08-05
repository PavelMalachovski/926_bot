"""Tests for the Pre-Market Plan builder, formatter and H1 chart."""

import dataclasses

from app.services.smc.chart import render_plan_chart
from app.services.smc.instruments import get_instrument
from app.services.smc.models import Direction, Trend
from app.services.smc.notifier import format_plan
from app.services.smc.plan import build_plan
from app.services.smc.profiles import AGGRESSIVE, CONSERVATIVE
from tests.test_smc.helpers import H1_PULLBACK_CLOSES, H4_UPTREND_CLOSES, make_candles

ETH = get_instrument("ETHUSD")


def _uptrend_data(price):
    """H4 uptrend, H1 with a demand zone, M5 last close = price."""
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    m5 = make_candles([price], step_minutes=5)
    return h4, h1, m5


class TestBuildPlan:
    def test_uptrend_projects_long(self):
        # price above the H1 demand zone (top 3138) so the plan is a pullback long
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        assert plan.h4_trend == Trend.UP
        assert len(plan.scenarios) == 1
        s = plan.scenarios[0]
        assert s.direction == Direction.LONG and not s.speculative
        assert s.entry == 3138.0  # demand zone top
        assert s.stop_loss < s.zone_bottom  # below the zone
        assert s.take_profit > s.entry and s.rr > 0

    def test_flat_projects_both_speculative(self):
        flat = [3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000]
        h4 = make_candles(flat, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3165.0], step_minutes=5)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        assert plan.h4_trend == Trend.FLAT
        # a demand zone below and a supply zone above -> up to two brackets
        assert all(s.speculative for s in plan.scenarios)
        dirs = {s.direction for s in plan.scenarios}
        assert Direction.LONG in dirs  # demand 3131-3138 sits below price 3165

    def test_market_closed_note(self):
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0, market_closed=True)
        assert plan.scenarios == [] and "closed" in plan.note.lower()

    def test_long_skipped_when_zone_above_price(self):
        # price below the demand zone -> not a clean pullback-long plan
        h4, h1, m5 = _uptrend_data(3120.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        assert plan.scenarios == [] and plan.note

    def test_scenario_targets_liquidity(self):
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3150, 3152, 3151, 3153])
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        assert plan.scenarios
        s = plan.scenarios[0]
        # TP is a structural level, not a multiple of the risk
        assert s.take_profit != round(s.entry + 2.5 * abs(s.entry - s.stop_loss), 2)
        assert s.rr >= 1.0

    def test_scenario_tp_is_the_exact_nearest_liquidity(self):
        # entry = H1 demand zone top = 3138.0, stop = zone bottom (3131.0) -
        # sl_buffer (2.0) = 3129.0 -> risk = 9.0. Nearest unswept liquidity
        # above entry is the H1 swing high at 3221.0 (same pool as
        # TestLiquidityTarget in test_engine.py); TP = 3221.0 - 2.0 = 3219.0.
        # rr = (3219.0 - 3138.0) / 9.0 = 81.0 / 9.0 = 9.0 exactly.
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3140, 3142, 3145])
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0, profile=CONSERVATIVE)
        assert len(plan.scenarios) == 1
        s = plan.scenarios[0]
        assert s.direction == Direction.LONG
        assert s.entry == 3138.0
        assert s.stop_loss == 3129.0
        assert s.take_profit == 3219.0
        assert s.rr == 9.0

    def test_scenario_below_threshold_is_explained(self):
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3150, 3152, 3151, 3153])
        plan = build_plan(ETH, h4, h1, m5, min_rr=99.0)
        assert plan.scenarios == []
        assert "1:" in plan.note

    def test_scenario_skipped_when_target_is_inside_the_sl_buffer(self):
        # An inflated sl_buffer pulls the target within one buffer of entry:
        # TP = 3221.0 - 100 = 3121.0, BELOW entry 3138.0 for a LONG scenario
        # -- a negative reward that abs() alone would report as a positive
        # RR. min_rr=0.1 proves the skip does not depend on the threshold.
        big_buffer = dataclasses.replace(ETH, sl_buffer=100.0)
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3160.0], step_minutes=5)
        plan = build_plan(big_buffer, h4, h1, m5, min_rr=0.1, profile=CONSERVATIVE)
        assert plan.scenarios == []
        assert "no positive reward" in plan.note


class TestFormatAndChart:
    def test_format_plan_html(self):
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        text = format_plan(plan)
        assert "Pre-Market Plan" in text and "LONG" in text
        assert "Buy Limit" in text and "SL" in text and "TP" in text
        assert "<" not in text.replace("<b>", "").replace("</b>", "")

    def test_note_only_message(self):
        h4, h1, m5 = _uptrend_data(3120.0)  # produces a note, no scenarios
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        text = format_plan(plan)
        assert "ℹ️" in text

    def test_footnote_reflects_the_liquidity_stop_reanchor(self):
        # The old footnote claimed the live alert "tightens" the SL to an M5
        # pivot; Rule 6 now anchors to the swept extreme, which can be wider
        # than the plan's preliminary zone-bottom stop. The footnote must not
        # promise a tighten, and must not still mention the M5 pivot.
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        text = format_plan(plan)
        assert "re-anchors it to the swept extreme" in text
        assert "may be wider" in text
        assert "M5 pivot" not in text
        assert "tighten" not in text.lower()
        assert "Order lives only within its session." in text

    def test_render_plan_chart_png(self):
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        png = render_plan_chart(plan, h1)
        assert png is not None and png[:4] == b"\x89PNG"

    def test_chart_none_without_scenarios(self):
        h4, h1, m5 = _uptrend_data(3120.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        assert render_plan_chart(plan, h1) is None


class TestLiquidityTpAndProfile:
    def test_aggressive_profile_takes_h4_choch_direction_when_flat(self):
        # H4 uptrend then a decisive, unreclaimed break of the last HL: trend
        # downgrades to FLAT but an aggressive trader can take the CHoCH
        # direction (SHORT) as a single non-speculative scenario.
        closes = H4_UPTREND_CLOSES + [3200, 3100, 3050, 3000, 2990]
        h4 = make_candles(closes, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3150.0], step_minutes=5)

        plan = build_plan(
            get_instrument("ETHUSD"), h4, h1, m5, min_rr=1.0, profile=AGGRESSIVE
        )
        assert plan.h4_trend == Trend.FLAT
        assert len(plan.scenarios) == 1
        s = plan.scenarios[0]
        assert s.direction == Direction.SHORT
        assert not s.speculative
        assert s.rr >= 2.0
        # SHORT: TP below entry below stop, never the LONG-side ordering.
        assert s.take_profit < s.entry < s.stop_loss

    def test_conservative_profile_ignores_h4_choch_when_flat(self):
        # same fixture, but conservative never reads the CHoCH: it falls back
        # to the flat both-direction speculative brackets instead.
        closes = H4_UPTREND_CLOSES + [3200, 3100, 3050, 3000, 2990]
        h4 = make_candles(closes, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3150.0], step_minutes=5)

        plan = build_plan(
            get_instrument("ETHUSD"), h4, h1, m5, min_rr=1.0, profile=CONSERVATIVE
        )
        assert plan.h4_trend == Trend.FLAT
        assert all(s.speculative for s in plan.scenarios)


class TestPlanKeyboard:
    def _bot(self, pairs):
        from app.services.smc.telegram_bot import TelegramCommandBot

        bot = TelegramCommandBot.__new__(TelegramCommandBot)
        bot.state = type("S", (), {"pairs": pairs})()
        return bot

    def test_keyboard_has_each_pair_and_all(self):
        bot = self._bot(["ETHUSD", "USDJPY", "GBPUSD"])
        kb = bot._plan_keyboard()["inline_keyboard"]
        datas = [b["callback_data"] for row in kb for b in row]
        assert "plan_ETHUSD" in datas and "plan_USDJPY" in datas
        assert "plan_GBPUSD" in datas and "plan_ALL" in datas

    def test_two_buttons_per_row(self):
        bot = self._bot(["ETHUSD", "USDJPY", "GBPUSD", "USDCAD"])
        kb = bot._plan_keyboard()["inline_keyboard"]
        assert len(kb[0]) == 2 and len(kb[1]) == 2  # pairs paired up
        assert kb[-1][0]["callback_data"] == "plan_ALL"
