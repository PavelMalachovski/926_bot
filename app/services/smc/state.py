"""Persistent watcher state: enabled pairs and reported setups."""

import json
import os
from typing import Dict, List

import structlog

from app.services.smc.instruments import DEFAULT_PAIRS, INSTRUMENTS

logger = structlog.get_logger(__name__)


class WatcherState:
    """Small JSON-backed state shared by the scheduler and the command bot."""

    def __init__(self, path: str):
        self.path = path
        self.pairs: List[str] = list(DEFAULT_PAIRS)
        self.last_setup: Dict[str, str] = {}  # pair -> fingerprint
        self.last_digest_date: str = ""  # Prague date of the last morning digest
        self.news_warned: Dict[str, str] = {}  # Rule 0.4 dedup: key -> iso time
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        pairs = [p for p in data.get("pairs", []) if p in INSTRUMENTS]
        if pairs:
            self.pairs = pairs
        self.last_setup = dict(data.get("last_setup", {}))
        self.last_digest_date = data.get("last_digest_date", "")
        self.news_warned = dict(data.get("news_warned", {}))

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "pairs": self.pairs,
                        "last_setup": self.last_setup,
                        "last_digest_date": self.last_digest_date,
                        "news_warned": self.news_warned,
                    },
                    f,
                )
        except OSError as e:
            logger.warning("Failed to persist watcher state", error=str(e))

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
