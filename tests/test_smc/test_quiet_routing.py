"""D25 (owner decision 2026-09-05): minimal messages. In a cycle without a
completed setup NOTHING goes to Telegram — the legacy get-ready paths
(plan-zone alert, PD radar) are consulted only when their flags are raised,
and the 🔁 plan-updated message is off unless SMC_PLAN_CHANGE_ALERT."""

from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.services.smc.db import Database
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.journal import SignalJournal
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.plan import PairPlan, PlanScenario
from app.services.smc.planbook import PlanBook, PlanEntry, plan_fingerprint, plan_snapshot
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    make_candles,
)

AT = datetime(2026, 9, 7, 8, 30, tzinfo=timezone.utc)  # Monday, London block


def _watch(price=3144.0):
    """A genuine engine WATCH right under the H1 demand zone 3131-3138 —
    exactly where the old get-ready alerts used to fire."""
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    m5 = make_candles([price], step_minutes=5)
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=AT)
    r.session_name = "Frankfurt/London"
    r.price = price
    r = TripleSyncEngine(max_entry_gap_r=99.0).evaluate(h4=h4, h1=h1, m5=m5, result=r)
    r.h4_candles, r.h1_candles, r.m5_candles = h4, h1, m5
    assert r.verdict == Verdict.WATCH and r.h1_zone is not None
    return r


class _Notifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, reply_markup=None, disable_notification=False):
        self.sent.append(text)
        return len(self.sent)

    async def edit_message(self, *a, **k):
        return True

    async def send_photo(self, *a, **k):
        return 99

    async def pin(self, message_id):
        pass


def _watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    w = Watcher.__new__(Watcher)
    w.db = db
    w.state = WatcherState(db)
    w.state.pairs = ["ETHUSD"]
    w.journal = SignalJournal(db)
    w.notifier = _Notifier()
    w.news = None
    w.last_results = {}
    w.planbook = PlanBook()

    async def _no_track(*a, **k):
        return None

    w._track_journal = _no_track
    return w


@pytest.fixture(autouse=True)
def _d25_defaults(monkeypatch):
    monkeypatch.setattr(settings.smc, "zone_ping", False)
    monkeypatch.setattr(settings.smc, "pd_alert", False)
    monkeypatch.setattr(settings.smc, "plan_change_alert", False)


class TestNothingBeforeTheSetup:
    @pytest.mark.asyncio
    async def test_a_watch_cycle_sends_nothing_and_skips_the_legacy_paths(
        self, tmp_path, monkeypatch
    ):
        w = _watcher(tmp_path)
        calls = []

        async def legacy_zone(key, result):
            calls.append("zone")
            return False

        async def legacy_pd(key, result):
            calls.append("pd")

        monkeypatch.setattr(w, "_maybe_plan_zone_alert", legacy_zone)
        monkeypatch.setattr(w, "_maybe_pd_alert", legacy_pd)

        async def fake_check_pair(key):
            return "line", _watch()

        monkeypatch.setattr(w, "check_pair", fake_check_pair)
        await w.run_cycle()
        await w.run_cycle()

        assert w.notifier.sent == []
        assert calls == []
        # ... but the audit behind the button was refreshed all the same
        assert w.planbook.get("ETHUSD").audit is not None

    @pytest.mark.asyncio
    async def test_legacy_flags_re_enable_the_old_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.smc, "zone_ping", True)
        monkeypatch.setattr(settings.smc, "pd_alert", True)
        w = _watcher(tmp_path)
        calls = []

        async def legacy_zone(key, result):
            calls.append("zone")
            return False

        async def legacy_pd(key, result):
            calls.append("pd")

        monkeypatch.setattr(w, "_maybe_plan_zone_alert", legacy_zone)
        monkeypatch.setattr(w, "_maybe_pd_alert", legacy_pd)

        async def fake_check_pair(key):
            return "line", _watch()

        monkeypatch.setattr(w, "check_pair", fake_check_pair)
        await w.run_cycle()
        assert calls == ["zone", "pd"]


class TestPlanChangeMessageIsLegacy:
    @pytest.mark.asyncio
    async def test_summary_edit_no_longer_announces_by_default(self, tmp_path, monkeypatch):
        """The silent edit of the 08:05/14:05 summary keeps happening; the
        🔁 message it used to trigger is off unless SMC_PLAN_CHANGE_ALERT."""
        w = _watcher(tmp_path)
        announced = []

        async def spy(*a, **k):
            announced.append(a)

        monkeypatch.setattr(w, "_notify_plan_changes", spy)

        def plan(bottom):
            return PairPlan(
                pair="ETHUSD", price=3160.0, price_decimals=2, h4_trend=Trend.UP,
                scenarios=[PlanScenario(
                    direction=Direction.LONG, entry=bottom + 7, stop_loss=bottom - 2,
                    take_profit=bottom + 30, rr=2.0, zone_bottom=bottom,
                    zone_top=bottom + 7, speculative=False,
                )],
            )

        old = plan(3131.0)
        w.state.plan_summary = {
            "message_id": 1, "slot": "08:05",
            "date": WatcherState._prague_day(),
            "fingerprints": {"ETHUSD": plan_fingerprint(old)},
            "snapshots": {"ETHUSD": plan_snapshot(old)},
        }
        w.planbook.update("ETHUSD", PlanEntry(plan=plan(3200.0), data={}, as_of="10:00"))
        await w._maybe_edit_plan_summary()
        assert announced == []
        monkeypatch.setattr(settings.smc, "plan_change_alert", True)
        w.planbook.update("ETHUSD", PlanEntry(plan=plan(3300.0), data={}, as_of="10:05"))
        await w._maybe_edit_plan_summary()
        assert len(announced) == 1
