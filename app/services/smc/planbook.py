"""In-memory book of current Pre-Market Plans (auto-plan, spec 2026-08-11).

The watcher fills it from the 08:05/14:05 snapshot fetches and from candles
each cycle already fetched; the `aplan_*` buttons and the plan-zone alert
read it. Nothing here talks to the network or the DB — a restart simply
leaves the book empty until the next cycle refills it.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.services.smc.models import AnalysisResult, Candle
from app.services.smc.plan import PairPlan, PlanScenario


@dataclass
class PlanEntry:
    plan: PairPlan
    # the exact candles the plan was built from: {"h4": [...], "h1": [...],
    # "m5": [...]} — the chart and the live-status line reuse them so a
    # button press costs zero API calls
    data: Dict[str, List[Candle]]
    as_of: str  # Prague HH:MM of the last closed M5 candle
    # D25 (owner decision 2026-09-05): the Strategy audit, computed WITH the
    # plan — the pure checklist result on the same candles and the pending
    # (limit) entries priced off it (`pending.build_pending`). The aplan_*
    # button only delivers it, so a press costs zero API calls; None when
    # the audit could not be computed (the button then falls back to the
    # plan text).
    result: Optional[AnalysisResult] = None
    audit: Optional[Any] = None  # pending.PendingAnalysis
    # D26: Claude's second opinion on the audit, taken with the 08:05/14:05
    # snapshot (and /plan); the per-cycle recompute carries it forward
    # rather than paying for a new one every five minutes. None = no read.
    ai_read: Optional[Any] = None  # ai_read.AIRead


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


# --------------------------------------------------------- primary plan
#
# Owner decision 2026-09-10 ("план — главный"): the /plan the owner pressed
# is the primary picture for the pair, and the 🚨 alert is its continuation.
# The snapshot below is what survives in the kv store (`state.primary_plan`)
# — JSON-shaped on purpose — and `match_primary_plan` is how the alert
# checks the setup the engine just formed against it. Detector mode is
# untouched: a mismatch is LABELLED on the card, never suppressed.


@dataclass
class PlanMatch:
    """The stored plan as the alert reads it."""

    date: str  # Prague YYYY-MM-DD the plan was built
    as_of: str  # Prague HH:MM of its last closed M5 candle
    direction: Optional[str]  # "long" / "short" / None (no direction then)
    matches: bool  # same direction and an overlapping zone
    zones: List[Tuple[float, float, Optional[str]]] = field(default_factory=list)
    main: Optional[float] = None  # the MAIN pending entry price, if priced
    deep: Optional[float] = None  # the DEEP pending entry price, if priced
    ai_stance: Optional[str] = None
    ai_entry: Optional[str] = None
    ai_confidence: Optional[int] = None
    ai_read: str = ""
    ai_risks: List[str] = field(default_factory=list)

    @property
    def when(self) -> str:
        """'10.09 14:05' — the plan's own timestamp on the card."""
        try:
            d = self.date.split("-")
            return f"{d[2]}.{d[1]} {self.as_of}"
        except (IndexError, AttributeError):
            return self.as_of


def primary_plan_snapshot(entry: PlanEntry, date: str) -> dict:
    """The kv-store shape of a fresh /plan: zones, the MAIN/DEEP prices and
    Claude's read. Everything the alert and the next AI read need, nothing
    that cannot be JSON."""
    audit = entry.audit
    direction = None
    if audit is not None and audit.direction is not None:
        direction = audit.direction.value
    entries = []
    if audit is not None:
        for e in audit.entries:
            entries.append({
                "role": e.role, "label": e.label, "entry": e.entry,
                "stop_loss": e.stop_loss,
                "tp1": e.targets[0].price if e.targets else None,
            })
    ai = None
    if entry.ai_read is not None:
        r = entry.ai_read
        ai = {
            "stance": r.stance, "preferred_entry": r.preferred_entry,
            "confidence": int(r.confidence), "read": r.read,
            "risks": list(r.risks), "model": r.model,
        }
    return {
        "date": date,
        "as_of": entry.as_of,
        "direction": direction,
        "zones": [list(z) for z in entry.plan.zones_shown()],
        "entries": entries,
        "ai": ai,
    }


def match_primary_plan(stored: Optional[dict], result: AnalysisResult) -> Optional[PlanMatch]:
    """Read a stored snapshot against the setup the engine just formed.

    None when there is no plan for the pair. Otherwise `matches` is True
    only when the setup trades the plan's direction AND its H1 zone
    overlaps a zone the plan showed — the same overlap rule
    `WatcherState.zone_was_planned` uses (a zone drifts a little as pivots
    confirm; any overlap in the same direction is the same idea). A
    malformed row reads as no plan rather than raising on the alert path.
    """
    if not isinstance(stored, dict):
        return None
    try:
        zones: List[Tuple[float, float, Optional[str]]] = []
        for z in stored.get("zones") or []:
            if isinstance(z, (list, tuple)) and len(z) >= 2:
                zones.append((
                    float(z[0]), float(z[1]),
                    str(z[2]) if len(z) > 2 and z[2] is not None else None,
                ))
        direction = stored.get("direction")
        setup_dir = result.setup.direction.value if result.setup else None
        matches = False
        if result.h1_zone is not None and setup_dir is not None:
            low = min(result.h1_zone.bottom, result.h1_zone.top)
            high = max(result.h1_zone.bottom, result.h1_zone.top)
            for z_low, z_high, z_dir in zones:
                if z_dir and z_dir != setup_dir:
                    continue
                if direction and z_dir is None and direction != setup_dir:
                    continue
                if min(z_low, z_high) <= high and low <= max(z_low, z_high):
                    matches = True
                    break
        main = deep = None
        for e in stored.get("entries") or []:
            if e.get("role") == "main":
                main = float(e["entry"])
            elif e.get("role") == "deep":
                deep = float(e["entry"])
        ai = stored.get("ai") or {}
        return PlanMatch(
            date=str(stored.get("date") or ""),
            as_of=str(stored.get("as_of") or ""),
            direction=direction,
            matches=matches,
            zones=zones,
            main=main,
            deep=deep,
            ai_stance=ai.get("stance"),
            ai_entry=ai.get("preferred_entry"),
            ai_confidence=int(ai["confidence"]) if ai.get("confidence") is not None else None,
            ai_read=str(ai.get("read") or ""),
            ai_risks=[str(r) for r in (ai.get("risks") or [])],
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
