"""Pre-Market Plan (strategy Шаблон B): conditional setups projected from
H4/H1 structure before the session, for the 07:45 morning briefing.

For a trending pair the plan is the with-trend scenario. For a flat pair both
directions are projected as speculative brackets ("if it breaks up → long, if
down → short"). No M5 trigger exists yet, so the stop is preliminary — beyond
the H1 zone extremum (per Шаблон B), not the tighter live M5-pivot stop.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.services.smc.instruments import Instrument
from app.services.smc.models import Candle, Direction, Trend
from app.services.smc.profiles import CONSERVATIVE, StrategyProfile
from app.services.smc.structure import (
    detect_trend,
    find_h1_zone,
    h4_choch_direction,
)


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
    direction: Direction,
    price: float,
    speculative: bool,
    tp_rr: float,
    profile: StrategyProfile,
) -> Optional[PlanScenario]:
    """Project a conditional setup with the fixed take-profit (tp_rr × risk)."""
    zone = find_h1_zone(h1, direction, max_touches=profile.max_zone_touches)
    if zone is None:
        return None

    if direction == Direction.LONG:
        # a pullback-to-demand plan: the zone must sit at/below current price
        if zone.top >= price:
            return None
        entry, stop = zone.top, zone.bottom - instrument.sl_buffer
    else:
        if zone.bottom <= price:
            return None
        entry, stop = zone.bottom, zone.top + instrument.sl_buffer

    risk = abs(entry - stop)
    if risk <= 0:
        return None

    sign = 1 if direction == Direction.LONG else -1
    d = instrument.price_decimals
    return PlanScenario(
        direction=direction,
        entry=round(entry, d),
        stop_loss=round(stop, d),
        take_profit=round(entry + sign * tp_rr * risk, d),
        rr=round(tp_rr, 2),
        zone_bottom=round(zone.bottom, d),
        zone_top=round(zone.top, d),
        speculative=speculative,
    )


def build_plan(
    instrument: Instrument,
    h4: List[Candle],
    h1: List[Candle],
    m5: List[Candle],
    tp_rr: float = 2.5,
    profile: Optional[StrategyProfile] = None,
    market_closed: bool = False,
) -> PairPlan:
    """Build the pre-market plan; TP is projected at the fixed tp_rr × risk."""
    profile = profile or CONSERVATIVE
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

    directions: List[Tuple[Direction, bool]] = []
    if trend == Trend.UP:
        directions = [(Direction.LONG, False)]
    elif trend == Trend.DOWN:
        directions = [(Direction.SHORT, False)]
    elif profile.allow_h4_choch_entry:
        choch = h4_choch_direction(h4)
        if choch is not None:
            directions = [(choch, False)]  # aggressive: first-leg direction
    if not directions and trend == Trend.FLAT:
        directions = [(Direction.LONG, True), (Direction.SHORT, True)]

    for direction, speculative in directions:
        scenario = _scenario(
            instrument, h1, direction, price, speculative, tp_rr, profile,
        )
        if scenario:
            plan.scenarios.append(scenario)

    if not plan.scenarios:
        plan.note = "No clean H1 zone for a plan yet — wait for structure to form"
    return plan
