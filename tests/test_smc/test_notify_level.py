"""Tests for the notification level state (owner decision 2026-08-12,
Phase 2 Task 4b). The /notify command and its keyboard were retired with the
command menu trim (owner decision D27, 2026-09-06); the level itself stays
as internal plumbing read by `Watcher._alert_send_suppressed`.
"""

import pytest

from app.services.smc.db import Database
from app.services.smc.state import NOTIFY_LEVELS, WatcherState


# --------------------------------------------------------------------- state


class TestNotifyLevelState:
    def test_defaults_to_all(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        assert state.notify_level == "all"

    def test_set_and_persists_across_reload(self, tmp_path):
        db_path = str(tmp_path / "s.db")
        state = WatcherState(Database(db_path))

        state.set_notify_level("star")

        assert state.notify_level == "star"
        reloaded = WatcherState(Database(db_path))
        assert reloaded.notify_level == "star"

    def test_all_three_levels_are_accepted(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        for level in NOTIFY_LEVELS:
            state.set_notify_level(level)
            assert state.notify_level == level

    def test_rejects_unknown_level(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))

        with pytest.raises(ValueError):
            state.set_notify_level("loud")

        assert state.notify_level == "all"  # unchanged by the rejected call
