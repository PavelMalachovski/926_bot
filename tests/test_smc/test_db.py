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
