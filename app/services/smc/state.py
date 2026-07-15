"""Persistent watcher state (pairs, dedup keys) backed by SQLite."""

from typing import Dict, List

import structlog

from app.services.smc.db import Database
from app.services.smc.instruments import DEFAULT_PAIRS, INSTRUMENTS

logger = structlog.get_logger(__name__)


class WatcherState:
    """Runtime state shared by the scheduler and the command bot."""

    def __init__(self, db: Database):
        self.db = db
        pairs = db.kv_get("pairs") or []
        self.pairs: List[str] = [p for p in pairs if p in INSTRUMENTS] or list(
            DEFAULT_PAIRS
        )
        self.last_setup: Dict[str, str] = db.kv_get("last_setup") or {}
        self.last_digest_date: str = db.kv_get("last_digest_date") or ""
        self.news_warned: Dict[str, str] = db.kv_get("news_warned") or {}

    def save(self) -> None:
        self.db.kv_set("pairs", self.pairs)
        self.db.kv_set("last_setup", self.last_setup)
        self.db.kv_set("last_digest_date", self.last_digest_date)
        self.db.kv_set("news_warned", self.news_warned)

    def toggle_pair(self, key: str) -> bool:
        """Toggle a pair on/off. Returns True if the pair is now enabled."""
        key = key.upper()
        if key not in INSTRUMENTS:
            raise KeyError(key)
        if key in self.pairs:
            self.pairs.remove(key)
            enabled = False
        else:
            # keep the strategy's instrument order
            self.pairs = [k for k in INSTRUMENTS if k in self.pairs or k == key]
            enabled = True
        self.save()
        return enabled
