"""Persistent watcher state (pairs, dedup keys) backed by SQLite."""

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import structlog

from app.services.smc.db import Database
from app.services.smc.instruments import DEFAULT_PAIRS, INSTRUMENTS
from app.services.smc.sessions import to_prague

logger = structlog.get_logger(__name__)

# One stored plan zone: bottom, top and the direction it was projected in
# ("long"/"short", or None for a zone stored without one). JSON has no tuple,
# so the kv store hands these back as lists — every read runs them through
# `_normalise_zone`, which accepts either shape and returns this one.
PlanZone = Tuple[float, float, Optional[str]]

# Global notification level (owner decision 2026-08-12, Phase 2 Task 4b):
# replaces the retired per-pair /strategy picker. "all" = star loud + quiet
# regular (today's default two-tier behavior); "star" = star only, regular
# setups are journal-recorded and logged but not sent; "mute" = no setup
# alerts at all. This affects SETUP alerts only -- the 07:45 digest,
# plan-zone alerts and Rule 0.4/9 warnings keep flowing regardless.
NOTIFY_LEVELS: Tuple[str, ...] = ("all", "star", "mute")


class WatcherState:
    """Runtime state shared by the scheduler and the command bot."""

    def __init__(self, db: Database):
        self.db = db
        # None means "never set" -> apply the env/default pairs. A stored
        # list, even an empty one, is a deliberate choice (e.g. the owner
        # disabled every pair via /pairs) and must not resurrect the
        # defaults on the next restart (review finding 2026-08-11, MEDIUM).
        raw_pairs = db.kv_get("pairs")
        if raw_pairs is None:
            self.pairs: List[str] = list(DEFAULT_PAIRS)
        else:
            self.pairs = [p for p in raw_pairs if p in INSTRUMENTS]
        self.last_setup: Dict[str, str] = db.kv_get("last_setup") or {}
        self.last_digest_date: str = db.kv_get("last_digest_date") or ""
        self.news_warned: Dict[str, str] = db.kv_get("news_warned") or {}
        # pair -> ISO timestamp of the last "data source failed" warning,
        # throttled to one per pair per hour (see Watcher._warn_data_source_failure)
        self.source_warned: Dict[str, str] = db.kv_get("source_warned") or {}
        self.day_stop_notified: str = db.kv_get("day_stop_notified") or ""
        # pair -> ISO expiry: no new alerts for the pair until then (Took it)
        self.pair_cooldown: Dict[str, str] = db.kv_get("pair_cooldown") or {}
        # pair -> [zone_bottom, zone_top, direction, prague_date] of the
        # plan zone already alerted this episode (reset when price leaves,
        # the plan drops the zone, or the day changes). Legacy bool values
        # from the pre-auto-plan ping are dropped: they carry no bounds to
        # compare against. Anything not a 4-element list (legacy bool, a
        # partially-written record) is dropped the same way.
        raw_pinged = db.kv_get("zone_pinged") or {}
        self.zone_pinged: Dict[str, list] = {
            k: v for k, v in raw_pinged.items()
            if isinstance(v, list) and len(v) == 4
        }
        self.pair_profile: Dict[str, str] = db.kv_get("pair_profile") or {}
        # auto-plan snapshot gate: slot "HH:MM" -> Prague date it fired
        self.auto_plan_sent: Dict[str, str] = db.kv_get("auto_plan_sent") or {}
        # the latest plan-summary message, so silent edits survive restarts:
        # {"message_id", "slot", "date", "fingerprints": {pair: fingerprint}}
        self.plan_summary: dict = db.kv_get("plan_summary") or {}
        # global mute: scheduler cycles are no-ops until /resume
        self.paused: bool = bool(db.kv_get("paused") or False)
        # pair -> [[bottom, top, direction], ...] shown by the last /plan run,
        # kept for one Prague day so an alert can say whether its zone was in
        # the morning picture (spec 2026-08-06 §6). A pair present with an
        # empty list means "a plan ran and showed nothing" — that is not the
        # same as no plan at all.
        self.plan_zones: Dict[str, List[PlanZone]] = db.kv_get("plan_zones") or {}
        self.plan_zones_date: str = db.kv_get("plan_zones_date") or ""
        # global setup-alert level: "all" | "star" | "mute" (Task 4b). A
        # stored value outside the known set (should not happen -- only
        # `set_notify_level` writes it, and it validates) falls back to the
        # default rather than propagating garbage into the gate check.
        raw_notify_level = db.kv_get("notify_level")
        self.notify_level: str = (
            raw_notify_level if raw_notify_level in NOTIFY_LEVELS else "all"
        )

    def save(self) -> None:
        self.db.kv_set("pairs", self.pairs)
        self.db.kv_set("last_setup", self.last_setup)
        self.db.kv_set("last_digest_date", self.last_digest_date)
        self.db.kv_set("news_warned", self.news_warned)
        self.db.kv_set("source_warned", self.source_warned)
        self.db.kv_set("day_stop_notified", self.day_stop_notified)
        self.db.kv_set("pair_cooldown", self.pair_cooldown)
        self.db.kv_set("zone_pinged", self.zone_pinged)
        self.db.kv_set("pair_profile", self.pair_profile)
        self.db.kv_set("paused", self.paused)
        self.db.kv_set("plan_zones", self.plan_zones)
        self.db.kv_set("plan_zones_date", self.plan_zones_date)
        self.db.kv_set("auto_plan_sent", self.auto_plan_sent)
        self.db.kv_set("plan_summary", self.plan_summary)
        self.db.kv_set("notify_level", self.notify_level)

    # ------------------------------------------------------------ plan zones

    @staticmethod
    def _prague_day(now: Optional[datetime] = None) -> str:
        return to_prague(now or datetime.now(tz=timezone.utc)).date().isoformat()

    @staticmethod
    def _normalise_zone(zone: Sequence) -> PlanZone:
        """(bottom, top[, direction]) -> (low, high, direction or None)."""
        bottom, top = float(zone[0]), float(zone[1])
        direction = None
        if len(zone) > 2 and zone[2] is not None:
            # accept a Direction enum as readily as its value
            direction = str(getattr(zone[2], "value", zone[2]))
        return min(bottom, top), max(bottom, top), direction

    def remember_plan_zones(
        self, key: str, zones: Iterable[Sequence], now: Optional[datetime] = None
    ) -> None:
        """Store the zones a /plan run showed for the current Prague day.

        A run on a new day replaces the whole previous set rather than adding
        to it: yesterday's zones say nothing about today's alerts.
        """
        today = self._prague_day(now)
        if self.plan_zones_date != today:
            self.plan_zones = {}
            self.plan_zones_date = today
        self.plan_zones[key.upper()] = [self._normalise_zone(z) for z in zones]
        self.save()

    def has_plan_today(self, key: str, now: Optional[datetime] = None) -> bool:
        """True when a /plan for this pair was run (and looked at) today."""
        if self.plan_zones_date != self._prague_day(now):
            return False
        return key.upper() in self.plan_zones

    def zone_was_planned(
        self,
        key: str,
        bottom: float,
        top: float,
        direction: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Whether a live zone is one this morning's plan already showed.

        Matching is by overlap, not equality: an H1 zone shifts slightly as
        new pivots confirm, so any overlap in the same direction is the same
        trading idea. A stored zone with no direction matches either side —
        the plan projects demand below price and supply above it, so its two
        speculative zones can never overlap anyway.
        """
        if not self.has_plan_today(key, now):
            return False
        wanted = str(getattr(direction, "value", direction)) if direction else None
        low, high = min(bottom, top), max(bottom, top)
        for stored in self.plan_zones.get(key.upper(), []):
            z_low, z_high, z_dir = self._normalise_zone(stored)
            if wanted and z_dir and z_dir != wanted:
                continue
            if z_low <= high and low <= z_high:
                return True
        return False

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.save()

    def set_notify_level(self, level: str) -> None:
        """Set the global setup-alert level (`/notify`, Task 4b).

        Rejects anything outside `NOTIFY_LEVELS` rather than silently
        coercing it -- a caller passing something other than the three
        defined levels has a bug, not a preference the state layer should
        paper over.
        """
        if level not in NOTIFY_LEVELS:
            raise ValueError(f"Unknown notify level: {level!r}")
        self.notify_level = level
        self.save()

    def set_profile(self, key: str, profile_key: str) -> None:
        """Set a pair's strategy profile and clear its dedup so the new
        profile's first alert is not suppressed by a stale fingerprint."""
        key = key.upper()
        self.pair_profile[key] = profile_key
        self.last_setup.pop(key, None)
        self.zone_pinged.pop(key, None)
        self.save()

    def set_all_profiles(self, profile_key: str) -> None:
        """Set every known pair's profile in one save (used by 'all pairs')."""
        for key in INSTRUMENTS:
            self.pair_profile[key] = profile_key
            self.last_setup.pop(key, None)
            self.zone_pinged.pop(key, None)
        self.save()

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
