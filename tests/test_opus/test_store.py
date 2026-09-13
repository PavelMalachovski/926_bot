"""Persistence on the kv store: plans, trades, history, the call budget."""

from datetime import timedelta

from app.services.opus.decision import parse_decision
from app.services.opus.plan import REPLACED, plan_from_decision, trade_from_plan
from app.services.opus.store import PlanStore
from app.services.smc.db import Database
from tests.test_opus.helpers import GOOD_LIMIT, GOOD_WAIT, NOW


def plan(payload, when=NOW):
    return plan_from_decision("USDJPY", parse_decision(payload), when, 147.25,
                              when + timedelta(hours=3), "09.09 10:30")


class TestStore:
    def test_roundtrip(self, tmp_path):
        db = Database(str(tmp_path / "opus.db"))
        store = PlanStore(db)
        first = plan(GOOD_LIMIT)
        assert store.put_plan(first) is None
        store.add_trade(trade_from_plan(first, NOW, "limit"))
        store.bump_event_calls("USDJPY", NOW)
        store.note_call("USDJPY", NOW)
        store.last_block = "2026-09-09/Frankfurt-London"
        store.save()

        again = PlanStore(Database(str(tmp_path / "opus.db")))
        assert again.plans["USDJPY"] == first
        assert len(again.trades) == 1 and again.trades[0].pair == "USDJPY"
        assert again.orders_today("USDJPY", NOW) == 1
        assert again.event_calls_today("USDJPY", NOW) == 1
        assert again.minutes_since_call("USDJPY", NOW + timedelta(minutes=7)) == 7.0
        assert again.last_block == "2026-09-09/Frankfurt-London"

    def test_new_plan_replaces_a_live_one(self, tmp_path):
        store = PlanStore(Database(str(tmp_path / "opus.db")))
        first = plan(GOOD_LIMIT)
        store.put_plan(first)
        previous = store.put_plan(plan(GOOD_WAIT, NOW + timedelta(minutes=30)))
        assert previous is first and first.status == REPLACED
        assert store.orders_today("USDJPY", NOW) == 1  # a wait is not an order

    def test_daily_counters_roll(self, tmp_path):
        store = PlanStore(Database(str(tmp_path / "opus.db")))
        store.bump_event_calls("USDJPY", NOW)
        tomorrow = NOW + timedelta(days=1)
        assert store.event_calls_today("USDJPY", tomorrow) == 0
        store.bump_event_calls("USDJPY", tomorrow)
        assert list(store.event_calls) == ["2026-09-10:USDJPY"]

    def test_duplicate_trade_ignored(self, tmp_path):
        store = PlanStore(Database(str(tmp_path / "opus.db")))
        p = plan(GOOD_LIMIT)
        store.add_trade(trade_from_plan(p, NOW, "limit"))
        store.add_trade(trade_from_plan(p, NOW, "limit"))
        assert len(store.trades) == 1
