"""The brief: every section the model is allowed to reason from."""

from datetime import timedelta

from app.services.opus.brief import build_brief, engine_hint, liquidity_hint
from app.services.opus.candles import candles_text
from app.services.opus.decision import parse_decision
from app.services.opus.plan import plan_from_decision
from app.services.smc.instruments import get_instrument
from app.services.smc.news import NewsEvent
from tests.test_opus.helpers import GOOD_LIMIT, NOW, market_data

JPY = get_instrument("USDJPY")


class TestCandlesText:
    def test_rows_are_prague_time_and_bounded(self):
        data = market_data()
        text = candles_text(data["m5"], 3, 3)
        rows = text.splitlines()
        assert len(rows) == 3
        assert rows[-1].startswith("09.09 10:25")  # last closed M5 before 10:30 Prague
        assert len(rows[-1].split()) == 6


class TestBrief:
    def test_sections(self):
        data = market_data()
        cpi = NewsEvent(time=NOW + timedelta(hours=4), currency="USD", title="CPI m/m")
        brief = build_brief(
            JPY, data, NOW, todays_news=[cpi], orders_today=1, min_rr=2.0,
            counts=(10, 20, 30),
        )
        assert "Pair: USDJPY (pip 0.01, prices to 3 decimals)" in brief
        assert "Now: 2026-09-09 10:30 Wednesday Prague" in brief
        assert "Session: Frankfurt/London, block ends 14:00" in brief
        assert "14:30 USD CPI m/m (no entries 13:30-14:45)" in brief
        assert "Orders already issued today for this pair: 1" in brief
        assert "Minimum RR to tp1 (hard limit): 1:2.0" in brief
        assert "Rule-engine reference" in brief and "Checklist state" in brief
        assert "Unswept liquidity (engine)" in brief
        assert "H4 (10 candles):" in brief and "M5 (30 candles):" in brief
        assert "EVENT" not in brief and "PREVIOUS PLAN" not in brief

    def test_off_session_and_blackout(self):
        sunday = NOW + timedelta(days=4)
        data = market_data(end=sunday)
        nfp = NewsEvent(time=sunday + timedelta(minutes=10), currency="USD", title="NFP")
        brief = build_brief(JPY, data, sunday, blackout=nfp, todays_news=[nfp])
        assert "Session: CLOSED" in brief
        assert "RED-NEWS BLACKOUT NOW: USD NFP" in brief and "no entries until" in brief

    def test_follow_up_carries_the_previous_plan_and_event(self):
        data = market_data()
        plan = plan_from_decision(
            "USDJPY", parse_decision(GOOD_LIMIT), NOW, 147.25, NOW + timedelta(hours=3), "09.09 10:30",
        )
        brief = build_brief(JPY, data, NOW, previous=plan, event="invalidated")
        assert "EVENT: an M5 candle closed beyond the invalidation" in brief
        assert "PREVIOUS PLAN (09.09 10:30 Prague, price then 147.250): LIMIT SHORT" in brief
        assert "entry 148.100, stop 148.400, tp1 147.400, tp2 147.000, invalidation 146.900" in brief
        assert "your read then:" in brief

    def test_engine_hint_never_raises(self):
        assert "engine reference unavailable" in engine_hint(JPY, {"h4": [], "h1": [], "m5": []}, NOW)
        assert liquidity_hint(JPY, market_data()).startswith("Unswept liquidity")
