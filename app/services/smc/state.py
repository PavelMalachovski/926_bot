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
        self.day_stop_notified: str = db.kv_get("day_stop_notified") or ""
        # pair -> ISO expiry: no new alerts for the pair until then (Took it)
        self.pair_cooldown: Dict[str, str] = db.kv_get("pair_cooldown") or {}
        # pair -> bool: whether the "price reached the zone" ping was sent for
        # the current in-zone episode (reset when price leaves the zone)
        self.zone_pinged: Dict[str, bool] = db.kv_get("zone_pinged") or {}

    def save(self) -> None:
        self.db.kv_set("pairs", self.pairs)
        self.db.kv_set("last_setup", self.last_setup)
        self.db.kv_set("last_digest_date", self.last_digest_date)
        self.db.kv_set("news_warned", self.news_warned)
        self.db.kv_set("day_stop_notified", self.day_stop_notified)
        self.db.kv_set("pair_cooldown", self.pair_cooldown)
        self.db.kv_set("zone_pinged", self.zone_pinged)

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
