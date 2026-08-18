"""Sniper tier primitives: room/sweep/premium-discount and the star verdict.

Phase 2 of the sniper redesign (docs/superpowers/specs/2026-08-12-sniper-
redesign-design.md, "Phase 2 locked decisions", owner 2026-08-12) replaces
Rule 7's RR-to-liquidity gate with a three-condition star tier, checked
*after* a setup has already fully formed (detector mode, CLAUDE.md, is
unchanged: the alert always fires either way). A setup earns the star when
all of:

1. **Room** (`room_r`): the nearest H1/H4-only liquidity pool ahead of the
   entry sits >= `MIN_ROOM_R` risk units away, or no such pool exists at all
   (an unmeasurable condition passes, per the locked decision).
2. **Sweep** (`sweep_label`): the touch..CHoCH excursion took a concrete
   liquidity pool — PDH/PDL, Asia high/low, EQH/EQL, or an unswept swing.
3. **Premium/Discount** (`pd_state`): the entry sits on the correct side of
   the last confirmed H1 dealing range's midpoint, or the range itself
   cannot be judged (no confirmed H1 swings yet) — again, unmeasurable
   passes.
4. **Trend agreement** (`engine.trends_disagree`, owner decision D6,
   2026-08-16): H4 and H1 must not point opposite ways. An H4 FLAT is not a
   disagreement (that is the H1-fallback direction case, owner decision
   2026-08-06) and neither is an H1 FLAT under a trending H4.

`classify` also takes `stale` (an already-computed staleness flag from the
caller — Task 2) as a fifth, independent gate: even a clean room/sweep/pd/
trend read does not earn the star if the setup is stale.

Ported from the validated Phase 1 prototype
(C:\\temp\\926_bot_data\\scripts\\sn_rules.py, replayed on a year of data,
see docs/superpowers/specs/2026-08-12-sniper-redesign-design.md "5.
Validation") with `room_r`/`classify` added per the Phase 2 design (the
`room_r` computation itself is ported from
C:\\temp\\926_bot_data\\scripts\\sn_run.py).
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.services.smc.liquidity import find_liquidity, nearest_liquidity
from app.services.smc.models import Candle, Direction
from app.services.smc.structure import find_pivots

PRAGUE = ZoneInfo("Europe/Prague")

# Room check threshold (design doc Phase 2 locked decision 4, FVG-basis
# recalibration): the nearest H1/H4 pool ahead must sit at least this many risk
# units away, or not exist at all.
MIN_ROOM_R = 1.0


def _prague_date(ts_utc):
    return ts_utc.astimezone(PRAGUE).date()


def _day_extremes(m5: List[Candle], day) -> Optional[Tuple[float, float]]:
    """(low, high) over the Prague calendar day `day`, None if no candles."""
    rows = [c for c in m5 if _prague_date(c.timestamp) == day]
    if not rows:
        return None
    return min(c.low for c in rows), max(c.high for c in rows)


def _asia_extremes(m5: List[Candle], day) -> Optional[Tuple[float, float]]:
    """(low, high) of 00:00-08:00 Prague on `day` (pre-session accumulation)."""
    rows = [
        c for c in m5
        if _prague_date(c.timestamp) == day
        and c.timestamp.astimezone(PRAGUE).hour < 8
    ]
    if not rows:
        return None
    return min(c.low for c in rows), max(c.high for c in rows)


def sweep_label(
    m5: List[Candle],
    h1: List[Candle],
    direction: Direction,
    touch_idx: int,
    choch_idx: int,
    tolerance: float,
    ts_utc,
) -> Optional[str]:
    """Name of the pool swept by the touch..choch excursion, or None.

    A LONG's excursion sweeps LOW-side pools (wick below the level); a
    SHORT's sweeps HIGH-side pools. Priority: PDH/PDL > Asia > EQ pools
    (equal_count >= 2) > single unswept swing. Levels are taken as-of the
    touch (m5[:touch_idx] / the PIT H1 slice), so the excursion itself
    cannot create the pool it sweeps.
    """
    window = m5[touch_idx:choch_idx + 1]
    if not window:
        return None
    is_long = direction == Direction.LONG
    extreme = min(c.low for c in window) if is_long else max(c.high for c in window)

    def crossed(level: float) -> bool:
        return extreme < level if is_long else extreme > level

    today = _prague_date(ts_utc)
    prev_days = sorted(
        {d for c in m5 if (d := _prague_date(c.timestamp)) < today}, reverse=True
    )
    if prev_days:
        pd = _day_extremes(m5, prev_days[0])
        if pd is not None:
            pdl, pdh = pd
            if is_long and crossed(pdl):
                return "PDL"
            if not is_long and crossed(pdh):
                return "PDH"
    asia = _asia_extremes(m5, today)
    if asia is not None:
        al, ah = asia
        if is_long and crossed(al):
            return "AsiaL"
        if not is_long and crossed(ah):
            return "AsiaH"

    levels = (
        find_liquidity(m5[:touch_idx], "M5", tolerance)
        + find_liquidity(h1, "H1", tolerance)
    )
    side = [lv for lv in levels if lv.is_high != is_long and crossed(lv.price)]
    if not side:
        return None
    pools = [lv for lv in side if lv.equal_count >= 2]
    if pools:
        n = max(lv.equal_count for lv in pools)
        return (f"EQL({n})" if is_long else f"EQH({n})")
    return "swingL" if is_long else "swingH"


def dealing_range(h1: List[Candle]) -> Optional[Tuple[float, float]]:
    """(low, high) of the last confirmed H1 swing low and swing high."""
    pivots = find_pivots(h1)
    highs = [p for p in pivots if p.is_high]
    lows = [p for p in pivots if not p.is_high]
    if not highs or not lows:
        return None
    low, high = lows[-1].price, highs[-1].price
    if low >= high:
        return None
    return low, high


def pd_state(
    direction: Direction, entry: float, rng: Optional[Tuple[float, float]]
) -> Optional[str]:
    """'ok' when the entry is on the right side of equilibrium, 'bad' when
    not, None when no dealing range exists (condition can't be judged)."""
    if rng is None:
        return None
    mid = (rng[0] + rng[1]) / 2.0
    if direction == Direction.LONG:
        return "ok" if entry <= mid else "bad"
    return "ok" if entry >= mid else "bad"


def room_r(
    h1: List[Candle],
    h4: List[Candle],
    direction: Direction,
    entry: float,
    risk: float,
    tolerance: float,
) -> Optional[float]:
    """Distance from `entry` to the nearest H1/H4 pool ahead, in units of
    `risk`; None when no such pool exists (an unmeasurable, passing
    condition — see MIN_ROOM_R). M5 pools are excluded on purpose: they are
    the noise Rule 7 used to chase (design doc, "Room check")."""
    levels = find_liquidity(h1, "H1", tolerance) + find_liquidity(h4, "H4", tolerance)
    obstacle = nearest_liquidity(levels, direction, entry)
    if obstacle is None or not risk:
        return None
    return abs(obstacle.price - entry) / risk


@dataclass
class TierVerdict:
    """Whether a setup earns the star, and which conditions it missed."""

    star: bool
    missed: List[str] = field(default_factory=list)


def classify(
    room: Optional[float],
    sweep: Optional[str],
    pd: Optional[str],
    stale: bool,
    trend_disagrees: bool = False,
    min_room_r: float = MIN_ROOM_R,
) -> TierVerdict:
    """Star iff room is unmeasurable-or-wide-enough, a pool was swept, pd is
    ok-or-unmeasurable, the setup is not stale, and H4/H1 do not point
    opposite ways (owner decision D6, 2026-08-16).

    `trend_disagrees` defaults to False so a caller that does not measure it
    is treated as "nothing is arguing" rather than silently losing the star.
    Detector mode is unchanged: a disagreeing setup is still announced, just
    not as a ⭐.
    """
    checks = (
        ("room", room is None or room >= min_room_r),
        ("sweep", sweep is not None),
        ("pd", pd in ("ok", None)),
        ("stale", not stale),
        ("trend", not trend_disagrees),
    )
    missed = [name for name, ok in checks if not ok]
    return TierVerdict(star=not missed, missed=missed)
