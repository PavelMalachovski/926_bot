"""Owner request 2026-08-31: news at 07:55, plans at 08:05 and 14:05.

The times themselves are one-line defaults. What needed code is the WAKE:
both the digest and the auto-plan snapshot fire from inside `run_cycle`, so
a time that does not sit on the cadence grid is served by the next tick
instead of at the time asked for. 08:05 and 14:05 are on the 5-minute
in-session grid, but 07:55 is off the 15-minute off-session grid (which
ticks 07:45, then 08:00) — so the scheduler now wakes at the digest time
too, not only at the plan slots.
"""

from datetime import datetime, timezone

import pytest

from app.core.config import SMCSettings, settings
from app.services.smc.db import Database
from app.services.smc.sessions import PRAGUE, active_session
from app.services.smc.state import WatcherState
from smc_watcher import _parse_hhmm


def _watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    watcher = Watcher.__new__(Watcher)
    watcher.db = db
    watcher.state = WatcherState(db)
    return watcher


class TestDefaults:
    def test_the_shipped_schedule(self):
        s = SMCSettings()
        assert s.news_digest_time == "07:55"
        assert s.auto_plan_times == "08:05,14:05"

    def test_the_plan_slots_open_the_session_blocks_they_belong_to(self):
        """08:05 sits in Frankfurt/London, 14:05 in New York — each plan is
        built on candles its own block has already printed."""
        for hhmm, block in (("08:05", "Frankfurt/London"), ("14:05", "New York")):
            hh, mm = (int(x) for x in hhmm.split(":"))
            local = PRAGUE.localize(datetime(2026, 8, 31, hh, mm))
            assert active_session(local.astimezone(timezone.utc)) == block

    def test_the_digest_lands_before_the_first_block_opens(self):
        local = PRAGUE.localize(datetime(2026, 8, 31, 7, 55))
        assert active_session(local.astimezone(timezone.utc)) is None


class TestParseHhmm:
    @pytest.mark.parametrize(
        "raw,expected",
        [("07:55", "07:55"), (" 8:05 ", "08:05"), ("14:05", "14:05")],
    )
    def test_valid(self, raw, expected):
        assert _parse_hhmm(raw) == expected

    @pytest.mark.parametrize("raw", ["25:99", "garbage", "", "8", None, "08:60"])
    def test_invalid(self, raw):
        assert _parse_hhmm(raw) is None


class TestWakeSlots:
    def test_it_covers_both_the_plans_and_the_digest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.smc, "auto_plan", True)
        monkeypatch.setattr(settings.smc, "auto_plan_times", "08:05,14:05")
        monkeypatch.setattr(settings.smc, "news_digest", True)
        monkeypatch.setattr(settings.smc, "news_digest_time", "07:55")
        assert _watcher(tmp_path)._wake_slots() == ["07:55", "08:05", "14:05"]

    def test_the_digest_wake_survives_auto_plan_being_off(
        self, tmp_path, monkeypatch
    ):
        """The regression this change exists for: with auto-plan disabled the
        old code returned None and 07:55 waited for the 08:00 tick."""
        monkeypatch.setattr(settings.smc, "auto_plan", False)
        monkeypatch.setattr(settings.smc, "news_digest", True)
        monkeypatch.setattr(settings.smc, "news_digest_time", "07:55")
        assert _watcher(tmp_path)._wake_slots() == ["07:55"]

    def test_no_digest_no_digest_wake(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.smc, "auto_plan", True)
        monkeypatch.setattr(settings.smc, "auto_plan_times", "08:05")
        monkeypatch.setattr(settings.smc, "news_digest", False)
        assert _watcher(tmp_path)._wake_slots() == ["08:05"]

    def test_a_garbage_digest_time_is_ignored_not_fatal(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings.smc, "auto_plan", False)
        monkeypatch.setattr(settings.smc, "news_digest", True)
        monkeypatch.setattr(settings.smc, "news_digest_time", "nonsense")
        assert _watcher(tmp_path)._wake_slots() == []

    def test_everything_off_means_nothing_to_wake_for(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.smc, "auto_plan", False)
        monkeypatch.setattr(settings.smc, "news_digest", False)
        watcher = _watcher(tmp_path)
        assert watcher._wake_slots() == []
        assert watcher._seconds_until_next_wake() is None


class TestSecondsUntilNextWake:
    def test_it_returns_the_nearest_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.smc, "auto_plan", True)
        monkeypatch.setattr(settings.smc, "auto_plan_times", "08:05,14:05")
        monkeypatch.setattr(settings.smc, "news_digest", True)
        monkeypatch.setattr(settings.smc, "news_digest_time", "07:55")
        seconds = _watcher(tmp_path)._seconds_until_next_wake()
        assert seconds is not None
        assert 0 < seconds <= 24 * 3600  # always within a day, never negative
