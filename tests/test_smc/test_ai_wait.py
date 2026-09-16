"""D30 (owner decision 2026-09-16): /plan → an order or a WATCHED wait. A
"no order" answer may name one checkable event; the bot watches for it,
says ⏰ when it fires and asks Claude again at once. A resting AI order
gets a 📍 when price enters its zone and a ✅ when it fills."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc import i18n
from app.services.smc.ai_read import (
    AIProposal,
    AIRead,
    WaitFor,
    check_wait,
    describe_catalog,
    validate_proposal,
    validate_wait,
)
from app.services.smc.models import Candle
from app.services.smc.notifier import format_ai_read, wait_text
from app.services.smc.planbook import PlanBook
from tests.test_smc.test_ai_read import _StubReader, _watcher
from tests.test_smc.test_ai_setup import (
    T0,
    TestPlanbook,
    _catalog,
    _good_proposal,
    _limit,
    _Notifier,
    _read_with,
)
from tests.test_smc.test_daily import D1_NEAR


def candle(ts, o, h, l, c):  # noqa: E741
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c)


def _none_with(wait: dict):
    return {"order": "none", "direction": "none", "basis": "none", "entry": None,
            "stop": None, "target": None, "invalidation": "", "wait_for": wait}


class TestValidateWait:
    def test_a_close_below_a_listed_level_is_kept_and_snapped(self):
        result, audit, cat = _catalog()
        stop = result.setup.stop_loss
        wait, note = validate_wait(
            {"kind": "close_below", "level": stop + 0.5, "timeframe": "H1", "note": "OB gone"}, cat,
        )
        assert note == "" and wait.is_set
        assert wait.level == stop and wait.timeframe == "H1" and wait.note == "OB gone"

    def test_daily_and_range_levels_are_allowed(self):
        result, h4, h1, m5 = __import__("tests.test_smc.test_daily", fromlist=["_run"])._run(D1_NEAR)
        from app.services.smc.ai_read import build_catalog
        from app.services.smc.sniper import daily_levels

        cat = build_catalog(result, __import__("tests.test_smc.test_daily", fromlist=["ETH"]).ETH)
        pdh = daily_levels(D1_NEAR, T0)["pdh"]
        assert ("PDH", pdh) in cat.extra
        wait, note = validate_wait({"kind": "sweep_above", "level": pdh, "timeframe": "H1", "note": ""}, cat)
        assert note == "" and wait.level == pdh and wait.timeframe == "M5"  # wicks read M5
        assert "OTHER LEVELS a wait may name: " in describe_catalog(cat, 2.0)
        assert "WAIT EVENTS" in describe_catalog(cat, 2.0)

    def test_an_unlisted_level_or_a_missing_one_means_no_wait(self):
        result, audit, cat = _catalog()
        wait, note = validate_wait({"kind": "close_above", "level": 1.0, "timeframe": "M5", "note": ""}, cat)
        assert not wait.is_set and "not a listed level" in note
        wait, note = validate_wait({"kind": "close_above", "level": None, "timeframe": "M5", "note": ""}, cat)
        assert not wait.is_set and "without a level" in note

    def test_news_and_session_need_no_level(self):
        result, audit, cat = _catalog()
        wait, note = validate_wait({"kind": "news", "level": None, "timeframe": "none", "note": "CPI"}, cat)
        assert wait.is_set and wait.kind == "news" and note == ""

    def test_the_proposal_carries_the_wait_and_the_band(self):
        result, audit, cat = _catalog()
        p, note = validate_proposal(_none_with({"kind": "news", "level": None, "timeframe": "none", "note": ""}), cat)
        assert not p.is_trade and p.wait_for.kind == "news"
        trade, _ = validate_proposal(_good_proposal(result), cat)
        assert trade.band_low == result.setup.fvg.bottom and trade.band_high == result.setup.fvg.top
        again = AIProposal.from_dict(p.to_dict())
        assert again.wait_for.kind == "news"


class TestCheckWait:
    def test_close_and_sweep_kinds_on_candles_after_since(self):
        m5 = [
            candle(T0 - timedelta(minutes=10), 100, 106, 99, 105),  # before: ignored
            candle(T0, 100, 101, 98, 99),
            candle(T0 + timedelta(minutes=5), 99, 103, 98.5, 102.5),
        ]
        assert check_wait(WaitFor("close_above", 104.0, "M5"), m5, [], T0) is None
        assert "M5 closed above 102.0" in check_wait(WaitFor("close_above", 102.0, "M5"), m5, [], T0)
        assert "closed below 99.5" in check_wait(WaitFor("close_below", 99.5, "M5"), m5, [], T0)
        assert "a wick took 102.8" in check_wait(WaitFor("sweep_above", 102.8, "M5"), m5, [], T0)
        assert check_wait(WaitFor("sweep_below", 98.0, "M5"), m5, [], T0) is None
        h1 = [candle(T0, 100, 110, 90, 108)]
        assert "H1 closed above 105.0" in check_wait(WaitFor("close_above", 105.0, "H1"), [], h1, T0)

    def test_news_and_session_open(self):
        assert check_wait(WaitFor("news"), [], [], T0, now=T0, next_news=T0 + timedelta(hours=1)) is None
        assert "release has passed" in check_wait(
            WaitFor("news"), [], [], T0, now=T0 + timedelta(hours=2), next_news=T0 + timedelta(hours=1),
        )
        assert check_wait(WaitFor("session_open"), [], [], T0, session_block_now="d/NY", session_block_set="d/NY") is None
        assert "new session block" in check_wait(
            WaitFor("session_open"), [], [], T0, session_block_now="d/New-York", session_block_set="d/Frankfurt-London",
        )
        assert check_wait({"kind": "none"}, [], [], T0) is None


class TestFormat:
    def test_the_block_names_the_awaited_event(self):
        p = AIProposal(order="none", direction="none", basis="none",
                       wait_for=WaitFor("close_below", 2515.0, "H1", "OB <gone>"))
        text = format_ai_read(_read_with(p))
        assert "📐 AI setup: none — waiting for: H1 close below 2515.00 · OB &lt;gone&gt;" in text
        assert "⏰ the bot watches for it" in text
        assert wait_text(WaitFor("news")) == "the next red news release"

    def test_russian(self):
        i18n.set_language("ru")
        try:
            assert wait_text(WaitFor("sweep_above", 2527.56, "M5")) == "снятия 2527.56 фитилём сверху"
        finally:
            i18n.set_language("en")


class TestWatcher:
    def _setup(self, tmp_path, reply):
        result, entry, p = TestPlanbook()._entry()
        w = _watcher(tmp_path, _StubReader(read=reply))
        w.notifier = _Notifier()
        w.planbook = PlanBook()
        w.planbook.update("ETHUSD", entry)
        return w, result, entry

    def test_a_wait_answer_is_remembered_and_an_order_clears_it(self, tmp_path):
        w, result, entry = self._setup(tmp_path, None)
        waiting = _read_with(AIProposal(order="none", direction="none", basis="none",
                                        wait_for=WaitFor("close_below", 3128.0, "H1", "zone gone")))
        w._record_ai_order("ETHUSD", result, waiting, source="plan")
        assert w.state.ai_waits["ETHUSD"]["kind"] == "close_below"
        assert w.state.ai_waits["ETHUSD"]["expires_at"] is not None
        w._record_ai_order("ETHUSD", result, _read_with(_limit()), source="plan")
        assert "ETHUSD" not in w.state.ai_waits

    @pytest.mark.asyncio
    async def test_the_event_fires_once_and_claude_is_asked_again_now(self, tmp_path):
        against = AIRead(stance="against", preferred_entry="wait", confidence=2, read="Zone is gone.",
                         risks=[], as_of="14:05",
                         proposal=AIProposal(order="none", direction="none", basis="none"))
        w, result, entry = self._setup(tmp_path, against)
        w.state.ai_waits["ETHUSD"] = {
            "kind": "close_below", "level": 3128.0, "timeframe": "M5", "note": "",
            "set_at": T0.isoformat(), "expires_at": None, "block": None,
        }
        w.state.ai_reread_at["ETHUSD"] = datetime.now(tz=timezone.utc).isoformat()  # throttle is bypassed
        result.m5_candles = [candle(T0 + timedelta(minutes=5), 3130, 3131, 3120, 3125)]
        await w._maybe_ai_wait_event("ETHUSD", result)
        assert "ETHUSD" not in w.state.ai_waits
        assert w.notifier.sent[0].startswith("⏰ <b>ETHUSD: the event you waited for happened</b> — M5 close below 3128.00")
        assert "Zone is gone." in w.notifier.sent[1]
        facts = w.ai.facts[0][0]
        assert "RE-READ, trigger: the event you were waiting for happened: M5 closed below 3128.0" in facts
        await w._maybe_ai_wait_event("ETHUSD", result)
        assert len(w.notifier.sent) == 2

    @pytest.mark.asyncio
    async def test_an_expired_wait_is_dropped_silently(self, tmp_path):
        w, result, entry = self._setup(tmp_path, None)
        w.state.ai_waits["ETHUSD"] = {
            "kind": "news", "level": None, "timeframe": "none", "note": "",
            "set_at": T0.isoformat(), "expires_at": (T0 + timedelta(hours=1)).isoformat(), "block": None,
        }
        await w._maybe_ai_wait_event("ETHUSD", result)
        assert "ETHUSD" not in w.state.ai_waits and w.notifier.sent == []

    @pytest.mark.asyncio
    async def test_price_entering_the_zone_is_announced_once_before_the_fill(self, tmp_path):
        w, result, entry = self._setup(tmp_path, None)
        p = _limit(entry=3134.5, stop=3128.0, target=3221.0)  # a DEEP order inside 3131-3138
        p.band_low, p.band_high = 3131.0, 3138.0
        row = w.journal.record_ai("ETHUSD", p, T0, "NY")
        assert row["zone_low"] == 3131.0
        result.m5_candles = [candle(T0 + timedelta(minutes=5), 3140, 3141, 3136.5, 3137)]  # in the band, above the entry
        await w._maybe_ai_zone_reached("ETHUSD", result)
        await w._maybe_ai_zone_reached("ETHUSD", result)
        assert len(w.notifier.sent) == 1
        assert w.notifier.sent[0].startswith("📍 <b>ETHUSD: price entered the AI order's zone</b> 3131.00–3138.00")
        assert "LONG limit at 3134.50 is not filled yet" in w.notifier.sent[0]

    @pytest.mark.asyncio
    async def test_a_candle_that_touches_the_entry_is_a_fill_not_a_zone_message(self, tmp_path):
        w, result, entry = self._setup(tmp_path, None)
        p = _limit(entry=3134.5, stop=3128.0, target=3221.0)
        p.band_low, p.band_high = 3131.0, 3138.0
        # a fresh order: the journal's 5-day OPEN_TIMEOUT is judged against
        # the wall clock, and a week-old fixture would resolve as "timeout"
        recent = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15)
        w.journal.record_ai("ETHUSD", p, recent, "NY")
        touch = [candle(recent + timedelta(minutes=5), 3140, 3141, 3133.0, 3137)]
        result.m5_candles = touch
        await w._maybe_ai_zone_reached("ETHUSD", result)
        assert w.notifier.sent == []
        events = w.journal.update_pair("ETHUSD", touch)
        await w._handle_journal_events(events)
        assert len(w.notifier.sent) == 1
        assert w.notifier.sent[0].startswith("✅ <b>ETHUSD: the AI order filled</b> — LONG at 3134.50")
