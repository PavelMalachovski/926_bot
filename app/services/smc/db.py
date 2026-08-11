"""SQLite persistence for the watcher: signal journal + key-value state.

One small database file holds everything that must survive restarts:
recorded entries (signals) and runtime state (selected pairs, dedup keys).
On Railway attach a volume and point SMC_DB_FILE at it (e.g. /data/smc.db)
so the data also survives redeploys.
"""

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

SIGNAL_COLUMNS = [
    "id",
    "pair",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "rr",
    "session",
    "created_at",
    "expires_at",
    "status",
    "filled_at",
    "resolved_at",
    "checked_until",
    "taken",  # None = unanswered, 1 = user took the trade, 0 = skipped
    "message_id",  # Telegram message id of the alert (live setup card)
    "alert_text",  # original alert body, re-used when editing the card
    "profile_key",  # conservative | aggressive
]

# Manual trade journal parsed from MetaTrader screenshots.
TRADE_COLUMNS = [
    "id",  # uuid
    "ticket",  # MT4/MT5 order id (used for de-duplication)
    "symbol",
    "direction",  # buy / sell
    "volume",  # lots
    "open_price",
    "close_price",
    "open_time",  # ISO string
    "close_time",  # ISO string
    "sl",
    "tp",
    "profit",
    "swap",
    "commission",
    "taxes",
    "closed_by_sl",  # 1 if the "[sl]" marker was present
    "status",  # pending / confirmed
    "batch_id",
    "created_at",
]


# `take_profit` is nullable on purpose (detector mode, 2026-08-06): a setup
# with no unswept liquidity ahead is announced and tracked, it simply has no
# structural objective. Databases created before that date declared the column
# NOT NULL — see _relax_take_profit_not_null.
SIGNALS_TABLE_SQL = """
                CREATE TABLE IF NOT EXISTS {name} (
                    id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL,
                    rr REAL NOT NULL,
                    session TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    status TEXT NOT NULL,
                    filled_at TEXT,
                    resolved_at TEXT,
                    checked_until TEXT,
                    taken INTEGER,
                    message_id INTEGER,
                    alert_text TEXT,
                    profile_key TEXT
                )
"""


