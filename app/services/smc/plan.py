"""Pre-Market Plan (strategy Шаблон B): conditional setups projected from
H4/H1 structure before the session, for the 07:45 morning briefing.

For a trending pair the plan is the with-trend scenario. For a flat pair both
directions are projected as speculative brackets ("if it breaks up → long, if
down → short"). No M5 trigger exists yet, so the stop is preliminary — beyond
the H1 zone extremum (per Шаблон B). The live alert re-anchors it to the
swept extreme of the zone excursion (Rule 6), which is not necessarily
tighter than this preliminary stop.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.services.smc.instruments import Instrument
from app.services.smc.liquidity import find_liquidity, nearest_liquidity
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
    h4: List[Candle],
    h1: List[Candle],
    direction: Direction,
    price: float,
    speculative: bool,
    min_rr: float,
    profile: StrategyProfile,
) -> Tuple[Optional[PlanScenario], Optional[str]]:
    """Project a conditional setup aimed at the nearest unswept liquidity.

    Returns `(scenario, reason)` — exactly one is non-None. `reason` explains
    a live zone whose nearest pool does not reach `min_rr`, so the plan can
    say so instead of pretending no structure exists.

    M5 is not scanned here: hours before the session its swings will have
    been swept long before price reaches the zone.
    """
    zone = find_h1_zone(h1, direction, max_touches=profile.max_zone_touches)
    if zone is None:
        return None, None

    if direction == Direction.LONG:
        # a pullback-to-demand plan: the zone must sit at/below current price
        if zone.top >= price:
            return None, None
        entry, stop = zone.top, zone.bottom - instrument.sl_buffer
    else:
        if zone.bottom <= price:
            return None, None
        entry, stop = zone.bottom, zone.top + instrument.sl_buffer

    risk = abs(entry - stop)
    if risk <= 0:
        return None, None

    tolerance = instrument.min_fvg
    levels = (
        find_liquidity(h1, "H1", tolerance) + find_liquidity(h4, "H4", tolerance)
    )
    target = nearest_liquidity(levels, direction, entry)
    if target is None:
        return None, None

    sign = -1 if direction == Direction.LONG else 1
    take_profit = target.price + sign * instrument.sl_buffer
    reward = (take_profit - entry) if direction == Direction.LONG else (entry - take_profit)
    d = instrument.price_decimals
    # See engine.py's Rule 7 for why this can't be inferred from RR alone:
    # a target inside one sl_buffer of the entry would put the TP on the
    # wrong side while abs(take_profit - entry) still reads positive.
    if reward <= 0:
        return None, (
            f"Zone {'Demand' if direction == Direction.LONG else 'Supply'} "
            f"{zone.bottom:.{d}f}-{zone.top:.{d}f} is live, but the nearest "
            "liquidity sits inside the SL buffer — no positive reward"
        )
    rr = reward / risk
    if rr < min_rr:
        return None, (
            f"Zone {'Demand' if direction == Direction.LONG else 'Supply'} "
            f"{zone.bottom:.{d}f}-{zone.top:.{d}f} is live, but the nearest "
            f"liquidity gives 1:{rr:.1f} — waiting for other structure"
        )

    return PlanScenario(
        direction=direction,
        entry=round(entry, d),
        stop_loss=round(stop, d),
        take_profit=round(take_profit, d),
        rr=round(rr, 2),
        zone_bottom=round(zone.bottom, d),
        zone_top=round(zone.top, d),
        speculative=speculative,
    ), None


def build_plan(
    instrument: Instrument,
    h4: List[Candle],
    h1: List[Candle],
    m5: List[Candle],
    min_rr: float = 1.0,
    profile: Optional[StrategyProfile] = None,
    market_closed: bool = False,
) -> PairPlan:
    """Build the pre-market plan; only scenarios reaching min_rr to the
    nearest unswept liquidity are shown."""
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

    reasons = []
    for direction, speculative in directions:
        scenario, reason = _scenario(
            instrument, h4, h1, direction, price, speculative, min_rr, profile,
        )
        if scenario:
            plan.scenarios.append(scenario)
        elif reason:
            reasons.append(reason)

    if not plan.scenarios:
        plan.note = reasons[0] if reasons else (
            "No clean H1 zone for a plan yet — wait for structure to form"
        )
    return plan
