"""Market structure analysis: pivots, trend, zones, BOS/CHoCH (Rules 1-3)."""

from typing import List, Optional, Tuple

from app.services.smc.models import Candle, Direction, Pivot, Trend, Zone

# A pivot needs `PIVOT_WING` candles on each side and at least
# `CONFIRMATION_CANDLES` closed candles after it to be considered confirmed
# (an extreme is confirmed by 2 closed candle bodies).
PIVOT_WING = 2
CONFIRMATION_CANDLES = 2


def find_pivots(candles: List[Candle]) -> List[Pivot]:
    """Find confirmed swing highs/lows using a fractal window."""
    pivots: List[Pivot] = []
    n = len(candles)
    last_confirmed = n - CONFIRMATION_CANDLES
    for i in range(PIVOT_WING, min(n - PIVOT_WING, last_confirmed)):
        window = candles[i - PIVOT_WING : i + PIVOT_WING + 1]
        c = candles[i]
        if c.high == max(w.high for w in window) and all(
            c.high > w.high for j, w in enumerate(window) if j != PIVOT_WING
        ):
            pivots.append(Pivot(i, c.high, c.timestamp, is_high=True))
        if c.low == min(w.low for w in window) and all(
            c.low < w.low for j, w in enumerate(window) if j != PIVOT_WING
        ):
            pivots.append(Pivot(i, c.low, c.timestamp, is_high=False))
    return pivots


def detect_trend(candles: List[Candle]) -> Trend:
    """Rule 1: H4 trend from the last two confirmed highs and lows.

    Uptrend = HH + HL, downtrend = LH + LL, anything else = flat.
    A body close beyond the last HL/LH (CHoCH) downgrades the trend to flat.
    """
    pivots = find_pivots(candles)
    highs = [p for p in pivots if p.is_high]
    lows = [p for p in pivots if not p.is_high]
    if len(highs) < 2 or len(lows) < 2:
        return Trend.FLAT

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price

    if hh and hl:
        # CHoCH check: a body close below the last HL breaks the uptrend —
        # but only while price still holds beyond the level. A fakeout that
        # was reclaimed does not kill the trend forever.
        if _break_still_holds(candles, lows[-1], below=True):
            return Trend.FLAT
        return Trend.UP
    if lh and ll:
        if _break_still_holds(candles, highs[-1], below=False):
            return Trend.FLAT
        return Trend.DOWN
    return Trend.FLAT


def h4_choch_direction(candles: List[Candle]) -> Optional[Direction]:
    """Aggressive entry: direction implied by an unreclaimed H4 CHoCH.

    Consulted only when detect_trend() is FLAT. Returns SHORT if the last
    confirmed higher-low was broken down and not reclaimed, LONG if the last
    confirmed lower-high was broken up and not reclaimed, else None.
    """
    pivots = find_pivots(candles)
    highs = [p for p in pivots if p.is_high]
    lows = [p for p in pivots if not p.is_high]
    if lows and _break_still_holds(candles, lows[-1], below=True):
        return Direction.SHORT
    if highs and _break_still_holds(candles, highs[-1], below=False):
        return Direction.LONG
    return None


def _break_still_holds(candles: List[Candle], pivot: Pivot, below: bool) -> bool:
    """True if a body closed beyond the pivot level and price has not
    reclaimed it since (the structural break is still in force)."""
    broken = False
    for c in candles[pivot.index + 1 :]:
        if below:
            if c.close < pivot.price and c.body_low < pivot.price:
                broken = True
            elif broken and c.close > pivot.price:
                broken = False  # level reclaimed
        else:
            if c.close > pivot.price and c.body_high > pivot.price:
                broken = True
            elif broken and c.close < pivot.price:
                broken = False
    return broken


def build_zone(candles: List[Candle], pivot: Pivot) -> Zone:
    """Rule 2: zone from the pivot candle — wick to body edge."""
    c = candles[pivot.index]
    if pivot.is_high:
        return Zone(
            bottom=c.body_low,
            top=c.high,
            is_demand=False,
            pivot_index=pivot.index,
            timestamp=pivot.timestamp,
        )
    return Zone(
        bottom=c.low,
        top=c.body_high,
        is_demand=True,
        pivot_index=pivot.index,
        timestamp=pivot.timestamp,
    )


