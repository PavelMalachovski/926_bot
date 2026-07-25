"""Tests for the Pre-Market Plan builder, formatter and H1 chart."""

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
        plan = build_plan(ETH, h4, h1, m5, min_rr=2.0)
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
        plan = build_plan(ETH, h4, h1, m5, min_rr=2.0)
        assert plan.h4_trend == Trend.FLAT
        # a demand zone below and a supply zone above -> up to two brackets
        assert all(s.speculative for s in plan.scenarios)
        dirs = {s.direction for s in plan.scenarios}
        assert Direction.LONG in dirs  # demand 3131-3138 sits below price 3165

    def test_market_closed_note(self):
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=2.0, market_closed=True)
        assert plan.scenarios == [] and "closed" in plan.note.lower()

    def test_long_skipped_when_zone_above_price(self):
        # price below the demand zone -> not a clean pullback-long plan
        h4, h1, m5 = _uptrend_data(3120.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=2.0)
        assert plan.scenarios == [] and plan.note


class TestFormatAndChart:
    def test_format_plan_html(self):
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=2.0)
        text = format_plan(plan, min_rr=2.0)
        assert "Pre-Market Plan" in text and "LONG" in text
        assert "Buy Limit" in text and "SL" in text and "TP" in text
        assert "<" not in text.replace("<b>", "").replace("</b>", "")

    def test_no_qualifying_scenario_shows_reason_note(self):
        # min_rr so high that even the uptrend pullback zone can't reach it:
        # build_plan drops the scenario and format_plan renders the reason.
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=99.0)  # force the RR filter
        assert plan.scenarios == []
        text = format_plan(plan, min_rr=99.0)
        assert "ℹ️" in text

    def test_note_only_message(self):
        h4, h1, m5 = _uptrend_data(3120.0)  # produces a note, no scenarios
        plan = build_plan(ETH, h4, h1, m5, min_rr=2.0)
        text = format_plan(plan)
        assert "ℹ️" in text

    def test_render_plan_chart_png(self):
        h4, h1, m5 = _uptrend_data(3160.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=2.0)
        png = render_plan_chart(plan, h1)
        assert png is not None and png[:4] == b"\x89PNG"

    def test_chart_none_without_scenarios(self):
        h4, h1, m5 = _uptrend_data(3120.0)
        plan = build_plan(ETH, h4, h1, m5, min_rr=2.0)
        assert render_plan_chart(plan, h1) is None


class TestRRFilterAndProfile:
    def test_plan_skips_scenario_below_min_rr_with_reason(self):
        # NOTE: the brief's original fixture used m5=[3140, 3138, 3136] (last
        # close 3136), which sits BELOW the H1 demand zone top (3138). That
        # makes _scenario's "zone.top >= price" guard fire before the RR walk
        # ever runs, so build_plan falls back to the generic
        # "No clean zone with RR >= 1:2 yet" note -- which happens to also
        # contain "1:" and would pass the assertion for the wrong reason.
        # Using an ascending m5 (last close 3145, above the zone) makes the
        # zone live so _scenario actually walks targets and the note states
        # the real achievable RR (see hand-trace in task-7-report.md).
        inst = get_instrument("ETHUSD")
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3140, 3142, 3145])
        plan = build_plan(inst, h4, h1, m5, min_rr=99.0, profile=CONSERVATIVE)
        assert plan.scenarios == []
        assert plan.note is not None
        assert "live" in plan.note and "1:" in plan.note  # achievable RR stated

    def test_plan_scenario_meets_min_rr_when_target_exists(self):
        inst = get_instrument("ETHUSD")
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3140, 3142, 3145])
        plan = build_plan(inst, h4, h1, m5, min_rr=1.5, profile=CONSERVATIVE)
        assert plan.scenarios  # a target reaching RR 1.5 exists for this fixture
        for s in plan.scenarios:
            assert s.rr >= 1.5

    def test_aggressive_profile_takes_h4_choch_direction_when_flat(self):
        # H4 uptrend then a decisive, unreclaimed break of the last HL: trend
        # downgrades to FLAT but an aggressive trader can take the CHoCH
        # direction (SHORT) as a single non-speculative scenario.
        closes = H4_UPTREND_CLOSES + [3200, 3100, 3050, 3000, 2990]
        h4 = make_candles(closes, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3150.0], step_minutes=5)

        plan = build_plan(
            get_instrument("ETHUSD"), h4, h1, m5, min_rr=2.0, profile=AGGRESSIVE
        )
        assert plan.h4_trend == Trend.FLAT
        assert len(plan.scenarios) == 1
        s = plan.scenarios[0]
        assert s.direction == Direction.SHORT
        assert not s.speculative
        assert s.rr >= 2.0

    def test_conservative_profile_ignores_h4_choch_when_flat(self):
        # same fixture, but conservative never reads the CHoCH: it falls back
        # to the flat both-direction speculative brackets instead.
        closes = H4_UPTREND_CLOSES + [3200, 3100, 3050, 3000, 2990]
        h4 = make_candles(closes, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3150.0], step_minutes=5)

        plan = build_plan(
            get_instrument("ETHUSD"), h4, h1, m5, min_rr=2.0, profile=CONSERVATIVE
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
