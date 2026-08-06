"""Liquidity levels: unswept swing highs/lows and EQH/EQL pools (Rule 7).

A zone (structure.py) is an order block — an area price reacted from. A
liquidity level is the resting stop-loss pool behind a swing extreme. They
are different objects: the take-profit targets liquidity, the H1 entry
targets a zone.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from app.services.smc.models import Candle, Direction, Pivot
from app.services.smc.structure import find_pivots

# Higher timeframes break ties last; a pool on H4 outranks the same price on M5.
_TF_RANK = {"M5": 0, "H1": 1, "H4": 2}


@dataclass(frozen=True)
class LiquidityLevel:
    """An unswept swing extreme, or a pool of equal ones."""

    price: float
    is_high: bool
    timeframe: str  # "M5" | "H1" | "H4"
    equal_count: int  # 1 = single swing, 2+ = EQH/EQL pool
    timestamp: Optional[datetime] = None


def _is_swept(candles: List[Candle], pivot: Pivot, tolerance: float) -> bool:
    """True once a later candle traded beyond the level by more than
    `tolerance`.

    Wick-based on purpose: liquidity is taken by the wick that runs the stops,
    not by a body close (that is zone invalidation, a different rule). The
    tolerance is what makes equal highs possible — a poke smaller than the
    instrument's minimum FVG has not taken the pool.
    """
    later = candles[pivot.index + 1:]
    if pivot.is_high:
        return any(c.high > pivot.price + tolerance for c in later)
    return any(c.low < pivot.price - tolerance for c in later)


def _cluster(
    pivots: List[Pivot], is_high: bool, timeframe: str, tolerance: float
) -> List[LiquidityLevel]:
    """Group same-side pivots within `tolerance` into single pools.

    Tolerance is measured from the group's first (lowest-price) member, not
    pairwise between neighbors — a run of 3+ near-equal levels each within
    `tolerance` of its immediate neighbor but where the span from first to
    last exceeds `tolerance` splits into a pair plus a singleton, not one
    pool of 3.
    """
    group = sorted(
        (p for p in pivots if p.is_high == is_high), key=lambda p: p.price
    )
    out: List[LiquidityLevel] = []
    i = 0
    while i < len(group):
        j = i
        while j + 1 < len(group) and group[j + 1].price - group[i].price <= tolerance:
            j += 1
        members = group[i:j + 1]
        # Price meets the pool from one side: the lowest high, the highest low.
        anchor = members[0] if is_high else members[-1]
        newest = max(members, key=lambda p: p.index)
        out.append(
            LiquidityLevel(
                price=anchor.price,
                is_high=is_high,
                timeframe=timeframe,
                equal_count=len(members),
                timestamp=newest.timestamp,
            )
        )
        i = j + 1
    return out


def find_liquidity(
    candles: List[Candle], timeframe: str, tolerance: float
) -> List[LiquidityLevel]:
    """All unswept liquidity on this timeframe, singles and pools alike."""
    unswept = [p for p in find_pivots(candles) if not _is_swept(candles, p, tolerance)]
    return (
        _cluster(unswept, True, timeframe, tolerance)
        + _cluster(unswept, False, timeframe, tolerance)
    )


def nearest_liquidity(
    levels: List[LiquidityLevel], direction: Direction, entry: float
) -> Optional[LiquidityLevel]:
    """Closest unswept pool beyond `entry` in the trade direction.

    Equal distance is broken by pool size, then by timeframe — a pool of
    three equal highs is a better objective than one lone swing.
    """
    if direction == Direction.LONG:
        candidates = [lv for lv in levels if lv.is_high and lv.price > entry]
        distance = lambda lv: lv.price - entry  # noqa: E731
    else:
        candidates = [lv for lv in levels if not lv.is_high and lv.price < entry]
        distance = lambda lv: entry - lv.price  # noqa: E731
    if not candidates:
        return None
    candidates.sort(
        key=lambda lv: (
            distance(lv), -lv.equal_count, -_TF_RANK.get(lv.timeframe, 0)
        )
    )
    return candidates[0]


def liquidity_ladder(
    levels: List[LiquidityLevel], direction: Direction, entry: float,
    limit: int = 5,
) -> List[LiquidityLevel]:
    """The pools ahead, nearest first — not just the closest one.

    The nearest pool is often unusable: on a real USDCAD setup it sat 1.8
    pips from the entry against a 14-pip stop (RR 1:0.02) while the fourth
    pool out gave 1:1.0. The owner takes profit at liquidity and there is
    more than one pool; picking the closest for him was a decision made
    badly. Duplicate prices across timeframes collapse to the richer pool.
    """
    if direction == Direction.LONG:
        ahead = [lv for lv in levels if lv.is_high and lv.price > entry]
        ahead.sort(key=lambda lv: lv.price)
    else:
        ahead = [lv for lv in levels if not lv.is_high and lv.price < entry]
        ahead.sort(key=lambda lv: -lv.price)
    best: dict = {}
    order: List[float] = []
    for lv in ahead:
        key = round(lv.price, 8)
        if key not in best:
            best[key] = lv
            order.append(key)
        elif (lv.equal_count, _TF_RANK.get(lv.timeframe, 0)) > (
            best[key].equal_count, _TF_RANK.get(best[key].timeframe, 0)
        ):
            best[key] = lv
    return [best[k] for k in order[:limit]]