def _mark_zone_state(candles: List[Candle], zone: Zone) -> Zone:
    """Mark whether the zone was tested/invalidated after forming, and count
    completed touches (entered-and-left excursions)."""
    start = zone.pivot_index + PIVOT_WING + 1
    touches = 0
    in_zone_prev = False
    for c in candles[start:]:
        if zone.is_demand:
            if c.close < zone.bottom and c.body_low < zone.bottom:
                zone.invalidated = True
                return zone
        else:
            if c.close > zone.top and c.body_high > zone.top:
                zone.invalidated = True
                return zone
        in_zone = c.low <= zone.top and c.high >= zone.bottom
        if in_zone_prev and not in_zone:
            touches += 1
        in_zone_prev = in_zone
    zone.touches = touches
    zone.tested = touches > 0
    return zone


def find_h1_zone(
    candles: List[Candle], direction: Direction, max_touches: int = 0
) -> Optional[Zone]:
    """Rule 2: latest valid H1 Demand (long)/Supply (short) zone with at most
    `max_touches` completed retests (0 = untested only, conservative)."""
    pivots = find_pivots(candles)
    want_high = direction == Direction.SHORT
    candidates = [p for p in pivots if p.is_high == want_high]
    for pivot in reversed(candidates):
        zone = _mark_zone_state(candles, build_zone(candles, pivot))
        if not zone.invalidated and zone.touches <= max_touches:
            return zone
    return None


def find_target_zones(
    candles: List[Candle], direction: Direction, entry: float
) -> List[Zone]:
    """Rule 7: all untested opposite zones beyond entry, nearest first."""
    pivots = find_pivots(candles)
    want_high = direction == Direction.LONG
    out: List[Zone] = []
    for pivot in (p for p in pivots if p.is_high == want_high):
        zone = _mark_zone_state(candles, build_zone(candles, pivot))
        if zone.invalidated or zone.tested:
            continue
        if direction == Direction.LONG and zone.bottom > entry:
            out.append(zone)
        elif direction == Direction.SHORT and zone.top < entry:
            out.append(zone)
    if direction == Direction.LONG:
        out.sort(key=lambda z: z.bottom)          # nearest above entry first
    else:
        out.sort(key=lambda z: z.top, reverse=True)  # nearest below entry first
    return out


def find_target_zone(
    candles: List[Candle], direction: Direction, entry: float
) -> Optional[Zone]:
    """Nearest untested opposite zone beyond entry (back-compat wrapper)."""
    return next(iter(find_target_zones(candles, direction, entry)), None)


def find_choch(
    candles: List[Candle], direction: Direction, from_index: int
) -> Optional[int]:
    """Rule 3 phase 2: M5 CHoCH in trade direction at/after `from_index`.

    For a long: find the last confirmed lower-high formed before/at the zone
    touch, then return the index of the first candle whose body closes above it.
    """
    pivots = find_pivots(candles)
    if direction == Direction.LONG:
        levels = [p for p in pivots if p.is_high and p.index <= from_index]
        if not levels:
            return None
        level = levels[-1].price
        for i in range(max(from_index, levels[-1].index + 1), len(candles)):
            c = candles[i]
            if c.close > level and c.body_high > level:
                return i
    else:
        levels = [p for p in pivots if not p.is_high and p.index <= from_index]
        if not levels:
            return None
        level = levels[-1].price
        for i in range(max(from_index, levels[-1].index + 1), len(candles)):
            c = candles[i]
            if c.close < level and c.body_low < level:
                return i
    return None


def sweep_extreme(
    candles: List[Candle], direction: Direction, touch_index: int, choch_index: int
) -> float:
    """Rule 6: the extreme of the excursion that swept liquidity before the
    CHoCH — the low of a long's pullback, the high of a short's.

    Anchoring the stop here instead of at the last fractal pivot matters when
    a shallower pivot forms after the sweep: the pivot stop would sit inside
    the wick that took the stops, and get taken with them.
    """
    window = candles[touch_index:choch_index + 1] or [candles[choch_index]]
    if direction == Direction.LONG:
        return min(c.low for c in window)
    return max(c.high for c in window)


def zone_touch_span(
    candles: List[Candle], zone: Zone
) -> Optional[Tuple[int, int]]:
    """Inclusive (start, end) indices of the LAST contiguous excursion into
    the zone, or None if price never entered.

    Intended for M5 candles against an H1 zone. `start` is the origin for the
    CHoCH/FVG search (the bug this replaces returned only the last candle, so
    the M5 CHoCH inside the zone was invisible while price sat in the zone).
    """
    start = end = None
    for i, c in enumerate(candles):
        in_zone = c.low <= zone.top and c.high >= zone.bottom
        if in_zone:
            if start is None or (end is not None and i > end + 1):
                start = i  # begin a new run
            end = i
    if start is None:
        return None
    return start, end
