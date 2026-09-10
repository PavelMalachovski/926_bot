"""The plan is primary (owner decision 2026-09-10): a /plan press is stored,
the 🚨 alert reads itself against it, and Claude's alert read sees its own
earlier plan. Detector mode is untouched — a mismatch is labelled, never
suppressed."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc import i18n
from app.services.smc.ai_read import AIRead, describe_for_ai
from app.services.smc.db import Database
from app.services.smc.instruments import get_instrument
from app.services.smc.models import Direction
from app.services.smc.notifier import format_result
from app.services.smc.planbook import (
    PlanMatch, match_primary_plan, primary_plan_snapshot,
)
from app.services.smc.state import WatcherState
from tests.test_smc.test_autoplan import TestStrategyAuditButton, _stub_watcher
from tests.test_smc.test_visuals import _approved_result


def _entry_with_read():
    entry = TestStrategyAuditButton()._audited_entry()
    entry.ai_read = AIRead(
        stance="agree", preferred_entry="deep", confidence=4,
        read="Clean pullback into the H1 demand block.", risks=["news at 14:30"],
        model="claude-sonnet-5", as_of="14:05",
    )
    return entry


class TestSnapshot:
    def test_snapshot_is_json_shaped_and_complete(self):
        snap = primary_plan_snapshot(_entry_with_read(), "2026-09-10")
        assert snap["date"] == "2026-09-10"
        assert snap["direction"] == "long"
        assert snap["zones"] and all(len(z) == 4 for z in snap["zones"])  # kind rides along
        roles = [e["role"] for e in snap["entries"]]
        assert "main" in roles
        assert snap["ai"]["stance"] == "agree"
        assert snap["ai"]["preferred_entry"] == "deep"
        assert snap["ai"]["confidence"] == 4
        import json

        json.dumps(snap)  # must survive the kv store

    def test_snapshot_without_a_read_or_audit(self):
        entry = TestStrategyAuditButton()._audited_entry()
        entry.audit = None
        snap = primary_plan_snapshot(entry, "2026-09-10")
        assert snap["ai"] is None and snap["entries"] == [] and snap["direction"] is None


class TestMatch:
    def _stored(self, direction="long", zones=None, ai=True):
        return {
            "date": "2026-09-10", "as_of": "14:05", "direction": direction,
            "zones": zones if zones is not None else [[3131.0, 3138.0, direction]],
            "entries": [
                {"role": "main", "label": "H1 Demand OB", "entry": 3138.0,
                 "stop_loss": 3128.0, "tp1": 3219.0},
                {"role": "deep", "label": "M5 OB", "entry": 3134.0,
                 "stop_loss": 3128.0, "tp1": 3219.0},
            ],
            "ai": {"stance": "agree", "preferred_entry": "deep", "confidence": 4,
                   "read": "ok", "risks": ["r1"], "model": "m"} if ai else None,
        }

    def test_no_plan_is_none(self):
        assert match_primary_plan(None, _approved_result()) is None
        assert match_primary_plan("garbage", _approved_result()) is None

    def test_same_direction_and_overlapping_zone_matches(self):
        m = match_primary_plan(self._stored(), _approved_result())
        assert m is not None and m.matches
        assert m.main == 3138.0 and m.deep == 3134.0
        assert m.ai_stance == "agree" and m.ai_entry == "deep" and m.ai_confidence == 4
        assert m.when == "10.09 14:05"

    def test_other_direction_does_not_match(self):
        m = match_primary_plan(self._stored(direction="short"), _approved_result())
        assert m is not None and not m.matches

    def test_disjoint_zone_does_not_match(self):
        m = match_primary_plan(
            self._stored(zones=[[3000.0, 3010.0, "long"]]), _approved_result()
        )
        assert m is not None and not m.matches

    def test_zone_without_direction_matches_on_overlap(self):
        m = match_primary_plan(
            self._stored(zones=[[3135.0, 3140.0, None]]), _approved_result()
        )
        assert m is not None and m.matches

    def test_malformed_row_reads_as_no_plan(self):
        assert match_primary_plan({"zones": "x", "entries": 5}, _approved_result()) is None


class TestCard:
    def test_matching_plan_line(self):
        i18n.set_language("en")
        m = PlanMatch(
            date="2026-09-10", as_of="14:05", direction="long", matches=True,
            zones=[(3131.0, 3138.0, "long")], main=3138.0, deep=3134.0,
            ai_stance="agree", ai_entry="deep", ai_confidence=4,
        )
        text = format_result(_approved_result(), in_plan=False, plan=m)
        assert (
            "📋 Per the 10.09 14:05 plan: LONG · MAIN 3138.00 · DEEP 3134.00 · "
            "Claude: AGREE 4/5, preferred DEEP"
        ) in text
        assert "not in the plan" not in text  # the plan line supersedes provenance

    def test_mismatch_line(self):
        i18n.set_language("en")
        m = PlanMatch(
            date="2026-09-10", as_of="14:05", direction="short", matches=False,
            zones=[(3200.0, 3210.0, "short")],
        )
        text = format_result(_approved_result(), plan=m)
        assert "📋 Not the 10.09 14:05 plan (SHORT 3200.00–3210.00 there)" in text

    def test_russian_plan_line(self):
        i18n.set_language("ru")
        m = PlanMatch(
            date="2026-09-10", as_of="14:05", direction="long", matches=True,
            zones=[(3131.0, 3138.0, "long")], main=3138.0, ai_stance="caution",
            ai_entry="main", ai_confidence=3,
        )
        text = format_result(_approved_result(), plan=m)
        assert "📋 По плану 10.09 14:05: LONG · MAIN 3138.00 · Claude: ОСТОРОЖНО 3/5, предпочитал MAIN" in text

    def test_no_plan_keeps_the_provenance_line(self):
        i18n.set_language("en")
        text = format_result(_approved_result(), in_plan=True, plan=None)
        assert "from this morning's plan" in text


class TestFactSheet:
    def test_plan_block_reaches_claude(self):
        m = PlanMatch(
            date="2026-09-10", as_of="14:05", direction="long", matches=True,
            main=3138.0, deep=3134.0, ai_stance="agree", ai_entry="deep",
            ai_confidence=4, ai_read="Clean pullback.", ai_risks=["news"],
        )
        facts = describe_for_ai(_approved_result(), get_instrument("ETHUSD"), plan=m)
        assert "YOUR EARLIER PLAN for this pair (10.09 14:05 Prague)" in facts
        assert "MAIN 3138.00, DEEP 3134.00" in facts
        assert "Your stance then: agree, preferred deep, confidence 4" in facts
        assert "Your read then: Clean pullback." in facts
        assert "MATCHES that plan" in facts

    def test_mismatch_is_stated(self):
        m = PlanMatch(date="2026-09-10", as_of="14:05", direction="short", matches=False)
        facts = describe_for_ai(_approved_result(), get_instrument("ETHUSD"), plan=m)
        assert "does NOT match" in facts

    def test_no_plan_no_block(self):
        facts = describe_for_ai(_approved_result(), get_instrument("ETHUSD"))
        assert "EARLIER PLAN" not in facts


class TestPersistence:
    def test_primary_plan_round_trips(self, tmp_path):
        db = Database(str(tmp_path / "smc.db"))
        state = WatcherState(db)
        state.remember_primary_plan("ethusd", {"date": "2026-09-10", "zones": []})
        reloaded = WatcherState(Database(str(tmp_path / "smc.db")))
        assert reloaded.primary_plan["ETHUSD"]["date"] == "2026-09-10"

    def test_poisoned_rows_are_dropped(self, tmp_path):
        db = Database(str(tmp_path / "smc.db"))
        db.kv_set("primary_plan", {"ETHUSD": "junk", "USDJPY": {"date": "x"}})
        assert WatcherState(db).primary_plan == {"USDJPY": {"date": "x"}}


class TestPlanPressStoresThePlan:
    @pytest.mark.asyncio
    async def test_plan_press_remembers_plan_and_zones(self, monkeypatch):
        import app.services.smc.chart as chart_mod

        monkeypatch.setattr(chart_mod, "render_plan_chart", lambda *a, **k: None)
        w = _stub_watcher()
        entry = _entry_with_read()

        async def fake_fetch(key, force_fresh=True):
            return entry

        w._fetch_pair_plan = fake_fetch
        await w._send_setup_analysis("ETHUSD", fresh=True)
        snap = w.state.primary_plan["ETHUSD"]
        assert snap["direction"] == "long" and snap["ai"]["stance"] == "agree"
        assert w.state.plan_zones["ETHUSD"]  # provenance is back since D27

    @pytest.mark.asyncio
    async def test_closed_market_plan_is_not_stored(self, monkeypatch):
        import app.services.smc.chart as chart_mod

        monkeypatch.setattr(chart_mod, "render_plan_chart", lambda *a, **k: None)
        w = _stub_watcher()
        entry = _entry_with_read()
        entry.plan.market_closed = True

        async def fake_fetch(key, force_fresh=True):
            return entry

        w._fetch_pair_plan = fake_fetch
        await w._send_setup_analysis("ETHUSD", fresh=True)
        assert w.state.primary_plan == {}


class TestAlertUsesThePlan:
    @pytest.mark.asyncio
    async def test_alert_card_carries_the_plan_line(self, tmp_path):
        from app.services.smc.journal import SignalJournal
        from smc_watcher import Watcher

        class _Notifier:
            def __init__(self):
                self.sent = []

            async def send(self, text, reply_markup=None, disable_notification=False):
                self.sent.append(text)
                return len(self.sent)

            async def send_photo(self, *a, **k):
                return None

            async def pin(self, message_id):
                pass

        db = Database(str(tmp_path / "smc.db"))
        w = Watcher.__new__(Watcher)
        w.db = db
        w.state = WatcherState(db)
        w.journal = SignalJournal(db)
        w.notifier = _Notifier()
        w.ai = None
        w.state.remember_primary_plan("ETHUSD", {
            "date": "2026-09-10", "as_of": "14:05", "direction": "long",
            "zones": [[3131.0, 3138.0, "long"]],
            "entries": [{"role": "main", "label": "x", "entry": 3138.0,
                         "stop_loss": 3128.0, "tp1": None}],
            "ai": {"stance": "agree", "preferred_entry": "main", "confidence": 5,
                   "read": "r", "risks": [], "model": "m"},
        })
        i18n.set_language("en")
        result = _approved_result()
        result.checked_at = datetime.now(tz=timezone.utc)
        sent = await w._send_alert("ETHUSD", result, "fp")
        assert sent is True
        assert "📋 Per the 10.09 14:05 plan: LONG · MAIN 3138.00 · Claude: AGREE 5/5, preferred MAIN" in w.notifier.sent[0]


class TestAuditSaysWhyTheReadIsMissing:
    @pytest.mark.asyncio
    async def test_no_key_line(self, monkeypatch):
        import app.services.smc.chart as chart_mod

        monkeypatch.setattr(chart_mod, "render_plan_chart", lambda *a, **k: None)
        i18n.set_language("en")
        w = _stub_watcher()
        entry = TestStrategyAuditButton()._audited_entry()  # no ai_read

        async def fake_fetch(key, force_fresh=True):
            return entry

        w._fetch_pair_plan = fake_fetch
        await w._send_setup_analysis("ETHUSD", fresh=True)
        text = w.notifier.sent[0][0] if isinstance(w.notifier.sent[0], tuple) else w.notifier.sent[0]
        assert "🧠 AI read is off: ANTHROPIC_API_KEY is not set" in text
        assert "📏 To the MAIN entry" in text

    @pytest.mark.asyncio
    async def test_reader_failure_reason_line(self, monkeypatch):
        import app.services.smc.chart as chart_mod
        from app.services.smc.ai_read import AIReader

        monkeypatch.setattr(chart_mod, "render_plan_chart", lambda *a, **k: None)
        i18n.set_language("en")
        w = _stub_watcher()
        w.ai = AIReader(api_key="k")
        w.ai.last_error = "max_tokens"
        entry = TestStrategyAuditButton()._audited_entry()

        async def fake_fetch(key, force_fresh=True):
            return entry

        w._fetch_pair_plan = fake_fetch
        await w._send_setup_analysis("ETHUSD", fresh=True)
        text = w.notifier.sent[0][0] if isinstance(w.notifier.sent[0], tuple) else w.notifier.sent[0]
        assert "🧠 AI read failed: max_tokens" in text


class TestPlanCancelled:
    """Owner request 2026-09-10: when the zone the /plan rests on is broken
    by a body close, say so once and tell the owner to pull the limit."""

    @staticmethod
    def _candle(ts, close, low=None, high=None):
        from app.services.smc.models import Candle

        return Candle(
            timestamp=ts, open=close, high=high if high is not None else close + 1,
            low=low if low is not None else close - 1, close=close,
        )

    @staticmethod
    def _stored(direction="long", kind="OB", date="2026-09-10", as_of="14:05", **extra):
        s = {
            "date": date, "as_of": as_of, "direction": direction,
            "zones": [[2390.0, 2396.83, direction, kind]],
            "entries": [], "ai": None,
        }
        s.update(extra)
        return s

    def _after(self, minutes):
        from app.services.smc.sessions import PRAGUE

        base = PRAGUE.localize(datetime(2026, 9, 10, 14, 5)).astimezone(timezone.utc)
        return base + timedelta(minutes=minutes)

    def test_body_close_below_a_demand_zone_breaks_it(self):
        from app.services.smc.planbook import plan_zone_break

        m5 = [
            self._candle(self._after(5), 2400.0),
            self._candle(self._after(10), 2392.0, low=2386.0),  # wick only — still fine
            self._candle(self._after(15), 2385.1),  # body close below the far edge
        ]
        broken = plan_zone_break(self._stored(), m5, [])
        assert broken is not None
        (lo, hi, direction), candle = broken
        assert (lo, hi, direction) == (2390.0, 2396.83, "long") and candle.close == 2385.1

    def test_close_before_the_plan_does_not_count(self):
        from app.services.smc.planbook import plan_zone_break

        m5 = [self._candle(self._after(-30), 2380.0), self._candle(self._after(5), 2400.0)]
        assert plan_zone_break(self._stored(), m5, []) is None

    def test_supply_zone_breaks_upward_and_range_never_breaks(self):
        from app.services.smc.planbook import plan_zone_break

        m5 = [self._candle(self._after(5), 2400.0)]
        assert plan_zone_break(self._stored("short"), m5, []) is not None
        assert plan_zone_break(self._stored("short", kind="RANGE"), m5, []) is None

    def test_h1_covers_an_older_plan(self):
        from app.services.smc.planbook import plan_zone_break

        h1 = [self._candle(self._after(120), 2380.0)]
        assert plan_zone_break(self._stored(), [], h1) is not None

    def test_cancelled_or_alerted_plans_are_left_alone(self):
        from app.services.smc.planbook import plan_zone_break

        m5 = [self._candle(self._after(5), 2380.0)]
        assert plan_zone_break(self._stored(cancelled_at="x"), m5, []) is None
        assert plan_zone_break(self._stored(alerted_at="x"), m5, []) is None
        assert plan_zone_break(None, m5, []) is None

    @pytest.mark.asyncio
    async def test_watcher_sends_once_and_stamps_the_plan(self, tmp_path):
        from smc_watcher import Watcher

        class _Notifier:
            def __init__(self):
                self.sent = []

            async def send(self, text, **kwargs):
                self.sent.append(text)
                return len(self.sent)

        db = Database(str(tmp_path / "smc.db"))
        w = Watcher.__new__(Watcher)
        w.state = WatcherState(db)
        w.notifier = _Notifier()
        w.state.remember_primary_plan("ETHUSD", self._stored())
        i18n.set_language("ru")
        result = _approved_result()
        result.m5_candles = [self._candle(self._after(15), 2385.1)]
        result.h1_candles = []
        await w._maybe_plan_cancelled("ETHUSD", result)
        await w._maybe_plan_cancelled("ETHUSD", result)
        assert len(w.notifier.sent) == 1
        text = w.notifier.sent[0]
        assert "📋 <b>План ETHUSD отменён</b>" in text
        assert "зона H1 Demand 2390.00–2396.83 пробита закрытием 2385.10" in text
        assert "Сними лимитку" in text and "/plan" in text
        reloaded = WatcherState(Database(str(tmp_path / "smc.db")))
        assert reloaded.primary_plan["ETHUSD"]["cancelled_at"]

    @pytest.mark.asyncio
    async def test_matching_alert_marks_the_plan_as_played_out(self, tmp_path):
        from app.services.smc.journal import SignalJournal
        from smc_watcher import Watcher

        class _Notifier:
            async def send(self, text, reply_markup=None, disable_notification=False):
                return 1

            async def send_photo(self, *a, **k):
                return None

            async def pin(self, message_id):
                pass

        db = Database(str(tmp_path / "smc.db"))
        w = Watcher.__new__(Watcher)
        w.db = db
        w.state = WatcherState(db)
        w.journal = SignalJournal(db)
        w.notifier = _Notifier()
        w.ai = None
        w.state.remember_primary_plan("ETHUSD", {
            "date": "2026-09-10", "as_of": "14:05", "direction": "long",
            "zones": [[3131.0, 3138.0, "long", "OB"]], "entries": [], "ai": None,
        })
        result = _approved_result()
        result.checked_at = datetime.now(tz=timezone.utc)
        assert await w._send_alert("ETHUSD", result, "fp") is True
        assert w.state.primary_plan["ETHUSD"]["alerted_at"]
