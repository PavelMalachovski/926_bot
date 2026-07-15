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
]


class Database:
    """Thin wrapper over sqlite3 with a signals table and a kv store."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
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
                    checked_until TEXT
                )
                """
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)"
            )

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
