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


class Database:
    """Thin wrapper over sqlite3 with a signals table and a kv store."""

    FALLBACK_PATH = ".smc_watcher.db"

    def __init__(self, path: str):
        self.path = path
        self.conn = self._connect(path)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
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
                    alert_text TEXT
                )
                """
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)"
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
                "CREATE INDEX IF NOT EXISTS idx_trades_batch ON trades(batch_id)"
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
            ):
                if column not in existing:
                    self.conn.execute(
                        f"ALTER TABLE signals ADD COLUMN {column} {sql_type}"
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

    # ------------------------------------------------------------------- kv

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # -------------------------------------------------------------- signals

    def signals_all(self) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM signals ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def signal_upsert(self, signal: Dict) -> None:
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

    # --------------------------------------------------------------- trades

    def trade_insert(self, trade: Dict) -> None:
        values = [trade.get(col) for col in TRADE_COLUMNS]
        placeholders = ", ".join("?" for _ in TRADE_COLUMNS)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO trades ({', '.join(TRADE_COLUMNS)}) "
                f"VALUES ({placeholders})",
                values,
            )

    def trades_by_batch(self, batch_id: str, status: str) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE batch_id = ? AND status = ?",
            (batch_id, status),
        ).fetchall()
        return [dict(row) for row in rows]

    def trades_by_status(self, status: str) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status = ? ORDER BY close_time DESC",
            (status,),
        ).fetchall()
        return [dict(row) for row in rows]

    def trade_set_status(self, trade_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE trades SET status = ? WHERE id = ?", (status, trade_id)
            )

    def trade_delete(self, trade_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))

    def trades_delete_batch(self, batch_id: str, status: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM trades WHERE batch_id = ? AND status = ?",
                (batch_id, status),
            )
            return cur.rowcount or 0


def migrate_legacy_json(
    db: Database, state_file: str, journal_file: str
) -> None:
    """One-time import of the old JSON files into SQLite (files kept as .bak)."""
    if db.kv_get("pairs") is None and os.path.exists(state_file):
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
