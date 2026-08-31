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


def _day_extremes(candles: List[Candle], day) -> Optional[Tuple[float, float]]:
    """(low, high) over the Prague calendar day `day`, None if no candles."""
    rows = [c for c in candles if _prague_date(c.timestamp) == day]
    if not rows:
        return None
    return min(c.low for c in rows), max(c.high for c in rows)


def _asia_extremes(candles: List[Candle], day) -> Optional[Tuple[float, float]]:
    """(low, high) of 00:00-08:00 Prague on `day` (pre-session accumulation)."""
    rows = [
        c for c in candles
        if _prague_date(c.timestamp) == day
        and c.timestamp.astimezone(PRAGUE).hour < 8
    ]
    if not rows:
        return None
    return min(c.low for c in rows), max(c.high for c in rows)


def _session_candles(
    m5: List[Candle], h1: List[Candle], as_of
) -> List[Candle]:
    """The candles the day-level pools (PDH/PDL, Asia) are read off.

    M5 alone truncated them. Production fetches 400 M5 candles — 33.3 hours —
    so a check at 18:00 Prague saw yesterday only from 08:40 onwards, missing
    the whole Asian session and the London open. Yesterday's low then read
    HIGHER than it really was (and its high LOWER), so any dip under that
    invented level was scored as "PDL swept" and handed out a ⭐ nothing had
    earned. H1 carries 400 candles ≈ 16 days and its per-candle extremes are
    exact, so adding it makes the day complete; M5 stays in the list for the
    fixtures (and cold starts) where H1 is empty, and for a day H1 does not
    reach back to.

    Everything is cut at `as_of` — the zone touch — so this can never see a
    pool the excursion itself printed. In production the cut changes nothing:
    yesterday's range and today's Asia range are both complete before the
    session that produces the touch even opens.
    """
    rows = list(m5) + list(h1)
    if as_of is not None:
        rows = [c for c in rows if c.timestamp <= as_of]
    return rows


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
    touch (m5[:touch_idx] and the H1 slice up to the touch candle), so the
    excursion itself cannot create — or delete — the pool it sweeps.
    """
    window = m5[touch_idx:choch_idx + 1]
    if not window:
        return None
    is_long = direction == Direction.LONG
    extreme = min(c.low for c in window) if is_long else max(c.high for c in window)

    def crossed(level: float) -> bool:
        return extreme < level if is_long else extreme > level

    as_of = m5[touch_idx].timestamp if 0 <= touch_idx < len(m5) else None
    day_rows = _session_candles(m5, h1, as_of)
    today = _prague_date(ts_utc)
    prev_days = sorted(
        {d for c in day_rows if (d := _prague_date(c.timestamp)) < today},
        reverse=True,
    )
    if prev_days:
        pd = _day_extremes(day_rows, prev_days[0])
        if pd is not None:
            pdl, pdh = pd
            if is_long and crossed(pdl):
                return "PDL"
            if not is_long and crossed(pdh):
                return "PDH"
    asia = _asia_extremes(day_rows, today)
    if asia is not None:
        al, ah = asia
        if is_long and crossed(al):
            return "AsiaL"
        if not is_long and crossed(ah):
            return "AsiaH"

    # The H1 half must be sliced exactly like the M5 half. `find_liquidity`
    # drops levels a LATER candle has already taken, so handing it the whole
    # H1 array deletes the very low/high this excursion swept — the search
    # below then looks for a crossed level in a list it was removed from, and
    # a genuine H1 sweep goes unnamed. It bites whenever the excursion spans a
    # closed H1 candle, i.e. whenever price sits in the zone for an hour or
    # more, which is the normal case.
    h1_pit = (
        [c for c in h1 if c.timestamp <= as_of] if as_of is not None else list(h1)
    )
    levels = (
        find_liquidity(m5[:touch_idx], "M5", tolerance)
        + find_liquidity(h1_pit, "H1", tolerance)
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
    has_imbalance: bool = True,
) -> TierVerdict:
    """Star iff room is unmeasurable-or-wide-enough, a pool was swept, pd is
    ok-or-unmeasurable, the setup is not stale, H4/H1 do not point opposite
    ways (owner decision D6, 2026-08-16), and the impulse left a valid M5
    imbalance behind it (owner decision D22, 2026-08-30).

    `trend_disagrees` defaults to False so a caller that does not measure it
    is treated as "nothing is arguing" rather than silently losing the star.
    `has_imbalance` defaults to True for the same reason: D22 moved the gap
    out of the setup definition and into this verdict, so a caller that does
    not report one is read as "not the thing being judged here" rather than
    silently losing the star.
    Detector mode is unchanged: a setup missing any of these is still
    announced, just not as a ⭐.
    """
    checks = (
        ("room", room is None or room >= min_room_r),
        ("sweep", sweep is not None),
        ("pd", pd in ("ok", None)),
        ("stale", not stale),
        ("trend", not trend_disagrees),
        ("imbalance", has_imbalance),
    )
    missed = [name for name, ok in checks if not ok]
    return TierVerdict(star=not missed, missed=missed)
