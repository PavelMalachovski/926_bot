"""Tests for SQLite database resilience."""

import sqlite3

from app.services.smc.db import Database


def test_signal_profile_key_column(tmp_path):
    from app.services.smc.db import Database, SIGNAL_COLUMNS
    assert "profile_key" in SIGNAL_COLUMNS
    db = Database(str(tmp_path / "t.db"))
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(signals)")}
    assert "profile_key" in cols


class TestDatabaseOpen:
    def test_creates_missing_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dirs" / "smc.db"
        db = Database(str(path))
        db.kv_set("probe", 1)
        assert path.exists()
        assert Database(str(path)).kv_get("probe") == 1

    def test_falls_back_when_path_unusable(self, tmp_path, monkeypatch):
        # A file where a directory is expected makes the path unusable.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.chdir(tmp_path)  # fallback file lands in tmp, not the repo
        db = Database(str(blocker / "sub" / "smc.db"))
        assert db.path == Database.FALLBACK_PATH
        db.kv_set("probe", "ok")  # still functional
        assert db.kv_get("probe") == "ok"

    def test_schema_creation_failure_does_not_crash_the_watcher(
        self, tmp_path, monkeypatch
    ):
        """CLAUDE.md: db.py must never crash the watcher. The constructor's
        first `with self.conn:` block (CREATE TABLE/INDEX/ALTER TABLE) used
        to be unguarded, so a sqlite3.Error there escaped __init__ and would
        crash-loop the process — the same failure mode
        `_relax_take_profit_not_null` already guards against, one step
        later in the schema. This pins the earlier guard.

        sqlite3.Connection is a C type and cannot be monkeypatched directly
        (setting `.execute` on it raises TypeError), so the failure is
        injected via a Connection subclass passed as `sqlite3.connect`'s
        `factory=`.
        """
        class _RaisingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if "CREATE TABLE" in sql and "signals" in sql:
                    raise sqlite3.OperationalError("simulated failure")
                return super().execute(sql, *args, **kwargs)

        original_connect = sqlite3.connect

        def connect_with_raising_factory(path, **kwargs):
            kwargs["factory"] = _RaisingConnection
            return original_connect(path, **kwargs)

        monkeypatch.setattr(
            "app.services.smc.db.sqlite3.connect", connect_with_raising_factory
        )
        db = Database(str(tmp_path / "smc.db"))  # must not raise
        assert db.conn is not None


class TestDatabaseRuntimeErrors:
    """CLAUDE.md: 'db.py must never crash the watcher' — this used to hold
    only at connect time. `sqlite3.connect` is lazy, so a corrupted DB file
    opens fine and only fails once a query actually runs: `_create_schema`
    caught that, but every later read (`kv_get`, `signals_all`, ...) was
    unguarded, so the first real read after boot raised straight out of
    `WatcherState.__init__` / `SignalJournal.__init__` and crash-looped the
    process. These pin the fix: reads degrade to safe defaults and the
    Database falls back to the same local ephemeral file `_connect` already
    uses for connect-time failures.
    """

    @staticmethod
    def _raising_connection_factory():
        class _RaisingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                raise sqlite3.DatabaseError("database disk image is malformed")

        return _RaisingConnection

    def test_runtime_error_falls_back_to_local_file_and_keeps_serving(
        self, tmp_path, monkeypatch
    ):
        """Only the configured path is corrupted; the fallback file is a
        real, healthy sqlite file — recovery should make later calls succeed
        again, not just return defaults forever."""
        corrupted_path = str(tmp_path / "corrupted.db")
        original_connect = sqlite3.connect
        raising_factory = self._raising_connection_factory()

        def flaky_connect(path, **kwargs):
            if path == corrupted_path:
                kwargs["factory"] = raising_factory
            return original_connect(path, **kwargs)

        monkeypatch.setattr("app.services.smc.db.sqlite3.connect", flaky_connect)
        monkeypatch.chdir(tmp_path)  # fallback file lands in tmp, not the repo

        db = Database(corrupted_path)  # construction must not raise

        assert db.kv_get("pairs") is None  # degrades instead of raising
        assert db.signals_all() == []  # degrades instead of raising
        assert db.path == Database.FALLBACK_PATH  # switched to the fallback

        # the fallback connection is genuinely healthy: normal use resumes
        db.kv_set("pairs", ["ETHUSD"])
        assert db.kv_get("pairs") == ["ETHUSD"]

    def test_runtime_error_keeps_returning_defaults_when_fallback_also_fails(
        self, tmp_path, monkeypatch
    ):
        """Every connection (including the fallback) is corrupted: calls
        must keep degrading gracefully rather than raising, forever."""
        original_connect = sqlite3.connect
        raising_factory = self._raising_connection_factory()

        def always_raising_connect(path, **kwargs):
            kwargs["factory"] = raising_factory
            return original_connect(path, **kwargs)

        monkeypatch.setattr(
            "app.services.smc.db.sqlite3.connect", always_raising_connect
        )
        monkeypatch.chdir(tmp_path)

        db = Database(str(tmp_path / "corrupted.db"))  # must not raise

        assert db.kv_get("pairs") is None
        assert db.signals_all() == []
        db.kv_set("pairs", ["ETHUSD"])  # a no-op, must not raise
        assert db.kv_get("pairs") is None  # still nothing persisted, still safe

    def test_watcher_state_constructs_on_a_fully_corrupted_database(
        self, tmp_path, monkeypatch
    ):
        """The exact boot-chain failure from the review: the first raw kv
        read used to raise straight out of WatcherState.__init__."""
        from app.services.smc.state import WatcherState

        original_connect = sqlite3.connect
        raising_factory = self._raising_connection_factory()

        def always_raising_connect(path, **kwargs):
            kwargs["factory"] = raising_factory
            return original_connect(path, **kwargs)

        monkeypatch.setattr(
            "app.services.smc.db.sqlite3.connect", always_raising_connect
        )
        monkeypatch.chdir(tmp_path)

        db = Database(str(tmp_path / "corrupted.db"))
        state = WatcherState(db)  # must not raise
        assert state.pairs == ["ETHUSD", "USDJPY"]

    def test_migration_gate_survives_a_broken_kv_get(self, tmp_path, monkeypatch):
        """migrate_legacy_json's gating `kv_get('pairs')` call used to sit
        outside its try — moved inside its own try so a broken read there
        skips migration instead of crashing the boot chain."""
        import json

        from app.services.smc.db import migrate_legacy_json

        db = Database(str(tmp_path / "smc.db"))
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"pairs": ["ETHUSD"]}))

        def raise_kv_get(key, default=None):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db, "kv_get", raise_kv_get)

        migrate_legacy_json(
            db, str(state_file), str(tmp_path / "journal.json")
        )  # must not raise

        assert state_file.exists()  # migration was skipped, not half-run
