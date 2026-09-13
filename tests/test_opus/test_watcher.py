"""OpusWatcher end to end on stubs: the /plan flow and the five-minute
monitor — zone reached, invalidated, filled, expired, session opened —
with the event-call budget and the cooldown."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import opus_bot
from app.services.opus.analyst import OpusAnalyst
from app.services.opus.decision import parse_decision
from app.services.opus.plan import (
    EV_SESSION_OPENED, EV_ZONE_REACHED, FILLED, OPEN, PENDING, plan_from_decision,
)
from app.services.opus.store import PlanStore
from app.services.smc.db import Database
from app.services.smc.models import Candle
from tests.test_opus.helpers import (
    FakeClient, GOOD_LIMIT, GOOD_WAIT, NOW, RecordingNotifier, market_data,
)


class FakeFetcher:
    def __init__(self, data, m5):
        self.data, self.m5 = data, m5
        self.all_calls = 0
        self.m5_calls = 0

    async def fetch_all_timeframes(self, force_fresh=False):
        self.all_calls += 1
        return self.data

    async def fetch_candles(self, interval, limit):
        self.m5_calls += 1
        return self.m5


class FailingFetcher:
    async def fetch_all_timeframes(self, force_fresh=False):
        from app.core.exceptions import DataFetchError

        raise DataFetchError("Twelve Data 429 apikey=SECRET")


def freeze(monkeypatch, when: datetime):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz else when.replace(tzinfo=None)

    monkeypatch.setattr(opus_bot, "datetime", FakeDT)


def candle(minutes_after: int, o, h, l, c, base=NOW) -> Candle:  # noqa: E741
    return Candle(timestamp=base + timedelta(minutes=minutes_after), open=o, high=h, low=l, close=c)


def build(tmp_path, answers, fetcher=None, when=NOW) -> opus_bot.OpusWatcher:
    w = opus_bot.OpusWatcher.__new__(opus_bot.OpusWatcher)
    w.db = Database(str(tmp_path / "opus.db"))
    w.store = PlanStore(w.db)
    w.pairs = ["USDJPY"]
    w.notifier = RecordingNotifier()
    w.news = None
    w.analyst = OpusAnalyst(client=FakeClient(list(answers)))
    w.last_data = {}
    w._lock = asyncio.Lock()
    data = market_data(end=when)
    w._fetcher = fetcher or FakeFetcher(data, data["m5"])
    w._build_fetcher = lambda instrument: w._fetcher
    return w


def stored_plan(w, payload, when=NOW, valid_hours=3, **overrides):
    plan = plan_from_decision(
        "USDJPY", parse_decision({**payload, **overrides}), when, 147.25,
        when + timedelta(hours=valid_hours), "09.09 10:30",
    )
    w.store.put_plan(plan)
    w.store.save()
    return plan


class TestPlanFlow:
    def test_plan_sends_card_and_chart_and_stores(self, tmp_path, monkeypatch):
        freeze(monkeypatch, NOW)
        w = build(tmp_path, [GOOD_LIMIT])
        asyncio.run(w.on_plan("USDJPY"))
        assert len(w.notifier.sent) == 1 and "📍 <b>LIMIT SHORT @ 148.100</b>" in w.notifier.sent[0]
        assert len(w.notifier.photos) == 1 and w.notifier.photos[0][:4] == b"\x89PNG"
        plan = w.store.plans["USDJPY"]
        assert plan.status == PENDING and plan.message_id == 101
        assert w.store.orders_today("USDJPY", NOW) == 1
        assert w.store.minutes_since_call("USDJPY", NOW) == 0.0
        # the brief the model got carries the candles and the engine hint
        text = w.analyst._client.calls[0]["messages"][0]["content"][-1]["text"]
        assert "M5 (200 candles):" in text and "Rule-engine reference" in text
        assert "Orders already issued today for this pair: 0" in text
        # persisted
        again = PlanStore(Database(str(tmp_path / "opus.db")))
        assert again.plans["USDJPY"].entry == 148.10

    def test_market_plan_opens_a_tracked_trade(self, tmp_path, monkeypatch):
        freeze(monkeypatch, NOW)
        w = build(tmp_path, [{**GOOD_LIMIT, "action": "market", "stop_loss": 147.55,
                              "tp1": 146.60, "tp2": None, "invalidation": None}])
        asyncio.run(w.on_plan("USDJPY"))
        assert "📈 <b>ENTER AT MARKET SHORT @" in w.notifier.sent[0]
        assert len(w.store.open_trades("USDJPY")) == 1

    def test_analyst_failure_is_one_line(self, tmp_path, monkeypatch):
        freeze(monkeypatch, NOW)
        w = build(tmp_path, ["garbage"])
        asyncio.run(w.on_plan("USDJPY"))
        assert w.notifier.sent == ["🧠 <b>USDJPY</b>: Opus did not answer (unparsable). Try /plan again."]
        assert "USDJPY" not in w.store.plans

    def test_data_error_is_redacted(self, tmp_path, monkeypatch):
        freeze(monkeypatch, NOW)
        w = build(tmp_path, [GOOD_LIMIT], fetcher=FailingFetcher())
        asyncio.run(w.on_plan("USDJPY"))
        assert len(w.notifier.sent) == 1
        assert "data error" in w.notifier.sent[0] and "SECRET" not in w.notifier.sent[0]


class TestMonitor:
    def test_zone_reached_asks_opus_again(self, tmp_path, monkeypatch):
        later = NOW + timedelta(minutes=20)
        freeze(monkeypatch, later)
        w = build(tmp_path, [GOOD_LIMIT], when=later)
        stored_plan(w, GOOD_WAIT, valid_hours=8)  # zone 146.90-147.05
        w.store.last_call = {}  # the press was long ago
        w._fetcher.m5 = [candle(5, 147.2, 147.25, 147.0, 147.1)]
        asyncio.run(w.monitor_tick())
        plan = w.store.plans["USDJPY"]
        assert plan.trigger == EV_ZONE_REACHED and plan.action == "limit"
        assert w.store.event_calls_today("USDJPY", later) == 1
        assert "🔁 Re-read after: price reached the watch zone" in w.notifier.sent[0]
        brief = w.analyst._client.calls[0]["messages"][0]["content"][-1]["text"]
        assert "EVENT: price has reached the watch zone" in brief and "PREVIOUS PLAN" in brief

    def test_budget_spent_sends_the_notice_instead(self, tmp_path, monkeypatch):
        later = NOW + timedelta(minutes=20)
        freeze(monkeypatch, later)
        w = build(tmp_path, [GOOD_LIMIT], when=later)
        stored_plan(w, GOOD_WAIT, valid_hours=8)
        w.store.last_call = {}
        for _ in range(6):
            w.store.bump_event_calls("USDJPY", later)
        w._fetcher.m5 = [candle(5, 147.2, 147.25, 147.0, 147.1)]
        asyncio.run(w.monitor_tick())
        assert w.analyst._client.calls == []
        assert w.notifier.sent == [
            "👀 <b>USDJPY</b>: price reached the watch zone 146.900–147.050 (147.100). "
            "Opus event calls for today are spent (6/6) — press /plan to ask Opus."
        ]
        assert w.store.plans["USDJPY"].action == "wait"  # unchanged, fired once
        asyncio.run(w.monitor_tick())
        assert len(w.notifier.sent) == 1

    def test_cooldown_after_a_press(self, tmp_path, monkeypatch):
        later = NOW + timedelta(minutes=5)
        freeze(monkeypatch, later)
        w = build(tmp_path, [GOOD_LIMIT], when=later)
        stored_plan(w, GOOD_WAIT, valid_hours=8)
        w.store.note_call("USDJPY", NOW)
        w._fetcher.m5 = [candle(5, 147.2, 147.25, 147.0, 147.1)]
        asyncio.run(w.monitor_tick())
        assert w.analyst._client.calls == []
        assert "Opus was asked 5 min ago — press /plan" in w.notifier.sent[0]

    def test_fill_opens_a_trade_silently(self, tmp_path, monkeypatch):
        later = NOW + timedelta(minutes=20)
        freeze(monkeypatch, later)
        w = build(tmp_path, [], when=later)
        stored_plan(w, GOOD_LIMIT)  # short 148.10
        w._fetcher.m5 = [candle(5, 147.9, 148.2, 147.8, 148.0)]
        asyncio.run(w.monitor_tick())
        assert w.store.plans["USDJPY"].status == FILLED
        assert [t.status for t in w.store.trades] == [OPEN]
        assert w.notifier.sent == []
        # the trade is then tracked to TP1
        w._fetcher.m5.append(candle(10, 148.0, 148.05, 147.3, 147.4))
        asyncio.run(w.monitor_tick())
        assert w.store.trades[0].status == "tp"

    def test_expired_plan_sends_pull_the_order(self, tmp_path, monkeypatch):
        later = NOW + timedelta(hours=4)  # 14:30 Prague, NY block
        freeze(monkeypatch, later)
        w = build(tmp_path, [], when=later)
        stored_plan(w, GOOD_LIMIT)  # valid 3h
        w._fetcher.m5 = [candle(5, 147.9, 148.0, 147.8, 147.95)]
        asyncio.run(w.monitor_tick())
        assert "the limit plan expired at 14:30 Prague — pull the order" in w.notifier.sent[0]
        assert w.analyst._client.calls == []

    def test_off_session_only_the_clock_runs(self, tmp_path, monkeypatch):
        night = NOW + timedelta(hours=14)  # 00:30 Prague next day
        freeze(monkeypatch, night)
        w = build(tmp_path, [], when=night)
        stored_plan(w, GOOD_LIMIT, valid_hours=2)
        asyncio.run(w.monitor_tick())
        assert w._fetcher.m5_calls == 0
        assert "expired" in w.notifier.sent[0]

    def test_new_session_block_re_reads_a_plan_from_the_previous_one(self, tmp_path, monkeypatch):
        ny = NOW + timedelta(hours=4)  # 14:30 Prague
        freeze(monkeypatch, ny)
        w = build(tmp_path, [GOOD_WAIT], when=ny)
        stored_plan(w, GOOD_LIMIT, valid_hours=9)  # made in London, still valid
        w.store.last_call = {}
        w.store.last_block = "2026-09-09/Frankfurt-London"
        w._fetcher.m5 = [candle(5, 147.9, 148.0, 147.8, 147.95, base=ny)]
        asyncio.run(w.monitor_tick())
        plan = w.store.plans["USDJPY"]
        assert plan.trigger == EV_SESSION_OPENED and plan.action == "wait"
        assert w.store.last_block == "2026-09-09/New York"
        # a second tick in the same block does nothing
        asyncio.run(w.monitor_tick())
        assert len(w.analyst._client.calls) == 1

    def test_nothing_to_watch_costs_no_fetch(self, tmp_path, monkeypatch):
        freeze(monkeypatch, NOW)
        w = build(tmp_path, [])
        asyncio.run(w.monitor_tick())
        assert w._fetcher.m5_calls == 0 and w.notifier.sent == []


class TestValidity:
    def test_session_and_day(self):
        assert opus_bot.valid_until("session", NOW) == datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        assert opus_bot.valid_until("day", NOW) == datetime(2026, 9, 9, 16, 30, tzinfo=timezone.utc)
        night = NOW + timedelta(hours=14)
        assert opus_bot.valid_until("session", night) == datetime(2026, 9, 10, 16, 30, tzinfo=timezone.utc)

    def test_status_and_journal_texts(self, tmp_path):
        w = build(tmp_path, [])
        assert "USDJPY: no plan yet" in w.status_text()
        assert "nothing yet" in w.journal_text()
        assert "off" in w.news_text()
