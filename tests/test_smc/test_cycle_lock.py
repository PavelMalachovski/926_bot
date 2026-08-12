"""Regression tests for review-hardening Task 4: cycle serialization and
honest alert accounting.

1. `run_cycle` (and `on_plan`/`on_stored_plan`) run under one `asyncio.Lock`
   on the Watcher, so a scheduler tick racing `/check` (or a slow `/plan
   ALL`) can no longer interleave past the dedup fingerprint check before
   either has written `state.last_setup` — the production bug that produced
   a second alert + a second journal row for the same setup.
2. `_send_alert` must not leave an orphan `pending` journal row when
   `notifier.send` returns None (Telegram outage / rate limit): the
   just-recorded signal is discarded (memory + DB), and `run_cycle`'s
   heartbeat line says the send failed instead of claiming success.

See tests/test_smc/test_autoplan.py for the stub-watcher pattern this
reuses, and test_multipair.py::TestAlertSendIsolation for the existing
render-first/record-second contract this extends.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.services.smc.db import Database
from app.services.smc.journal import SignalJournal
from app.services.smc.models import (
    AnalysisResult,
    Direction,
    FVG,
    TradeSetup,
    Verdict,
    Zone,
)
from app.services.smc.planbook import PlanBook
from app.services.smc.state import WatcherState


def _approved_result(symbol="ETHUSD"):
    checked_at = datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc)
    result = AnalysisResult(
        symbol=symbol, verdict=Verdict.APPROVED_LIMIT, checked_at=checked_at,
    )
    result.session_name = "Frankfurt/London"
    result.h1_zone = Zone(
        bottom=100.0, top=105.0, is_demand=True, pivot_index=3,
        timestamp=checked_at,
    )
    result.setup = TradeSetup(
        direction=Direction.LONG, entry=105.0, stop_loss=95.0,
        take_profit=125.0, rr=2.0,
        fvg=FVG(9, 103.0, 105.0, True, checked_at),
    )
    return result


# --------------------------------------------------------------- (a) races


class _RaceNotifier:
    """`send` truly suspends (like the real httpx call it stands in for) so
    two concurrent run_cycle()s actually interleave around it. A notifier
    that resolved synchronously would let one cycle run start-to-finish —
    including writing the dedup fingerprint — before the other ever got a
    chance to run, which would hide the exact race this test exists to
    catch (cold-cache Twelve Data fetches take 10-60s+ in production)."""

    def __init__(self):
        self.sent = []
        self._next_id = 0

    async def send(self, text, reply_markup=None, disable_notification=False):
        await asyncio.sleep(0)
        self._next_id += 1
        self.sent.append(text)
        return self._next_id

    async def pin(self, message_id):
        pass

    async def send_photo(self, *args, **kwargs):
        return None


def _race_watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    watcher = Watcher.__new__(Watcher)
    watcher.db = db
    watcher.state = WatcherState(db)
    watcher.state.pairs = ["ETHUSD"]
    watcher.journal = SignalJournal(db)
    watcher.notifier = _RaceNotifier()
    watcher.news = None
    watcher.last_results = {}
    watcher.planbook = PlanBook()

    async def _no_chart(*args, **kwargs):
        return None

    watcher._send_chart = _no_chart

    async def _no_track(*args, **kwargs):
        return None

    watcher._track_journal = _no_track

    async def _check_pair(key):
        # Yields control before returning the SAME approved result every
        # time, exactly like the real check_pair's network fetch does — the
        # unguarded window the review found.
        await asyncio.sleep(0)
        return "setup line", _approved_result(key)

    watcher.check_pair = _check_pair
    return watcher


class TestConcurrentCyclesAreSerialized:
    """Contract item 1: one asyncio.Lock serializes run_cycle bodies."""

    @pytest.mark.asyncio
    async def test_two_concurrent_cycles_send_and_record_exactly_once(
        self, tmp_path
    ):
        watcher = _race_watcher(tmp_path)
        await asyncio.gather(watcher.run_cycle(), watcher.run_cycle())
        assert len(watcher.notifier.sent) == 1, (
            "the second cycle must see the fingerprint the first one wrote "
            "and skip, not send a duplicate alert"
        )
        assert len(watcher.journal.signals) == 1
        assert len(watcher.db.signals_all()) == 1


# ------------------------------------------------------- (b) honest sends


class _FailingNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, reply_markup=None, disable_notification=False):
        self.sent.append(text)
        return None  # simulated Telegram/network failure

    async def pin(self, message_id):
        raise AssertionError("pin must not run after a failed send")

    async def send_photo(self, *args, **kwargs):
        raise AssertionError("send_photo must not run after a failed send")


def _failing_watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    watcher = Watcher.__new__(Watcher)
    watcher.db = db
    watcher.state = WatcherState(db)
    watcher.state.pairs = ["ETHUSD"]
    watcher.journal = SignalJournal(db)
    watcher.notifier = _FailingNotifier()
    watcher.news = None
    watcher.last_results = {}
    watcher.planbook = PlanBook()

    async def _no_track(*args, **kwargs):
        return None

    watcher._track_journal = _no_track

    async def _check_pair(key):
        return "setup line", _approved_result(key)

    watcher.check_pair = _check_pair
    return watcher


class TestSendAlertDiscardsOrphanRow:
    """Contract item 2: a failed send must not leave a pending journal row
    with no message ever delivered."""

    @pytest.mark.asyncio
    async def test_send_alert_returns_false_and_discards_the_signal(
        self, tmp_path
    ):
        watcher = _failing_watcher(tmp_path)
        result = _approved_result()
        sent = await watcher._send_alert("ETHUSD", result, "fp")
        assert sent is False
        assert watcher.journal.signals == []
        assert watcher.db.signals_all() == []
        assert "ETHUSD" not in watcher.state.last_setup

    @pytest.mark.asyncio
    async def test_run_cycle_heartbeat_reports_failure_not_success(
        self, tmp_path
    ):
        watcher = _failing_watcher(tmp_path)
        summary = await watcher.run_cycle()
        assert "ETHUSD: setup found but the alert failed to send" in summary
        assert "SETUP FOUND — details above!" not in summary
        assert watcher.journal.signals == []
        assert watcher.db.signals_all() == []


# ---------------------------------------------------- (c) honest muting


class _UnreachableNotifier:
    """`send`/`pin`/`send_photo` must never run once notify_level mutes the
    real Telegram send for this tier — calling any of them is a bug, not a
    simulated outage."""

    def __init__(self):
        self.sent = []

    async def send(self, text, reply_markup=None, disable_notification=False):
        raise AssertionError("send must not run once the level mutes the alert")

    async def pin(self, message_id):
        raise AssertionError("pin must not run once the level mutes the alert")

    async def send_photo(self, *args, **kwargs):
        raise AssertionError(
            "send_photo must not run once the level mutes the alert"
        )


def _muted_watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    watcher = Watcher.__new__(Watcher)
    watcher.db = db
    watcher.state = WatcherState(db)
    watcher.state.pairs = ["ETHUSD"]
    watcher.state.set_notify_level("mute")
    watcher.journal = SignalJournal(db)
    watcher.notifier = _UnreachableNotifier()
    watcher.news = None
    watcher.last_results = {}
    watcher.planbook = PlanBook()

    async def _no_track(*args, **kwargs):
        return None

    watcher._track_journal = _no_track

    async def _check_pair(key):
        return "setup line", _approved_result(key)

    watcher.check_pair = _check_pair
    return watcher


class TestMutedWordingDiffersFromFailedWording:
    """Task 6 fix wave: a deliberate `notify_level='mute'` used to produce
    the exact same heartbeat line as a real Telegram send failure — the
    owner could not tell "I muted this" from "Telegram is down". The
    suppressed path now gets its own wording, decided from state.notify_level
    before the send is even attempted, so it can never collide with the
    real-failure wording."""

    @pytest.mark.asyncio
    async def test_run_cycle_heartbeat_reports_suppression_not_failure(
        self, tmp_path
    ):
        watcher = _muted_watcher(tmp_path)
        summary = await watcher.run_cycle()

        assert (
            "ETHUSD: setup found — alert suppressed by notification level"
            in summary
        )
        assert "alert failed to send" not in summary
        assert "SETUP FOUND — details above!" not in summary
        # unlike a real failure, the setup is still journal-recorded
        assert len(watcher.journal.signals) == 1
        assert len(watcher.db.signals_all()) == 1


# --------------------------------------------------------- lock plumbing


class TestLockCoversAllThreeEntryPoints:
    """IMPORTANT audit item: run_cycle, on_plan and on_stored_plan share the
    SAME lock instance — two different entry points racing each other must
    serialize too, not just two run_cycle()s against each other."""

    @pytest.mark.asyncio
    async def test_run_cycle_and_on_plan_never_run_concurrently(self, tmp_path):
        watcher = _race_watcher(tmp_path)
        active = []
        peak = {"n": 0}

        async def guarded_check_pair(key):
            active.append("run_cycle")
            peak["n"] = max(peak["n"], len(active))
            await asyncio.sleep(0)  # yield while "inside" the locked section
            active.remove("run_cycle")
            return "setup line", _approved_result(key)

        watcher.check_pair = guarded_check_pair

        async def guarded_send_pair_plan(key):
            active.append("on_plan")
            peak["n"] = max(peak["n"], len(active))
            await asyncio.sleep(0)
            active.remove("on_plan")

        watcher._send_pair_plan = guarded_send_pair_plan

        await asyncio.gather(watcher.run_cycle(), watcher.on_plan("ETHUSD"))
        assert peak["n"] == 1, (
            "run_cycle and on_plan bodies overlapped — they must share one "
            "lock, not run concurrently"
        )
        assert active == []
