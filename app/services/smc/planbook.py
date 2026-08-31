"""In-memory book of current Pre-Market Plans (auto-plan, spec 2026-08-11).

The watcher fills it from the 08:05/14:05 snapshot fetches and from candles
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


def plan_snapshot(plan: PairPlan) -> dict:
    """`plan_fingerprint`'s material identity in a DIFFABLE shape.

    The fingerprint is a repr — perfect for "did anything change", useless
    for "what changed". Owner request 2026-08-31: a plan correction has to
    reach Telegram as its own message naming what moved, so the same facts
    are also stored structurally (and JSON-safely, since this rides in the
    kv store next to the fingerprints).

    Deliberately the same fields as the fingerprint, no more: price and RR
    drift are not material, or every five-minute recompute would announce
    itself.
    """
    return {
        "scenarios": sorted(
            [s.direction.value, s.zone_bottom, s.zone_top, bool(s.speculative)]
            for s in plan.scenarios
        ),
        "blocker": plan.blocker,
    }


def _by_direction(snapshot: dict) -> Dict[str, List[list]]:
    grouped: Dict[str, List[list]] = {}
    for row in snapshot.get("scenarios") or []:
        grouped.setdefault(str(row[0]), []).append(list(row))
    return grouped


def describe_plan_changes(old: dict, new: dict, decimals: int = 2) -> List[str]:
    """Plain lines naming what moved between two `plan_snapshot`s.

    Empty when nothing material differs. The caller escapes and sends; this
    only decides what is worth saying, so it stays unit-testable without a
    notifier. Scenarios are matched by DIRECTION — a plan carries at most
    one bracket per side, and when a zone shifts the owner wants to read it
    as "the long zone moved", not "one scenario vanished and another
    appeared".
    """
    lines: List[str] = []
    old_dirs, new_dirs = _by_direction(old), _by_direction(new)
    for side in sorted(set(old_dirs) | set(new_dirs)):
        was, now = old_dirs.get(side, []), new_dirs.get(side, [])
        label = side.upper()
        if was and not now:
            bounds = ", ".join(_band(r, decimals) for r in was)
            lines.append(f"{label} scenario dropped ({bounds})")
        elif now and not was:
            bounds = ", ".join(_band(r, decimals) for r in now)
            lines.append(f"{label} scenario added: {bounds}")
        elif was != now:
            if len(was) == 1 and len(now) == 1:
                lines.append(
                    f"{label} zone moved {_band(was[0], decimals)} → "
                    f"{_band(now[0], decimals)}"
                )
                if bool(was[0][3]) != bool(now[0][3]):
                    lines.append(
                        f"{label} is now "
                        + ("speculative" if now[0][3] else "confirmed")
                    )
            else:
                lines.append(
                    f"{label} zones changed: "
                    + ", ".join(_band(r, decimals) for r in now)
                )
    if old.get("blocker") != new.get("blocker"):
        if new.get("blocker"):
            lines.append(f"Now waiting: {new['blocker']}")
        else:
            lines.append("Blocker cleared — the plan is live")
    return lines


def _band(row: list, decimals: int) -> str:
    return f"{row[1]:.{decimals}f}–{row[2]:.{decimals}f}"


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
