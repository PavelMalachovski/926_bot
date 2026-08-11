"""Pair-list persistence contract: 'never set' must default, a deliberately
empty list must stay empty.

Review finding (2026-08-11, MEDIUM): `db.kv_get("pairs") or []` could not
tell a stored `[]` (the owner disabled every pair via /pairs) apart from
"never set" — both produced an empty list, and the old `[p for p in pairs
if p in INSTRUMENTS] or list(DEFAULT_PAIRS)` chain then resurrected the
default pairs from that empty list. A Railway redeploy would silently
re-enable every pair the owner had turned off.
"""

from app.core.config import settings
from app.services.smc.db import Database
from app.services.smc.instruments import DEFAULT_PAIRS
from app.services.smc.state import WatcherState


class TestWatcherStatePairsContract:
    @staticmethod
    def _db(tmp_path, name="smc.db"):
        return Database(str(tmp_path / name))

    def test_never_set_pairs_use_defaults(self, tmp_path):
        db = self._db(tmp_path)
        assert db.kv_get("pairs") is None  # precondition: truly never set
        state = WatcherState(db)
        assert state.pairs == list(DEFAULT_PAIRS)

    def test_deliberately_emptied_via_kv_set_stays_empty_across_reload(
        self, tmp_path
    ):
        db = self._db(tmp_path)
        db.kv_set("pairs", [])
        state = WatcherState(db)
        assert state.pairs == []

        reloaded = WatcherState(Database(str(tmp_path / "smc.db")))
        assert reloaded.pairs == []

    def test_deliberately_emptied_via_toggle_pair_stays_empty_across_reload(
        self, tmp_path
    ):
        db = self._db(tmp_path)
        state = WatcherState(db)
        for pair in list(state.pairs):
            state.toggle_pair(pair)
        assert state.pairs == []

        reloaded = WatcherState(Database(str(tmp_path / "smc.db")))
        assert reloaded.pairs == []

    def test_unknown_pairs_still_filtered_when_list_is_non_empty(self, tmp_path):
        db = self._db(tmp_path)
        db.kv_set("pairs", ["ETHUSD", "DOGEUSD"])
        state = WatcherState(db)
        assert state.pairs == ["ETHUSD"]


class _WatcherFactory:
    """Boots the real `smc_watcher.Watcher()` against one shared DB file so
    consecutive 'restarts' can be simulated within a test."""

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.telegram, "bot_token", "123:dummy")
        monkeypatch.setattr(settings.telegram, "chat_id", "1")
        monkeypatch.setattr(settings.smc, "chat_id", None)
        db_path = str(tmp_path / "smc.db")
        monkeypatch.setenv("SMC_DB_FILE", db_path)

        import importlib

        import smc_watcher

        importlib.reload(smc_watcher)
        monkeypatch.setattr(smc_watcher, "DB_FILE", db_path)
        self._module = smc_watcher

    def boot(self):
        return self._module.Watcher()


class TestPairListSurvivesRestart:
    """End-to-end: `smc_watcher.py`'s env-default block already gates on
    `db.kv_get("pairs") is None` one layer above WatcherState — this checks
    the two compose correctly through a real boot, not just WatcherState in
    isolation."""

    def test_never_configured_pairs_get_env_defaults_on_first_boot(
        self, tmp_path, monkeypatch
    ):
        factory = _WatcherFactory(tmp_path, monkeypatch)
        w = factory.boot()
        assert w.state.pairs == ["ETHUSD", "USDJPY"]

    def test_all_pairs_disabled_then_redeployed_do_not_resurrect(
        self, tmp_path, monkeypatch
    ):
        factory = _WatcherFactory(tmp_path, monkeypatch)
        w1 = factory.boot()
        for pair in list(w1.state.pairs):
            w1.state.toggle_pair(pair)
        assert w1.state.pairs == []

        w2 = factory.boot()  # simulated Railway redeploy, same volume
        assert w2.state.pairs == []
