"""Regression tests for review-hardening Task 5: poison-pill timestamps and
read-path purity.

Legacy JSON imports (see db.py's migrate_legacy_json) can carry naive
timestamps (no tzinfo — raises TypeError on the first aware-vs-naive
comparison) or outright unparseable garbage (raises ValueError). Before this
fix, one such value in `state.news_warned` or a journal row's
created_at/checked_until/expires_at would raise out of `_rule_04_warnings`
or `journal.update_pair` and kill every future cycle, forever — resolving
the poisoned row is exactly the code path that crashed.

Separately, `_cooldown_left` used to mutate `state.pair_cooldown` and call
`state.save()` (a DB write) from a pure-looking read — including from
`status_text()`, so a /status command performed a DB write. Cleanup now
lives in one place: `_purge_expired_cooldowns()`, called once per cycle
from `run_cycle`.

See tests/test_smc/test_autoplan.py / test_cycle_lock.py for the
stub-watcher patterns this reuses.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc.db import Database
from app.services.smc.journal import SignalJournal
from app.services.smc.state import WatcherState


# ------------------------------------------------------ (a) news_warned prune


def _rule04_watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    watcher = Watcher.__new__(Watcher)
    watcher.db = db
    watcher.state = WatcherState(db)
    watcher.journal = SignalJournal(db)  # no signals -> loop body never runs
    watcher.news = None
    return watcher


class TestRuleO4WarningsPruneSurvivesPoison:
    @pytest.mark.asyncio
    async def test_garbage_and_naive_entries_dropped_valid_survives(
        self, tmp_path
    ):
        watcher = _rule04_watcher(tmp_path)
        valid_aware = datetime.now(tz=timezone.utc).isoformat()
        watcher.state.news_warned = {
            "garbage": "not-a-date",
            "naive": "2026-08-11T00:00:00",  # no tzinfo
            "valid": valid_aware,
        }

        await watcher._rule_04_warnings()  # must not raise

        assert watcher.state.news_warned == {"valid": valid_aware}

    @pytest.mark.asyncio
    async def test_still_prunes_stale_aware_entries(self, tmp_path):
        watcher = _rule04_watcher(tmp_path)
        stale = (
            datetime.now(tz=timezone.utc) - timedelta(days=3)
        ).isoformat()
        fresh = datetime.now(tz=timezone.utc).isoformat()
        watcher.state.news_warned = {"stale": stale, "fresh": fresh}

        await watcher._rule_04_warnings()

        assert watcher.state.news_warned == {"fresh": fresh}

    @pytest.mark.asyncio
    async def test_save_skipped_when_nothing_changed(self, tmp_path):
        watcher = _rule04_watcher(tmp_path)
        fresh = datetime.now(tz=timezone.utc).isoformat()
        watcher.state.news_warned = {"fresh": fresh}
        calls = {"n": 0}
        real_save = watcher.state.save

        def counting_save():
            calls["n"] += 1
            real_save()

        watcher.state.save = counting_save

        await watcher._rule_04_warnings()

        assert calls["n"] == 0, "nothing changed — must not write to the DB"


class TestRule04WarningsCoverTheRunnerLeg:
    """Reviewer finding, carried into Phase 2 Task 4's scope: `open_runner`
    (journal.py — TP1 already closed half the position, the runner leg is
    still open) used to be missing from `_rule_04_warnings`'s status filter,
    which only checked `("pending", "open")`. A runner-leg position is still
    live and exposed to the news release exactly like a plain `open` one, so
    it must still get the pre-news warning."""

    class _FakeNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kwargs):
            self.sent.append(text)
            return 1

    def _watcher(self, tmp_path, status):
        from smc_watcher import Watcher
        from app.services.smc.news import NewsCalendar, NewsEvent

        db = Database(str(tmp_path / "smc.db"))
        watcher = Watcher.__new__(Watcher)
        watcher.db = db
        watcher.state = WatcherState(db)
        watcher.journal = SignalJournal(db)
        watcher.notifier = self._FakeNotifier()
        watcher.news = NewsCalendar()
        # ETHUSD is crypto -> relevant_currencies is {"USD"} (news.py).
        # 15 minutes out sits inside _rule_04_warnings' 30-minute horizon,
        # comfortably clear of the real wall clock at test-run time.
        event_time = datetime.now(tz=timezone.utc) + timedelta(minutes=15)
        watcher.news.events = [
            NewsEvent(time=event_time, currency="USD", title="Fed Speech")
        ]
        watcher.journal.signals.append(
            {"id": "sig1", "pair": "ETHUSD", "status": status}
        )
        return watcher

    @pytest.mark.asyncio
    async def test_open_runner_signal_gets_the_pre_news_warning(self, tmp_path):
        watcher = self._watcher(tmp_path, "open_runner")

        await watcher._rule_04_warnings()

        assert watcher.notifier.sent, "a runner-leg signal must still warn"
        message = watcher.notifier.sent[0]
        assert "ETHUSD" in message
        assert "RULE 0.4" in message
        assert "an open position" in message

    @pytest.mark.asyncio
    async def test_a_resolved_signal_still_does_not_warn(self, tmp_path):
        """Control: a signal that has already resolved (e.g. "tp") is not an
        active position or order, so the filter must keep excluding it."""
        watcher = self._watcher(tmp_path, "tp")

        await watcher._rule_04_warnings()

        assert watcher.notifier.sent == []


# --------------------------------------------------------------- (b) journal


def _journal_with_poison_rows(tmp_path):
    db = Database(str(tmp_path / "smc.db"))
    journal = SignalJournal(db)
    base = dict(
        pair="ETHUSD",
        direction="long",
        entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        rr=2.0,
        session="Frankfurt/London",
        status="pending",
        filled_at=None,
        resolved_at=None,
        checked_until=None,
        taken=None,
        message_id=None,
        alert_text=None,
        profile_key="conservative",
    )
    garbage = dict(
        base, id="sig-garbage", created_at="garbage", expires_at="garbage"
    )
    naive = dict(
        base,
        id="sig-naive",
        created_at="2020-01-01T00:00:00",  # naive, real calendar date
        expires_at="2020-01-02T00:00:00",  # naive, clearly in the past
    )
    journal.signals = [garbage, naive]
    return journal


class TestJournalUpdatePairSurvivesPoisonTimestamps:
    def test_update_pair_completes_and_rows_resolve_expired(self, tmp_path):
        journal = _journal_with_poison_rows(tmp_path)

        events = journal.update_pair("ETHUSD", [])  # must not raise

        by_id = {s["id"]: s for s in journal.signals}
        assert by_id["sig-garbage"]["status"] == "expired"
        assert by_id["sig-naive"]["status"] == "expired"
        resolved_ids = {signal["id"] for signal, _event in events}
        assert resolved_ids == {"sig-garbage", "sig-naive"}


# ----------------------------------------------------- (c) cooldown read-path


class _CooldownSpyState:
    def __init__(self, pair_cooldown):
        self.pair_cooldown = dict(pair_cooldown)
        self.save_calls = 0

    def save(self):
        self.save_calls += 1


def _cooldown_watcher(pair_cooldown):
    from smc_watcher import Watcher

    watcher = Watcher.__new__(Watcher)
    watcher.state = _CooldownSpyState(pair_cooldown)
    return watcher


class TestCooldownLeftIsReadOnly:
    def test_expired_entry_returns_falsy_without_mutation_or_save(self):
        expired = (
            datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        watcher = _cooldown_watcher({"ETHUSD": expired})

        result = watcher._cooldown_left("ETHUSD")

        assert not result
        assert watcher.state.save_calls == 0
        assert watcher.state.pair_cooldown == {"ETHUSD": expired}

    def test_poison_entry_returns_falsy_without_raising_or_mutation(self):
        watcher = _cooldown_watcher({"ETHUSD": "garbage"})

        result = watcher._cooldown_left("ETHUSD")  # must not raise

        assert not result
        assert watcher.state.save_calls == 0
        assert watcher.state.pair_cooldown == {"ETHUSD": "garbage"}

    def test_active_entry_still_reports_remaining_time(self):
        active = (
            datetime.now(tz=timezone.utc) + timedelta(hours=1, minutes=30)
        ).isoformat()
        watcher = _cooldown_watcher({"ETHUSD": active})

        result = watcher._cooldown_left("ETHUSD")

        assert result is not None
        assert watcher.state.save_calls == 0


# ------------------------------------------------- (d) purge inside run_cycle


class TestPurgeExpiredCooldowns:
    def test_purge_removes_expired_and_poison_keeps_active_saves_once(self):
        expired = (
            datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        active = (
            datetime.now(tz=timezone.utc) + timedelta(hours=1)
        ).isoformat()
        watcher = _cooldown_watcher(
            {"ETHUSD": expired, "USDJPY": "garbage", "EURUSD": active}
        )

        watcher._purge_expired_cooldowns()  # must not raise

        assert watcher.state.pair_cooldown == {"EURUSD": active}
        assert watcher.state.save_calls == 1

    def test_purge_is_a_noop_when_nothing_expired(self):
        active = (
            datetime.now(tz=timezone.utc) + timedelta(hours=1)
        ).isoformat()
        watcher = _cooldown_watcher({"EURUSD": active})

        watcher._purge_expired_cooldowns()

        assert watcher.state.pair_cooldown == {"EURUSD": active}
        assert watcher.state.save_calls == 0


class TestPurgeWiredIntoRunCycle:
    @pytest.mark.asyncio
    async def test_run_cycle_invokes_purge_when_pairs_are_active(
        self, tmp_path
    ):
        from smc_watcher import Watcher

        db = Database(str(tmp_path / "smc.db"))
        watcher = Watcher.__new__(Watcher)
        watcher.db = db
        watcher.state = WatcherState(db)
        watcher.state.pairs = ["ETHUSD"]
        watcher.journal = SignalJournal(db)
        watcher.notifier = _NullNotifier()
        watcher.news = None
        watcher.last_results = {}
        from app.services.smc.planbook import PlanBook

        watcher.planbook = PlanBook()

        async def _no_track(*args, **kwargs):
            return None

        watcher._track_journal = _no_track

        async def _check_pair(key):
            return f"{key}: nothing", None

        watcher.check_pair = _check_pair

        calls = {"n": 0}
        real_purge = watcher._purge_expired_cooldowns

        def spy_purge():
            calls["n"] += 1
            real_purge()

        watcher._purge_expired_cooldowns = spy_purge

        await watcher.run_cycle()

        assert calls["n"] == 1


class _NullNotifier:
    async def send(self, text, reply_markup=None, disable_notification=False):
        return 1

    async def pin(self, message_id):
        pass

    async def send_photo(self, *args, **kwargs):
        return None
