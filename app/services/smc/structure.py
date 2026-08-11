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


def find_order_block(
    candles: List[Candle],
    direction: Direction,
    before_index: int,
    since_index: int,
    entry: float,
    stop_loss: float,
) -> Optional[Zone]:
    """The last candle opposing the trade before the impulse (owner
    definition, 2026-08-06). Price reacts to it on the retest, and it sits
    deeper than the FVG edge — a second limit option at a better price.

    Boundaries follow `build_zone`'s convention so the bot has one rule
    everywhere: demand = low -> body_high, supply = body_low -> high.
    `before_index` is exclusive — the walk starts at `before_index - 1`.

    `since_index` is the inclusive floor of the walk: the index where price
    entered the H1 zone (`zone_touch_span`'s start). The definition says
    "before the impulse that broke structure", not "anywhere in history" —
    an unbounded walk drifts back into the descending/ascending sweep leg
    (and further) and returns a candle *worse* than the FVG entry. The
    zone-touch index is used rather than the CHoCH index because the FVG
    window may end on the CHoCH candle itself (`engine` searches from
    `max(touch, choch - 2)`), so a CHoCH floor would make the window empty by
    construction on exactly the setups where the block is most typical — the
    opposing candle that starts the impulse sits at the sweep extreme, i.e.
    *before* the CHoCH. `[since_index, before_index)` is the same excursion
    the stop is anchored in (`sweep_extreme(m5, direction, touch, choch)`).

    A candidate is returned only when it is genuinely the deeper entry it is
    advertised as: strictly beyond the FVG entry and strictly inside the
    stop. Otherwise the alert would print "← deeper entry" against a worse
    price, and the ladder's "RR from OB" column would be computed on a larger
    risk (or, past the stop, on a collapsed one) while being read as the
    better option.

    The returned Zone's `pivot_index`/`timestamp` carry the order-block
    candle's own index and timestamp — it is the zone's origin candle, not a
    fractal pivot, even though it reuses the same field.
    """
    is_long = direction == Direction.LONG
    for i in range(min(before_index, len(candles)) - 1, max(since_index, 0) - 1, -1):
        c = candles[i]
        opposing = (c.close < c.open) if is_long else (c.close > c.open)
        if not opposing:
            continue
        if is_long:
            zone = Zone(bottom=c.low, top=c.body_high, is_demand=True,
                        pivot_index=i, timestamp=c.timestamp)
            ob_entry = zone.top
            deeper = stop_loss < ob_entry < entry
        else:
            zone = Zone(bottom=c.body_low, top=c.high, is_demand=False,
                        pivot_index=i, timestamp=c.timestamp)
            ob_entry = zone.bottom
            deeper = entry < ob_entry < stop_loss
        return zone if deeper else None
    return None


def zone_ladder(
    candles: List[Candle],
    direction: Direction,
    beyond: float,
    limit: int = 5,
    exclude: Optional[Zone] = None,
) -> List[Zone]:
    """Untested zones on the trade's own side, further out than `beyond`,
    nearest first — alternative deeper entries.

    Same side as the setup's own zone (supply above for a SHORT, demand
    below for a LONG), so every rung is a price the owner could still be
    filled at if the first limit does not fill. Opposite-side zones would be
    obstacles on the way to the target, which is not what he asked for
    (owner confirmation 2026-08-06; he wanted the untested H1 order block at
    USDCAD 1.40710, above a short's entry, that the bot was hiding).

    This walks every candidate pivot the way `find_h1_zone` does, but keeps
    all of them instead of stopping at the first match. `exclude` is the
    zone the setup itself formed in — it sits on this side by construction,
    so without it the live entry zone would always be the first rung.
    """
    want_high = direction == Direction.SHORT
    out: List[Zone] = []
    seen = set()
    if exclude is not None:
        seen.add((round(exclude.bottom, 8), round(exclude.top, 8)))
    for pivot in (p for p in find_pivots(candles) if p.is_high == want_high):
        zone = _mark_zone_state(candles, build_zone(candles, pivot))
        if zone.invalidated or zone.tested:
            continue
        if exclude is not None and zone.pivot_index == exclude.pivot_index:
            continue
        if (zone.bottom > beyond) if want_high else (zone.top < beyond):
            key = (round(zone.bottom, 8), round(zone.top, 8))
            if key in seen:
                continue
            seen.add(key)
            out.append(zone)
    out.sort(key=lambda z: z.bottom if want_high else -z.top)
    return out[:limit]


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
    window = candles[touch_index:choch_index + 1]
    if direction == Direction.LONG:
        return min(c.low for c in window)
    return max(c.high for c in window)


def first_zone_touch(candles: List[Candle], zone: Zone) -> Optional[int]:
    """Index of the first candle entering the zone at/after the zone's own
    pivot timestamp, or None if price never did.

    The anchor for the invalidation scan: a body close through the far edge
    kills the zone from the FIRST touch onward, not just within the latest
    excursion (`zone_touch_span` returns the latest — anchoring there forgot
    an invalidation once price exited and re-entered). The timestamp filter
    matters on the other side: candles that crossed the same price range on
    the way to forming the pivot are approach noise, and counting them would
    false-invalidate a fresh zone from its own birth decline.
    """
    for i, c in enumerate(candles):
        if c.timestamp < zone.timestamp:
            continue
        if c.low <= zone.top and c.high >= zone.bottom:
            return i
    return None


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
