"""Pre-Market Plan (strategy Шаблон B): conditional setups projected from
H4/H1 structure before the session, for the 07:45 morning briefing.

For a trending pair the plan is the with-trend scenario. For a flat pair both
directions are projected as speculative brackets ("if it breaks up → long, if
down → short"). No M5 trigger exists yet, so the stop is preliminary — beyond
the H1 zone extremum (per Шаблон B), not the tighter live M5-pivot stop.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from app.services.smc.instruments import Instrument
from app.services.smc.models import Candle, Direction, Trend
from app.services.smc.structure import detect_trend, find_h1_zone, find_target_zone


@dataclass
class PlanScenario:
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    rr: float
    zone_bottom: float
    zone_top: float
    speculative: bool  # True for the flat-pair both-direction brackets


@dataclass
class PairPlan:
    pair: str
    price: float
    price_decimals: int
    h4_trend: Trend
    scenarios: List[PlanScenario] = field(default_factory=list)
    note: Optional[str] = None
    market_closed: bool = False


def _scenario(
    instrument: Instrument,
    h1: List[Candle],
    h4: List[Candle],
    direction: Direction,
    price: float,
    speculative: bool,
) -> Optional[PlanScenario]:
    """Project one conditional setup from the nearest untested H1 zone."""
    zone = find_h1_zone(h1, direction)
    if zone is None:
        return None

    if direction == Direction.LONG:
        # a pullback-to-demand plan: the zone must sit at/below current price
        if zone.top >= price:
            return None
        entry = zone.top
        stop = zone.bottom - instrument.sl_buffer
    else:
        if zone.bottom <= price:
            return None
        entry = zone.bottom
        stop = zone.top + instrument.sl_buffer

    risk = abs(entry - stop)
    if risk <= 0:
        return None
    target = find_target_zone(h1, direction, entry) or find_target_zone(
        h4, direction, entry
    )
    if target is None:
        return None
    tp = target.bottom if direction == Direction.LONG else target.top

    d = instrument.price_decimals
    return PlanScenario(
        direction=direction,
        entry=round(entry, d),
        stop_loss=round(stop, d),
        take_profit=round(tp, d),
        rr=round(abs(tp - entry) / risk, 2),
        zone_bottom=round(zone.bottom, d),
        zone_top=round(zone.top, d),
        speculative=speculative,
    )


def build_plan(
    instrument: Instrument,
    h4: List[Candle],
    h1: List[Candle],
    m5: List[Candle],
    market_closed: bool = False,
) -> PairPlan:
    """Build the pre-market plan for one instrument from fresh candles."""
    price = round(m5[-1].close, instrument.price_decimals) if m5 else 0.0
    trend = detect_trend(h4)
    plan = PairPlan(
        pair=instrument.key,
        price=price,
        price_decimals=instrument.price_decimals,
        h4_trend=trend,
        market_closed=market_closed,
    )
    if market_closed:
        plan.note = "Market closed (weekend) — no plan"
        return plan

    if trend == Trend.UP:
        s = _scenario(instrument, h1, h4, Direction.LONG, price, speculative=False)
        if s:
            plan.scenarios.append(s)
        else:
            plan.note = "H4 uptrend, but no clean untested H1 demand + target yet"
    elif trend == Trend.DOWN:
        s = _scenario(instrument, h1, h4, Direction.SHORT, price, speculative=False)
        if s:
            plan.scenarios.append(s)
        else:
            plan.note = "H4 downtrend, but no clean untested H1 supply + target yet"
    else:
        for direction in (Direction.LONG, Direction.SHORT):
            s = _scenario(instrument, h1, h4, direction, price, speculative=True)
            if s:
                plan.scenarios.append(s)
        if not plan.scenarios:
            plan.note = "H4 flat and no clean zones yet — wait for structure to form"
    return plan
