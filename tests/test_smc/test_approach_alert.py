"""The get-ready message (owner decision D25, 2026-09-05): price is almost at
the zone Rule 2 is waiting at. One per zone per Prague day, never more per
pair per day than the setup cap, in-session only, silenced by the 🔕 mute and
the taken cooldown, and gone once a setup has formed. It replaced the
plan-zone alert, the PD radar and the 🔁 plan-updated message, which now sit
behind default-off flags."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.smc.db import Database
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.journal import SignalJournal
from app.services.smc.models import AnalysisResult, Verdict
from app.services.smc.planbook import PlanBook
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    make_candles,
)

# Monday 2026-09-07 10:30 Prague (08:30 UTC): the London block.
AT = datetime(2026, 9, 7, 8, 30, tzinfo=timezone.utc)


def _watch(price=3144.0, at=AT):
    """A genuine engine WATCH: H4 uptrend, H1 demand zone 3131-3138, price
    above it — 'not reached yet, pullback phase' with `h1_zone` set."""
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    m5 = make_candles([price], step_minutes=5)
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=at)
    r.session_name = "Frankfurt/London"
    r.price = price
    r = TripleSyncEngine(max_entry_gap_r=99.0).evaluate(h4=h4, h1=h1, m5=m5, result=r)
    r.h4_candles, r.h1_candles, r.m5_candles = h4, h1, m5
    assert r.verdict == Verdict.WATCH and r.h1_zone is not None
    return r


class _Notifier:
    def __init__(self):
        self.sent = []  # (text, reply_markup)
        self.fail_sends = False

    async def send(self, text, reply_markup=None, disable_notification=False):
        if self.fail_sends:
            return None
        self.sent.append((text, reply_markup))
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
    monkeypatch.setattr(settings.smc, "approach_alert", True)
    monkeypatch.setattr(settings.smc, "approach_zone_factor", 1.0)
    monkeypatch.setattr(settings.smc, "max_setups_per_day", 2)
    monkeypatch.setattr(settings.smc, "zone_ping", False)
    monkeypatch.setattr(settings.smc, "pd_alert", False)


class TestApproachAlert:
    def test_fires_once_per_zone_per_day_with_the_bracket(self, tmp_path):
        w = _watcher(tmp_path)
        r = _watch()
        assert asyncio.run(w._maybe_approach_alert("ETHUSD", r)) is True
        assert asyncio.run(w._maybe_approach_alert("ETHUSD", r)) is False
        assert len(w.notifier.sent) == 1
        text, markup = w.notifier.sent[0]
        assert text.startswith("🔔 <b>ETHUSD</b>: price is $6.00 from the H1 Demand zone (OB)")
        assert "Buy Limit 3138.00 | 🛑 SL 3129.00" in text
        assert "TP1 " in text and "enter at market" in text
        assert markup["inline_keyboard"][0][0]["callback_data"].startswith("zmute_ETHUSD_")
        assert w.state.daily_count("approach", "ETHUSD", AT) == 1

    def test_a_drifted_zone_is_the_same_zone(self, tmp_path):
        w = _watcher(tmp_path)
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch()))
        r = _watch()
        r.h1_zone.bottom, r.h1_zone.top = 3132.0, 3139.0  # a tick later
        asyncio.run(w._maybe_approach_alert("ETHUSD", r))
        assert len(w.notifier.sent) == 1

    def test_far_from_the_zone_is_silent(self, tmp_path):
        w = _watcher(tmp_path)
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch(price=3160.0)))
        assert w.notifier.sent == []

    def test_inside_the_zone_says_so(self, tmp_path):
        w = _watcher(tmp_path)
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch(price=3135.0)))
        assert "price is inside the H1 Demand zone" in w.notifier.sent[0][0]

    def test_a_new_day_re_arms_the_zone(self, tmp_path):
        w = _watcher(tmp_path)
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch()))
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch(at=AT + timedelta(days=1))))
        assert len(w.notifier.sent) == 2

    def test_never_more_per_day_than_the_setup_cap(self, tmp_path):
        w = _watcher(tmp_path)
        w.state.bump_daily("approach", "ETHUSD", AT)
        w.state.bump_daily("approach", "ETHUSD", AT)
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch()))
        assert w.notifier.sent == []

    def test_off_session_is_silent(self, tmp_path):
        w = _watcher(tmp_path)
        r = _watch()
        r.session_name = None
        asyncio.run(w._maybe_approach_alert("ETHUSD", r))
        assert w.notifier.sent == []

    def test_a_formed_setup_has_its_own_message(self, tmp_path):
        w = _watcher(tmp_path)
        r = _watch()
        r.verdict = Verdict.APPROVED_LIMIT
        asyncio.run(w._maybe_approach_alert("ETHUSD", r))
        assert w.notifier.sent == []

    def test_mute_and_cooldown_silence_it(self, tmp_path):
        w = _watcher(tmp_path)
        w.state.mute_zone_alerts("ETHUSD", AT + timedelta(hours=2))
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch()))
        assert w.notifier.sent == []
        w.state.zone_muted = {}
        w.state.pair_cooldown["ETHUSD"] = (
            datetime.now(tz=timezone.utc) + timedelta(hours=2)
        ).isoformat()
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch()))
        assert w.notifier.sent == []

    def test_send_failure_retries_next_cycle(self, tmp_path):
        w = _watcher(tmp_path)
        w.notifier.fail_sends = True
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch()))
        w.notifier.fail_sends = False
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch()))
        assert len(w.notifier.sent) == 1
        assert w.state.daily_count("approach", "ETHUSD", AT) == 1

    def test_disabled_by_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.smc, "approach_alert", False)
        w = _watcher(tmp_path)
        asyncio.run(w._maybe_approach_alert("ETHUSD", _watch()))
        assert w.notifier.sent == []


class TestCycleGetReadyRouting:
    """run_cycle: the approach alert is THE get-ready message; the legacy
    plan-zone alert and PD radar stay off unless their flags are raised."""

    @pytest.mark.asyncio
    async def test_one_get_ready_message_per_cycle_and_legacy_paths_stay_quiet(
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

        approach = [t for t, _ in w.notifier.sent if t.startswith("🔔")]
        assert len(approach) == 1
        assert calls == []  # legacy paths never even consulted

    @pytest.mark.asyncio
    async def test_legacy_flags_re_enable_the_old_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.smc, "approach_alert", False)
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
        from app.services.smc.plan import PairPlan, PlanScenario
        from app.services.smc.planbook import PlanEntry, plan_fingerprint, plan_snapshot
        from app.services.smc.models import Direction, Trend

        monkeypatch.setattr(settings.smc, "plan_change_alert", False)
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
