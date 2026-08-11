"""In-memory book of current Pre-Market Plans (auto-plan, spec 2026-08-11).

The watcher fills it from the 07:55/13:55 snapshot fetches and from candles
each cycle already fetched; the `aplan_*` buttons and the plan-zone alert
read it. Nothing here talks to the network or the DB — a restart simply
leaves the book empty until the next cycle refills it.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from app.services.smc.models import Candle
from app.services.smc.plan import PairPlan, PlanScenario


@dataclass
class PlanEntry:
    plan: PairPlan
    # the exact candles the plan was built from: {"h4": [...], "h1": [...],
    # "m5": [...]} — the chart and the live-status line reuse them so a
    # button press costs zero API calls
    data: Dict[str, List[Candle]]
    as_of: str  # Prague HH:MM of the last closed M5 candle


def plan_fingerprint(plan: PairPlan) -> str:
    """Material identity of a plan: the scenario set (direction, zone
    bounds, speculative flag) plus the blocker stage. Price drift and RR
    drift are deliberately NOT material — the summary would otherwise be
    edited every five minutes."""
    scenarios = sorted(
        (s.direction.value, s.zone_bottom, s.zone_top, s.speculative)
        for s in plan.scenarios
    )
    return repr((scenarios, plan.blocker))


class PlanBook:
    def __init__(self) -> None:
        self._entries: Dict[str, PlanEntry] = {}

    def update(self, key: str, entry: PlanEntry) -> None:
        self._entries[key.upper()] = entry

    def get(self, key: str) -> Optional[PlanEntry]:
        return self._entries.get(key.upper())

    def scenario_for_touch(
        self, key: str, low: float, high: float
    ) -> Optional[PlanScenario]:
        """First scenario whose zone overlaps the candle range [low, high]."""
        entry = self.get(key)
        if entry is None:
            return None
        for s in entry.plan.scenarios:
            if s.zone_bottom <= high and low <= s.zone_top:
                return s
        return None

    def has_zone(
        self, key: str, low: float, high: float, direction: Optional[str] = None
    ) -> bool:
        """Whether the current plan still names a zone overlapping
        [low, high] (same direction when given) — the episode-reset check."""
        entry = self.get(key)
        if entry is None:
            return False
        for s in entry.plan.scenarios:
            if direction and s.direction.value != direction:
                continue
            if s.zone_bottom <= high and low <= s.zone_top:
                return True
        return False
