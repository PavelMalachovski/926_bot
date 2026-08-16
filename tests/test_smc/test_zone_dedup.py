"""One zone alert per session block, plus the mute gate (spec §1.3, §1.4).

The regression under test is USDCAD 2026-08-13: the same zone alerted at
16:20, 16:35, 16:55 and 17:25 Prague because the old rule re-armed on every
exit from the zone.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.plan import PairPlan, PlanScenario
from app.services.smc.planbook import PlanBook, PlanEntry
from app.services.smc.sessions import PRAGUE
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import make_candles


def _utc(hh, mm, day=16):
    return PRAGUE.localize(
        datetime(2026, 8, day, hh, mm)
    ).astimezone(timezone.utc)


def _scenario(direction=Direction.LONG, bottom=3131.0, top=3138.0):
    entry = top if direction == Direction.LONG else bottom
    return PlanScenario(
        direction=direction, entry=entry, stop_loss=bottom - 2.0,
        take_profit=top + 20.0, rr=2.1, zone_bottom=bottom, zone_top=top,
        speculative=False,
    )


def _entry(scenarios=None):
    plan = PairPlan(pair="ETHUSD", price=3160.0, price_decimals=2,
                    h4_trend=Trend.UP, scenarios=scenarios or [_scenario()])
    return PlanEntry(plan=plan, data={"h4": [], "h1": [], "m5": []},
                     as_of="09:00")


class _State:
    zone_already_pinged = WatcherState.zone_already_pinged
    remember_zone_ping = WatcherState.remember_zone_ping
    zone_muted_until = WatcherState.zone_muted_until
    mute_zone_alerts = WatcherState.mute_zone_alerts

    def __init__(self):
        self.zone_pinged = {}
        self.zone_muted = {}
        self.pair_cooldown = {}
        self.pair_profile = {}

    def save(self):
        pass


class _Notifier:
    def __init__(self):
        self.sent = []
        self.fail_sends = False

    async def send(self, text, reply_markup=None, disable_notification=False):
        if self.fail_sends:
            return None
        self.sent.append((text, reply_markup))
        return len(self.sent)


def _watcher(monkeypatch):
    from smc_watcher import Watcher

    monkeypatch.setattr(settings.smc, "zone_ping", True)
    w = Watcher.__new__(Watcher)
    w.state = _State()
    w.notifier = _Notifier()
    w.planbook = PlanBook()
    w.planbook.update("ETHUSD", _entry())
    return w


def _result(price=3137.0, at=None, verdict=Verdict.WATCH):
    r = AnalysisResult(symbol="ETHUSD", verdict=verdict,
                       checked_at=at or _utc(9, 0), price_decimals=2)
    r.session_name = "Frankfurt/London"
    r.m5_candles = make_candles([price], step_minutes=5)
    return r


class TestOneAlertPerBlock:
    def test_first_touch_alerts(self, monkeypatch):
        w = _watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        assert len(w.notifier.sent) == 1

    def test_exit_and_reentry_within_the_block_is_silent(self, monkeypatch):
        """The USDCAD 2026-08-13 regression."""
        w = _watcher(monkeypatch)
        for minute, price in ((20, 3137.0), (25, 3160.0), (35, 3137.0),
                              (55, 3160.0), (85, 3137.0)):
            asyncio.run(w._maybe_plan_zone_alert(
                "ETHUSD", _result(price=price, at=_utc(9, 0) + timedelta(minutes=minute))
            ))
        assert len(w.notifier.sent) == 1

    def test_drifting_bounds_stay_silent(self, monkeypatch):
        w = _watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        w.planbook.update("ETHUSD", _entry([_scenario(bottom=3131.5, top=3138.5)]))
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        assert len(w.notifier.sent) == 1

    def test_new_block_allows_one_more(self, monkeypatch):
        w = _watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result(at=_utc(13, 55))))
        ny = _result(at=_utc(14, 5))
        ny.session_name = "New York"
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", ny))
        assert len(w.notifier.sent) == 2

    def test_non_overlapping_zone_alerts_again(self, monkeypatch):
        w = _watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        w.planbook.update("ETHUSD", _entry([_scenario(bottom=3200.0, top=3210.0)]))
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result(price=3205.0)))
        assert len(w.notifier.sent) == 2

    def test_send_failure_retries_next_cycle(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.notifier.fail_sends = True
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        w.notifier.fail_sends = False
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        assert len(w.notifier.sent) == 1


class TestMuteGate:
    def test_muted_pair_gets_no_zone_alert(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result(at=_utc(9, 30))))
        assert w.notifier.sent == []

    def test_expired_mute_lets_the_alert_through(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        ny = _result(at=_utc(14, 5))
        ny.session_name = "New York"
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", ny))
        assert len(w.notifier.sent) == 1

    def test_mute_of_one_pair_does_not_silence_another(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.planbook.update("USDCAD", _entry())
        w.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        asyncio.run(w._maybe_plan_zone_alert("USDCAD", _result(at=_utc(9, 30))))
        assert len(w.notifier.sent) == 1


class TestMuteDoesNotAffectOtherAlerts:
    """D3: the 🔕 button silences this pair's ZONE alerts only. 🚨 setup
    alerts and Rule 0.4 news warnings always pass, muted or not — the
    promise that makes the button safe on a detector-mode bot where "once a
    setup forms, the alert always fires" (CLAUDE.md). This exercises the
    real `_send_alert` / `_rule_04_warnings` code paths, not a mock, using
    the same real-`WatcherState` harness as
    `test_multipair.py::TestNotifyLevelGating` and the real-`Watcher`
    harness as `test_autoplan.py` / `test_poison_state.py`.
    """

    class _RecordingNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, text, reply_markup=None, disable_notification=False):
            self.sent.append((text, reply_markup))
            return len(self.sent)

        async def send_photo(self, photo, caption=None, reply_to=None):
            return 999

        async def pin(self, message_id):
            pass

    def _watcher(self, tmp_path):
        from app.services.smc.db import Database
        from app.services.smc.journal import SignalJournal
        from smc_watcher import Watcher

        db = Database(str(tmp_path / "smc.db"))
        watcher = Watcher.__new__(Watcher)
        watcher.db = db
        watcher.state = WatcherState(db)
        watcher.journal = SignalJournal(db)
        watcher.notifier = self._RecordingNotifier()
        return watcher

    def test_muted_pair_still_gets_its_setup_alert(self, tmp_path):
        from tests.test_smc.test_multipair import _fingerprint_result

        watcher = self._watcher(tmp_path)
        watcher.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        result = _fingerprint_result()
        result.setup.tier_star = True
        result.setup.tp1 = result.setup.entry + 20
        result.setup.runner_tp = result.setup.entry + 40

        sent = asyncio.run(watcher._send_alert("ETHUSD", result, "fp-star"))

        assert sent is True
        assert len(watcher.notifier.sent) == 1

    def test_muted_pair_still_gets_its_rule_04_warning(self, tmp_path):
        from app.services.smc.news import NewsCalendar, NewsEvent

        watcher = self._watcher(tmp_path)
        watcher.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        watcher.news = NewsCalendar()
        event_time = datetime.now(tz=timezone.utc) + timedelta(minutes=15)
        watcher.news.events = [
            NewsEvent(time=event_time, currency="USD", title="Fed Speech")
        ]
        watcher.journal.signals.append(
            {"id": "sig1", "pair": "ETHUSD", "status": "open"}
        )

        asyncio.run(watcher._rule_04_warnings())

        assert watcher.notifier.sent, (
            "a muted pair must still get its Rule 0.4 warning"
        )
        message = watcher.notifier.sent[0][0]
        assert "ETHUSD" in message
        assert "RULE 0.4" in message
