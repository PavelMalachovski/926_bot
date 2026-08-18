"""Range detection from clustered pivots (spec §3.1, D7, D9).

A range, in this system, is a box price has traded inside for a while: a
ceiling made of two or more confirmed swing highs close enough together to
be "the same level", and a floor made of two or more confirmed swing lows
the same way. Because a range boundary is nothing more than a cluster of
equal(ish) highs or lows, its edges coincide with EQH/EQL liquidity pools
by construction (see liquidity.py) — this is the same liquidity hunt
applied to a box instead of a single swing.

Pure geometry: no network, no DB, no instrument/profile registry imports.
The caller supplies the raw `instrument.min_fvg` as `tolerance` (never the
profile-scaled value) — the same precedent `structure.find_h1_fvg_zone`
already set for this package.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from app.services.smc.models import Candle, Direction, Pivot, Zone
from app.services.smc.structure import find_pivots

# A band narrower than this multiple of tolerance is chop, not a range: the
# two boundaries would sit close enough that a single tick could touch both.
MIN_BAND_TOLERANCES = 3


@dataclass
class Range:
    """A price box bounded by a cluster of equal highs and equal lows."""

    top: float
    bottom: float
    touches_top: int
    touches_bottom: int
    broken: bool
    top_at: datetime      # timestamp of the most recent pivot in the top cluster
    bottom_at: datetime   # same for the bottom cluster
    # A wick closed back inside after piercing the boundary (D9), measured
    # over EVERY candle since that boundary's own latest pivot. It says the
    # level has been raided at some point, not that any particular setup did
    # the raiding — the ⭐ asks the same question of the setup's own
    # excursion instead (D13, see `boundary_swept`).
    swept_top: bool
    swept_bottom: bool
    # Timestamp of the first candle after BOTH boundaries' latest pivot — the
    # moment the box was fully printed, and the same anchor `broken` scans
    # from. `boundary_zone` hands it to the boundary Zone so an M5 excursion
    # is only counted once the range existed; see that function for why the
    # pivot's own timestamp would not do.
    printed_at: datetime


def detect_range(
    candles: List[Candle], tolerance: float, window: int = 120
) -> Optional[Range]:
    """Find the range the last `window` candles are trading inside, if any.

    Clusters confirmed pivots (`structure.find_pivots`) by price: the
    largest same-side cluster within `tolerance` of itself becomes a
    boundary (mean price, latest pivot's timestamp), ties broken by
    recency. Both boundaries need at least two touches to count as a
    boundary rather than a single unrepeated swing, and the box must be at
    least `MIN_BAND_TOLERANCES * tolerance` wide, or there is no range
    worth trading — just chop.
    """
    windowed = candles[-window:]
    if not windowed:
        return None

    pivots = find_pivots(windowed)
    highs = [p for p in pivots if p.is_high]
    lows = [p for p in pivots if not p.is_high]

    top_cluster = _largest_cluster(highs, tolerance)
    bottom_cluster = _largest_cluster(lows, tolerance)
    if top_cluster is None or bottom_cluster is None:
        return None
    if len(top_cluster) < 2 or len(bottom_cluster) < 2:
        return None

    top = sum(p.price for p in top_cluster) / len(top_cluster)
    bottom = sum(p.price for p in bottom_cluster) / len(bottom_cluster)
    if top - bottom < MIN_BAND_TOLERANCES * tolerance:
        return None

    top_latest = max(top_cluster, key=lambda p: p.index)
    bottom_latest = max(bottom_cluster, key=lambda p: p.index)

    # The anchors differ on purpose: `broken` only makes sense once the box
    # is fully formed, so it scans from after BOTH boundaries' last touch —
    # a body close beyond one edge before the other edge even existed isn't
    # a break of this range. Each `swept_*` flag only needs its OWN
    # boundary to exist, so it scans from right after that boundary's own
    # latest pivot.
    after = max(top_latest.index, bottom_latest.index)
    broken = _is_broken(windowed[after + 1:], top, bottom)
    swept_top = boundary_swept(windowed[top_latest.index + 1:], top, above=True)
    swept_bottom = boundary_swept(
        windowed[bottom_latest.index + 1:], bottom, above=False
    )

    return Range(
        top=top,
        bottom=bottom,
        touches_top=len(top_cluster),
        touches_bottom=len(bottom_cluster),
        broken=broken,
        top_at=top_latest.timestamp,
        bottom_at=bottom_latest.timestamp,
        swept_top=swept_top,
        swept_bottom=swept_bottom,
        printed_at=windowed[after + 1].timestamp,
    )


def boundary_zone(rng: Range, direction: Direction, tolerance: float) -> Zone:
    """A range boundary as a `Zone` the existing pipeline can consume.

    A SHORT trades the top, a LONG the bottom. The band is one `tolerance`
    thick, measured INWARD — `[top - tolerance, top]` at the high,
    `[bottom, bottom + tolerance]` at the low — so it is exactly the spread
    the boundary was clustered with. Expressing it as a Zone is what lets
    `zone_touch_span`, `find_choch`, `select_valid_fvg`, `find_order_block`
    and `sweep_extreme` run on a range unchanged; only the stop and the
    target come from the box itself.

    `timestamp` is `printed_at`, NOT the boundary's own `top_at`/`bottom_at`.
    The timestamp is the anchor `zone_touch_span` and `first_zone_touch` use
    to decide which candles count as an excursion into the band, and for an
    ordinary zone that anchor is the OPEN of the formation candle — so the
    zone's own birth hour still reads as a touch of it (a known defect,
    tracked separately). A boundary would inherit it in its worst form: the
    cluster's latest pivot IS an extreme at the band by construction, so its
    own hour would always read as an excursion into the boundary, and a box
    price never returned to could anchor a stop in the hour that drew it.
    `printed_at` is the first candle after both boundaries were in place,
    which is strictly after that hour closed and is also the moment the box
    became a box.

    `pivot_index` is 0 for the same reason `structure.m5_marks` uses 0 on
    its synthetic band: a boundary is a cluster of pivots, not one candle,
    so no single index means anything here — and the index space of the H1
    array is meaningless against the M5 candles this zone is consumed with.
    Nothing reads it for a RANGE zone (`find_pivots` never returns index 0,
    so `zone_ladder`'s identity check cannot collide with it either).
    """
    if direction == Direction.SHORT:
        return Zone(
            bottom=rng.top - tolerance,
            top=rng.top,
            is_demand=False,
            pivot_index=0,
            timestamp=rng.printed_at,
            kind="RANGE",
        )
    return Zone(
        bottom=rng.bottom,
        top=rng.bottom + tolerance,
        is_demand=True,
        pivot_index=0,
        timestamp=rng.printed_at,
        kind="RANGE",
    )


def _largest_cluster(
    pivots: List[Pivot], tolerance: float
) -> Optional[List[Pivot]]:
    """Sort by price and slide a two-pointer window over it, returning the
    largest run whose prices span at most `tolerance` end to end — one
    level, give or take the tolerance.

    The right edge advances one pivot at a time; the left edge is pulled
    forward whenever the window's span exceeds `tolerance`. Both edges
    only ever move forward, so each pivot is visited a bounded number of
    times by the pointers themselves; scoring a window still rescans it
    for the recency tie-break, so the pass is O(n·w) in the widest window
    (O(n²) when every pivot sits within `tolerance` of every other) —
    fine at `window=120` H1 candles, and not worth a monotonic deque.
    What matters is not the constant but that
    every pivot gets tried as a potential window *start*, not just as a
    window member decided once by whatever came before it. A fixed anchor
    (grouping everything against the first pivot pulled into a cluster)
    can lock out a pivot that would have started a bigger, tighter pool
    one position later: a loose outlier followed by a tight pool of five
    would have the outlier drag the first pool member into a two-member
    cluster with it, burning that pivot as an anchor-mate instead of
    letting the five-pivot pool form on its own.

    Returns the largest window seen, ties broken by the most recent
    (highest index)."""
    if not pivots:
        return None
    ordered = sorted(pivots, key=lambda p: p.price)
    best: Optional[List[Pivot]] = None
    best_key = None
    left = 0
    for right in range(len(ordered)):
        while ordered[right].price - ordered[left].price > tolerance:
            left += 1
        window = ordered[left:right + 1]
        key = (len(window), max(p.index for p in window))
        if best_key is None or key > best_key:
            best_key, best = key, window
    return best


def _is_broken(candles: List[Candle], top: float, bottom: float) -> bool:
    """True if a BODY closed beyond either boundary and the break STILL
    HOLDS at the end of the window (D15, owner decision 2026-08-18).

    Exactly `structure._break_still_holds`, applied to both edges of the box
    at once: a close beyond an edge arms the break, a close back inside
    disarms it. A pierce that is reclaimed is liquidity taken, not a
    breakout (D9) — the box survives it and `swept_top`/`swept_bottom`
    record it instead.
    """
    broken = False
    for c in candles:
        if c.close > top and c.body_high > top:
            broken = True
        elif c.close < bottom and c.body_low < bottom:
            broken = True
        elif broken and bottom < c.close < top:
            broken = False  # the box was reclaimed
    return broken


def boundary_swept(candles: List[Candle], level: float, above: bool) -> bool:
    """D9: a wick pierced the level but the body closed back inside it —
    liquidity taken, not a breakout.

    Public because the same predicate is asked at two scopes, and one
    definition of "swept" is the point: `detect_range` asks it of every
    candle since the boundary's own latest pivot (that is what `Range.
    swept_top`/`swept_bottom` report), while the engine asks it of the
    touch→CHoCH excursion the setup actually formed in (D13, owner decision
    2026-08-18) before granting the ⭐.
    """
    for c in candles:
        if above:
            if c.high > level and c.body_high <= level:
                return True
        else:
            if c.low < level and c.body_low >= level:
                return True
    return False
