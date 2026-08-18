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

from app.services.smc.models import Candle, Pivot
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
    swept_top: bool       # a wick closed back inside after piercing the top (D9)
    swept_bottom: bool


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

    after = max(top_latest.index, bottom_latest.index)
    broken = _is_broken(windowed[after + 1:], top, bottom)
    swept_top = _swept(windowed[top_latest.index + 1:], top, above=True)
    swept_bottom = _swept(windowed[bottom_latest.index + 1:], bottom, above=False)

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
    )


def _largest_cluster(
    pivots: List[Pivot], tolerance: float
) -> Optional[List[Pivot]]:
    """Sort by price and walk, grouping consecutive pivots within
    `tolerance` of the previous one into the same cluster. Returns the
    largest cluster, ties broken by the most recent (highest index)."""
    if not pivots:
        return None
    ordered = sorted(pivots, key=lambda p: p.price)
    clusters: List[List[Pivot]] = [[ordered[0]]]
    for p in ordered[1:]:
        if p.price - clusters[-1][-1].price <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return max(clusters, key=lambda c: (len(c), max(p.index for p in c)))


def _is_broken(candles: List[Candle], top: float, bottom: float) -> bool:
    """True if any candle's BODY closed beyond either boundary — the same
    idiom `structure._break_still_holds` and `_mark_zone_state` use for a
    genuine structural break, applied to both edges of the box."""
    for c in candles:
        if c.close > top and c.body_high > top:
            return True
        if c.close < bottom and c.body_low < bottom:
            return True
    return False


def _swept(candles: List[Candle], level: float, above: bool) -> bool:
    """D9: a wick pierced the level but the body closed back inside it —
    liquidity taken, not a breakout."""
    for c in candles:
        if above:
            if c.high > level and c.body_high <= level:
                return True
        else:
            if c.low < level and c.body_low >= level:
                return True
    return False
