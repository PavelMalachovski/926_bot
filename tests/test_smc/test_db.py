"""Tests for SQLite database resilience."""

import sqlite3

from app.services.smc.db import Database


def test_signal_profile_key_column(tmp_path):
    from app.services.smc.db import Database, SIGNAL_COLUMNS
    assert "profile_key" in SIGNAL_COLUMNS
    db = Database(str(tmp_path / "t.db"))
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(signals)")}
    assert "profile_key" in cols


class TestHybridLifecycleColumns:
    """Phase 2 sniper redesign: tp1/runner_tp/tier/result_r/tp1_at back the
    partial-close lifecycle (journal.evaluate_signal) and must exist on
    both a fresh schema and a DB file created before they did."""

    NEW_COLUMNS = {"tp1", "runner_tp", "tier", "result_r", "tp1_at"}

    def test_fresh_schema_has_the_new_columns(self, tmp_path):
        from app.services.smc.db import SIGNAL_COLUMNS

        assert self.NEW_COLUMNS <= set(SIGNAL_COLUMNS)
        db = Database(str(tmp_path / "fresh.db"))
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(signals)")}
        assert self.NEW_COLUMNS <= cols

    def test_old_schema_db_file_migrates_in_place_and_keeps_rows(self, tmp_path):
        """A DB file built on the schema as it existed before this task (no
        tp1/runner_tp/tier/result_r/tp1_at columns) must gain them in place —
        without losing the row that was already there."""
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE signals (
                id TEXT PRIMARY KEY, pair TEXT NOT NULL, direction TEXT NOT NULL,
                entry REAL NOT NULL, stop_loss REAL NOT NULL,
                take_profit REAL, rr REAL NOT NULL, session TEXT,
                created_at TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL,
                filled_at TEXT, resolved_at TEXT, checked_until TEXT,
                taken INTEGER, message_id INTEGER, alert_text TEXT,
                profile_key TEXT)"""
        )
        conn.execute(
            "INSERT INTO signals (id, pair, direction, entry, stop_loss, "
            "take_profit, rr, session, created_at, status) VALUES "
            "('old1', 'ETHUSD', 'long', 100.0, 90.0, 120.0, 2.0, "
            "'New York', '2026-01-01T00:00:00+00:00', 'tp')"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(signals)")}
        assert self.NEW_COLUMNS <= cols

        rows = db.signals_all()
        assert len(rows) == 1
        assert rows[0]["id"] == "old1"
        assert rows[0]["status"] == "tp"
        # migrated columns default to NULL on a pre-existing row
        assert rows[0]["tp1"] is None
        assert rows[0]["runner_tp"] is None
        assert rows[0]["tier"] is None
        assert rows[0]["result_r"] is None
        assert rows[0]["tp1_at"] is None


class TestZoneKindColumn:
    """Range trading (Task 5): the journal records which kind of H1 zone a
    setup came from — "OB", "FVG" or "RANGE" — via `signals.zone_kind`, on
    both a fresh schema and a DB file created before the column did."""

    def test_fresh_schema_has_zone_kind_column(self, tmp_path):
        from app.services.smc.db import SIGNAL_COLUMNS

        assert "zone_kind" in SIGNAL_COLUMNS
        db = Database(str(tmp_path / "fresh.db"))
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(signals)")}
        assert "zone_kind" in cols

    def test_old_schema_db_file_migrates_in_place_and_keeps_rows(self, tmp_path):
        """A DB file built on the schema as it existed before this task (no
        zone_kind column) must gain it in place — without losing the row
        that was already there."""
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE signals (
                id TEXT PRIMARY KEY, pair TEXT NOT NULL, direction TEXT NOT NULL,
                entry REAL NOT NULL, stop_loss REAL NOT NULL,
                take_profit REAL, rr REAL NOT NULL, session TEXT,
                created_at TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL,
                filled_at TEXT, resolved_at TEXT, checked_until TEXT,
                taken INTEGER, message_id INTEGER, alert_text TEXT,
                profile_key TEXT, tp1 REAL, runner_tp REAL, tier TEXT,
                result_r REAL, tp1_at TEXT)"""
        )
        conn.execute(
            "INSERT INTO signals (id, pair, direction, entry, stop_loss, "
            "take_profit, rr, session, created_at, status) VALUES "
            "('old1', 'ETHUSD', 'long', 100.0, 90.0, 120.0, 2.0, "
            "'New York', '2026-01-01T00:00:00+00:00', 'tp')"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(signals)")}
        assert "zone_kind" in cols

        rows = db.signals_all()
        assert len(rows) == 1
        assert rows[0]["id"] == "old1"
        assert rows[0]["status"] == "tp"
        # a pre-existing row has no zone_kind — the migration must not
        # invent one
        assert rows[0]["zone_kind"] is None


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
        must keep degrading gracefully rather than raising, forever. The
        recovery attempt must also be honest about it — a probe query on
        the fallback connection must fail before any 'switched to a local
        database' success line is logged, since _create_schema and
        _relax_take_profit_not_null both swallow sqlite3.Error internally
        (CLAUDE.md) and would otherwise let that log through unchallenged
        even though the fallback connection cannot serve a real query
        either."""
        import app.services.smc.db as db_mod

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

        events = []

        class _Spy:
            def error(self, event, **kw):
                events.append(event)

            def info(self, event, **kw):
                pass

        monkeypatch.setattr(db_mod, "logger", _Spy())

        assert db.kv_get("pairs") is None
        assert db.signals_all() == []
        db.kv_set("pairs", ["ETHUSD"])  # a no-op, must not raise
        assert db.kv_get("pairs") is None  # still nothing persisted, still safe

        # The fallback probe failed, so the success line must never fire...
        assert not any("switched to a local ephemeral" in e for e in events)
        # ...and the honest failure line was logged instead.
        assert any("also unusable" in e for e in events)

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
        from app.services.smc.instruments import DEFAULT_PAIRS

        assert state.pairs == list(DEFAULT_PAIRS)

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