class Database:
    """Thin wrapper over sqlite3 with a signals table and a kv store."""

    FALLBACK_PATH = ".smc_watcher.db"

    def __init__(self, path: str):
        self.path = path
        self.conn = self._connect(path)
        self.conn.row_factory = sqlite3.Row
        # Runtime (post-connect) SQLite errors: connect() is lazy, so a
        # corrupted file opens fine and only fails once a query actually
        # runs. `_run` below is the one-attempt-total fallback for that;
        # these two flags back it.
        self._runtime_fallback_attempted = False
        self._logged_runtime_errors: set = set()
        self._create_schema()
        # Deliberately outside the transaction above: a table rebuild is much
        # heavier than an ADD COLUMN and must not be able to take the watcher
        # down with it.
        self._relax_take_profit_not_null()

    def _create_schema(self) -> None:
        """Create tables/indexes and add columns missing from an older DB.

        A failure here must never crash the watcher (see CLAUDE.md): the same
        reasoning as `_relax_take_profit_not_null` below applies one step
        earlier — a bot that cannot start sends no alerts at all, whereas a
        bot on a not-yet-migrated schema merely fails the operations that
        touch the missing piece. Logs loudly and retries on the next start.
        """
        try:
            with self.conn:
                self.conn.execute(SIGNALS_TABLE_SQL.format(name="signals"))
                self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS kv "
                    "(key TEXT PRIMARY KEY, value TEXT)"
                )
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trades (
                        id TEXT PRIMARY KEY,
                        ticket TEXT,
                        symbol TEXT NOT NULL,
                        direction TEXT,
                        volume REAL,
                        open_price REAL,
                        close_price REAL,
                        open_time TEXT,
                        close_time TEXT,
                        sl REAL,
                        tp REAL,
                        profit REAL DEFAULT 0,
                        swap REAL DEFAULT 0,
                        commission REAL DEFAULT 0,
                        taxes REAL DEFAULT 0,
                        closed_by_sl INTEGER DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'pending',
                        batch_id TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_status "
                    "ON trades(status)"
                )
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_batch "
                    "ON trades(batch_id)"
                )
                # Migrate databases created before these columns existed
                existing = {
                    row["name"]
                    for row in self.conn.execute("PRAGMA table_info(signals)")
                }
                for column, sql_type in (
                    ("taken", "INTEGER"),
                    ("message_id", "INTEGER"),
                    ("alert_text", "TEXT"),
                    ("profile_key", "TEXT"),
                ):
                    if column not in existing:
                        self.conn.execute(
                            f"ALTER TABLE signals ADD COLUMN {column} {sql_type}"
                        )
        except sqlite3.Error as e:
            logger.error(
                "Could not create/migrate the database schema — the watcher "
                "keeps running, but operations on a missing table or column "
                "will fail until this succeeds. Will retry on the next "
                "start.",
                path=self.path,
                error=str(e),
            )

    def _relax_take_profit_not_null(self) -> None:
        """Drop the legacy NOT NULL on signals.take_profit.

        Detector mode records setups that have no unswept liquidity ahead, so
        the take-profit may be NULL. SQLite cannot drop a column constraint in
        place, so the table is rebuilt once (copy → drop → rename) with every
        row preserved.

        A failure here must never crash the watcher (see CLAUDE.md): a bot that
        cannot start sends no alerts at all, whereas a bot on an unmigrated
        table merely fails to insert the rare take-profit-less signal. So this
        logs loudly and returns, and retries on the next start — the rebuild
        drops any leftover scratch table first, so a half-finished attempt
        heals itself instead of poisoning every later one.
        """
        try:
            info = list(self.conn.execute("PRAGMA table_info(signals)"))
            if not any(r["name"] == "take_profit" and r["notnull"] for r in info):
                return
            # The copy lists SIGNAL_COLUMNS explicitly, so anything else in the
            # legacy table would be silently dropped — say so out loud.
            unknown = [r["name"] for r in info if r["name"] not in SIGNAL_COLUMNS]
            if unknown:
                logger.error(
                    "Rebuilding the signals table will DROP columns that are "
                    "not in SIGNAL_COLUMNS — their data is lost. Add them to "
                    "SIGNAL_COLUMNS if they are still needed.",
                    columns=unknown,
                )
            columns = ", ".join(SIGNAL_COLUMNS)
            with self.conn:
                self.conn.execute("DROP TABLE IF EXISTS signals_migrated")
                self.conn.execute(SIGNALS_TABLE_SQL.format(name="signals_migrated"))
                self.conn.execute(
                    f"INSERT INTO signals_migrated ({columns}) "
                    f"SELECT {columns} FROM signals"
                )
                self.conn.execute("DROP TABLE signals")
                self.conn.execute("ALTER TABLE signals_migrated RENAME TO signals")
            logger.info("Migrated signals.take_profit to a nullable column")
        except sqlite3.Error as e:
            logger.error(
                "Could not migrate signals.take_profit to a nullable column — "
                "the watcher keeps running, but recording a setup with no "
                "take-profit will fail until this succeeds. Will retry on the "
                "next start.",
                path=self.path,
                error=str(e),
            )

    def _connect(self, path: str) -> sqlite3.Connection:
        """Open the database, creating parent dirs; fall back instead of dying.

        A crash-looping watcher sends no alerts at all, so if the configured
        path is unusable (typical case: a root-owned Railway volume and a
        non-root container) we degrade to a local ephemeral file and log
        loudly — alerts keep flowing, only persistence is reduced.
        """
        try:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
            return sqlite3.connect(path, check_same_thread=False)
        except (sqlite3.OperationalError, OSError) as e:
            if path == self.FALLBACK_PATH:
                raise
            logger.error(
                "Cannot open database at configured path — falling back to a "
                "local ephemeral file (data will NOT survive redeploys). "
                "Check volume mount and permissions.",
                configured_path=path,
                fallback=self.FALLBACK_PATH,
                error=str(e),
            )
            self.path = self.FALLBACK_PATH
            return sqlite3.connect(self.FALLBACK_PATH, check_same_thread=False)

    # ---------------------------------------------------- runtime error guard

    def _recover_to_local_fallback(self) -> bool:
        """One-time escape hatch for a `sqlite3.Error` raised after connect.

        `sqlite3.connect` is lazy: a corrupted file, a full disk or a
        read-only mount all open successfully and only fail once a query
        runs. This mirrors `_connect`'s connect-time fallback for that case,
        so operations degrade to the same local ephemeral file instead of
        crash-looping the process (CLAUDE.md). Attempted at most once per
        process — if the fallback file is also unusable there is nowhere
        else to go, so later callers just keep getting safe defaults.
        """
        if self._runtime_fallback_attempted:
            return False
        self._runtime_fallback_attempted = True
        if self.path == self.FALLBACK_PATH:
            return False  # already on the fallback file; nothing left to try
        try:
            conn = sqlite3.connect(self.FALLBACK_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self.conn = conn
            self.path = self.FALLBACK_PATH
            self._create_schema()
            self._relax_take_profit_not_null()
            logger.error(
                "Runtime SQLite error — switched to a local ephemeral "
                "database (data will NOT survive redeploys). Check the "
                "configured DB path/volume.",
                fallback=self.FALLBACK_PATH,
            )
            return True
        except sqlite3.Error as e:
            logger.error(
                "Fallback database also failed after a runtime SQLite "
                "error — the watcher keeps running on safe defaults, but "
                "nothing will persist this process.",
                fallback=self.FALLBACK_PATH,
                error=str(e),
            )
            return False

    def _run(self, method: str, fn, default):
        """Run `fn()` (a closure over `self.conn`), degrading to `default`
        on a `sqlite3.Error` instead of letting it escape. Logs once per
        method per process (avoids per-cycle spam) and makes one fallback
        attempt total via `_recover_to_local_fallback`, retrying `fn` once
        against the recovered connection before giving up.
        """
        try:
            return fn()
        except sqlite3.Error as e:
            if method not in self._logged_runtime_errors:
                self._logged_runtime_errors.add(method)
                logger.error(
                    "SQLite error in Database.%s — returning a safe default; "
                    "the watcher keeps running." % method,
                    method=method,
                    path=self.path,
                    error=str(e),
                )
            if self._recover_to_local_fallback():
                try:
                    return fn()
                except sqlite3.Error:
                    pass
            return default

    # ------------------------------------------------------------------- kv

    def kv_get(self, key: str, default: Any = None) -> Any:
        def _read():
            row = self.conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except ValueError:
                return default

        return self._run("kv_get", _read, default)

    def kv_set(self, key: str, value: Any) -> None:
        def _write():
            with self.conn:
                self.conn.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )

        self._run("kv_set", _write, None)

    # -------------------------------------------------------------- signals

    def signals_all(self) -> List[Dict]:
        def _read():
            rows = self.conn.execute(
                "SELECT * FROM signals ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]

        return self._run("signals_all", _read, [])

    def signal_upsert(self, signal: Dict) -> None:
        def _write():
            values = [signal.get(col) for col in SIGNAL_COLUMNS]
            placeholders = ", ".join("?" for _ in SIGNAL_COLUMNS)
            assignments = ", ".join(
                f"{col} = excluded.{col}" for col in SIGNAL_COLUMNS if col != "id"
            )
            with self.conn:
                self.conn.execute(
                    f"INSERT INTO signals ({', '.join(SIGNAL_COLUMNS)}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {assignments}",
                    values,
                )

        self._run("signal_upsert", _write, None)

    # --------------------------------------------------------------- trades

    def trade_insert(self, trade: Dict) -> None:
        def _write():
            values = [trade.get(col) for col in TRADE_COLUMNS]
            placeholders = ", ".join("?" for _ in TRADE_COLUMNS)
            with self.conn:
                self.conn.execute(
                    f"INSERT INTO trades ({', '.join(TRADE_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    values,
                )

        self._run("trade_insert", _write, None)

    def trades_by_batch(self, batch_id: str, status: str) -> List[Dict]:
        def _read():
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE batch_id = ? AND status = ?",
                (batch_id, status),
            ).fetchall()
            return [dict(row) for row in rows]

        return self._run("trades_by_batch", _read, [])

    def trades_by_status(self, status: str) -> List[Dict]:
        def _read():
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE status = ? ORDER BY close_time DESC",
                (status,),
            ).fetchall()
            return [dict(row) for row in rows]

        return self._run("trades_by_status", _read, [])

    def trade_set_status(self, trade_id: str, status: str) -> None:
        def _write():
            with self.conn:
                self.conn.execute(
                    "UPDATE trades SET status = ? WHERE id = ?", (status, trade_id)
                )

        self._run("trade_set_status", _write, None)

    def trade_delete(self, trade_id: str) -> None:
        def _write():
            with self.conn:
                self.conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))

        self._run("trade_delete", _write, None)

    def trades_delete_batch(self, batch_id: str, status: str) -> int:
        def _write():
            with self.conn:
                cur = self.conn.execute(
                    "DELETE FROM trades WHERE batch_id = ? AND status = ?",
                    (batch_id, status),
                )
                return cur.rowcount or 0

        return self._run("trades_delete_batch", _write, 0)


def migrate_legacy_json(
    db: Database, state_file: str, journal_file: str
) -> None:
    """One-time import of the old JSON files into SQLite (files kept as .bak)."""
    try:
        pairs_already_set = db.kv_get("pairs") is not None
    except sqlite3.Error as e:
        # kv_get itself no longer raises (see Database._run), but this stays
        # defensive on its own: treat an unreadable gate as "already set"
        # rather than risk re-importing over good state.
        logger.warning("Could not check existing pairs before migration", error=str(e))
        pairs_already_set = True

    if not pairs_already_set and os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in ("pairs", "last_setup", "last_digest_date", "news_warned"):
                if key in data:
                    db.kv_set(key, data[key])
            os.replace(state_file, state_file + ".bak")
            logger.info("Migrated legacy state file into SQLite", file=state_file)
        except (OSError, ValueError) as e:
            logger.warning("State migration failed", error=str(e))

    if not db.signals_all() and os.path.exists(journal_file):
        try:
            with open(journal_file, "r", encoding="utf-8") as f:
                signals = json.load(f)
            for signal in signals:
                db.signal_upsert({col: signal.get(col) for col in SIGNAL_COLUMNS})
            os.replace(journal_file, journal_file + ".bak")
            logger.info(
                "Migrated legacy journal into SQLite",
                file=journal_file,
                signals=len(signals),
            )
        except (OSError, ValueError) as e:
            logger.warning("Journal migration failed", error=str(e))


def open_database(path: str) -> Optional[Database]:
    try:
        return Database(path)
    except sqlite3.Error as e:
        logger.error("Failed to open SQLite database", path=path, error=str(e))
        raise
