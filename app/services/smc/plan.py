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
from app.services.smc.models import Candle, Direction, Trend, Zone
from app.services.smc.profiles import (
    CONSERVATIVE,
    StrategyProfile,
    effective_min_fvg,
)
from app.services.smc.range import Range, boundary_zone, detect_range
from app.services.smc.structure import (
    detect_trend,
    find_h1_fvg_zone,
    find_zone_of_interest,
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
    # What kind of zone this scenario is projected from: "OB" (order block)
    # or "FVG" (untouched H1 imbalance) — mirrors Zone.kind (Task 1). Charts
    # and messages name it; nothing branches on it.
    kind: str = "OB"
    # The runner-up zone the winner beat (spec 2026-08-16 §2.4): only set
    # when `kind` is "OB" and an untouched H1 imbalance also qualifies
    # positionally (see `_scenario`'s pullback-side test) — a deeper
    # alternative the plan chart draws alongside the winner, dimmer and
    # dashed. None whenever the winner was itself the imbalance (nothing
    # was beaten) or no positional candidate exists. Deliberately excluded
    # from `planbook.plan_fingerprint`: its appearance/disappearance is not
    # a change of trading idea.
    runner_up: Optional[Zone] = None
    # True when `kind == "RANGE"` and this boundary was pierced by a wick
    # and reclaimed at some point since its own latest confirmed pivot
    # (Range.swept_top/swept_bottom, D9) -- worth a note on the plan message
    # because a pool already raided once may have less liquidity behind it
    # than a boundary that has never been tested. Always False for OB/FVG
    # scenarios, which have no such flag to report.
    swept: bool = False


@dataclass
class PairPlan:
    pair: str
    price: float
    price_decimals: int
    h4_trend: Trend
    scenarios: List[PlanScenario] = field(default_factory=list)
    note: Optional[str] = None
    market_closed: bool = False
    # The stage the plan stopped at, in the live checklist's own words (spec
    # 2026-08-06 §6). None when there is a plan, or when the market is closed
    # — a weekend is not a missing stage.
    blocker: Optional[str] = None
    # (bottom, top, direction) of the zone the blocker sentence names by its
    # bounds, when it names one. `/plan` keeps its min_rr filter while the
    # alert dropped it, so "zone is live but liquidity gives 1:0.6" is both a
    # routine plan blocker and a routine announcement — the owner read those
    # bounds this morning, so the zone counts as planned (owner decision
    # 2026-08-06, spec §6).
    blocker_zone: Optional[Tuple[float, float, str]] = None
    # Rule 1 parity with the engine: when H4 is FLAT the direction can come
    # from the H1 trend (owner decision 2026-08-06) or — aggressive only —
    # from an unreclaimed H4 CHoCH. The note labels that source the same way
    # the live alert does; None when the direction is plain H4 (or absent).
    direction_note: Optional[str] = None

    def zones_shown(self) -> List[Tuple[float, float, str]]:
        """Every zone this plan message named, scenario or blocker — the set
        an alert later checks itself against."""
        zones = [
            (s.zone_bottom, s.zone_top, s.direction.value) for s in self.scenarios
        ]
        if self.blocker_zone:
            zones.append(self.blocker_zone)
        return zones


# The live checklist's own sentences (engine.py Rules 1 and 2). The plan
# reports the stage it stopped at in these words rather than inventing a
# second vocabulary. Stages the plan cannot evaluate — M5 CHoCH and the
# imbalance — are never reported: price has not reached the zone yet, so they
# are not yet due.
H4_STRUCTURE_NOTE = (
    "Wait for a clear HH+HL or LH+LL structure on H4 "
    "(2 closed bodies beyond the extreme)"
)


def _zone_note(direction: Direction) -> str:
    """Word-for-word the engine's own Rule 2 watch note (engine.py) — the
    plan reports the stage it stopped at in the live checklist's words, and
    a test pins the two sentences equal."""
    return (
        "Wait for a fresh H1 zone to form — an untested "
        f"{'HL' if direction == Direction.LONG else 'LH'}"
        " order block or an untouched H1 imbalance"
    )


# Reason ranks: a reason about a zone that already exists is more specific
# than one about a zone that does not, and beats the H4 fallback.
_LIVE_ZONE = 0
_NO_ZONE = 1

# (rank, sentence, zone the sentence names by its bounds or None)
Reason = Tuple[int, str, Optional[Tuple[float, float, str]]]


def _is_pullback_side(zone: Zone, direction: Direction, price: float) -> bool:
    """True when `zone` sits on the correct side of price for a pullback —
    below price for a LONG, above for a SHORT. The exact test `_scenario`
    already applies to the winning zone (below, as `zone.top >= price` /
    `zone.bottom <= price` rejections); factored out so the OB/FVG
    runner-up is judged by the same rule rather than a second one. A zone
    price has already traded through is not one the owner is still
    waiting at."""
    if direction == Direction.LONG:
        return zone.top < price
    return zone.bottom > price


def _scenario(
    instrument: Instrument,
    h4: List[Candle],
    h1: List[Candle],
    direction: Direction,
    price: float,
    speculative: bool,
    min_rr: float,
    profile: StrategyProfile,
) -> Tuple[Optional[PlanScenario], Optional[Reason]]:
    """Project a conditional setup aimed at the nearest unswept liquidity.

    Returns `(scenario, reason)` — exactly one is non-None. `reason` is
    `(rank, sentence, zone)`: the stage this direction stopped at, so the plan
    can name it instead of falling back to a generic "no structure" line, plus
    the zone that sentence names by its bounds (None when it names none).

    M5 is not scanned here: hours before the session its swings will have
    been swept long before price reaches the zone.
    """
    d = instrument.price_decimals
    kind = "Demand" if direction == Direction.LONG else "Supply"
    # The engine computes this with the same helper, so the plan and the
    # live checklist cannot name different H1 zones — also reused below for
    # the runner-up lookup, so both candidates share one min-size floor.
    min_size = effective_min_fvg(instrument.min_fvg, profile)
    zone = find_zone_of_interest(
        h1, direction, min_size=min_size, max_touches=profile.max_zone_touches,
    )
    if zone is None:
        return None, (_NO_ZONE, _zone_note(direction), None)

    def named(rank: int, sentence: str) -> Reason:
        """Every sentence below prints the zone's bounds, so the owner read
        them — the zone counts as one the plan showed him."""
        return rank, sentence, (
            round(zone.bottom, d), round(zone.top, d), direction.value,
        )

    zone_label = (
        f"H1 {kind} zone ({zone.kind}) {zone.bottom:.{d}f}-{zone.top:.{d}f}"
    )
    inside = (
        f"Price is already inside the {zone_label} — no pullback left to "
        "project, the live checklist takes over"
    )
    if direction == Direction.LONG:
        # a pullback-to-demand plan: the zone must sit at/below current price
        if zone.contains(price):
            return None, named(_LIVE_ZONE, inside)
        if not _is_pullback_side(zone, direction, price):
            return None, named(
                _NO_ZONE,
                f"Price is below the {zone_label} — wait for a fresh untested "
                "HL to form under price",
            )
        entry, stop = zone.top, zone.bottom - instrument.sl_buffer
    else:
        if zone.contains(price):
            return None, named(_LIVE_ZONE, inside)
        if not _is_pullback_side(zone, direction, price):
            return None, named(
                _NO_ZONE,
                f"Price is above the {zone_label} — wait for a fresh untested "
                "LH to form above price",
            )
        entry, stop = zone.bottom, zone.top + instrument.sl_buffer

    risk = abs(entry - stop)
    if risk <= 0:
        return None, named(
            _LIVE_ZONE,
            f"{zone_label} is live, but entry and stop coincide — no risk to "
            "measure",
        )

    tolerance = instrument.min_fvg
    levels = (
        find_liquidity(h1, "H1", tolerance) + find_liquidity(h4, "H4", tolerance)
    )
    target = nearest_liquidity(levels, direction, entry)
    if target is None:
        return None, named(
            _LIVE_ZONE,
            f"{zone_label} is live, but there is no unswept liquidity ahead "
            "of it to aim at",
        )

    sign = -1 if direction == Direction.LONG else 1
    take_profit = target.price + sign * instrument.sl_buffer
    reward = (take_profit - entry) if direction == Direction.LONG else (entry - take_profit)
    # See engine.py's Rule 7 for why this can't be inferred from RR alone:
    # a target inside one sl_buffer of the entry would put the TP on the
    # wrong side while abs(take_profit - entry) still reads positive.
    if reward <= 0:
        return None, named(
            _LIVE_ZONE,
            f"Zone {kind} {zone.bottom:.{d}f}-{zone.top:.{d}f} is live, but "
            "the nearest liquidity sits inside the SL buffer — no positive "
            "reward",
        )
    rr = reward / risk
    if rr < min_rr:
        return None, named(
            _LIVE_ZONE,
            f"Zone {kind} {zone.bottom:.{d}f}-{zone.top:.{d}f} is live, but "
            f"the nearest liquidity gives 1:{rr:.1f} — waiting for other "
            "structure",
        )

    # D4's runner-up (spec 2026-08-16 §2.4): when the order block won zone
    # selection, the untouched imbalance it beat is still worth drawing —
    # but only when it is a genuine pullback candidate in its own right, not
    # a gap price has already run past.
    runner_up = None
    if zone.kind == "OB":
        candidate = find_h1_fvg_zone(h1, direction, min_size)
        if candidate is not None and _is_pullback_side(candidate, direction, price):
            runner_up = candidate

    return PlanScenario(
        direction=direction,
        entry=round(entry, d),
        stop_loss=round(stop, d),
        take_profit=round(take_profit, d),
        rr=round(rr, 2),
        zone_bottom=round(zone.bottom, d),
        zone_top=round(zone.top, d),
        speculative=speculative,
        kind=zone.kind,
        runner_up=runner_up,
    ), None


def _range_scenario(
    instrument: Instrument, rng: Range, direction: Direction, min_rr: float,
) -> Tuple[Optional[PlanScenario], Optional[Reason]]:
    """Project one boundary of an in-play range as a scenario (D11/D12).

    Entry is the boundary itself, SL sits one `sl_buffer` beyond it, and TP
    is the OPPOSITE boundary pulled in by one `sl_buffer` — the same
    geometry `engine.py`'s range branch builds for the live setup (Rule 6/7
    there), so the plan can never show a different bracket than the live
    checklist would once price arrives. No M5 data exists yet at plan time,
    so — like every other scenario here — the stop is preliminary and never
    the live alert's swept-extreme re-anchor.
    """
    d = instrument.price_decimals
    tolerance = instrument.min_fvg
    zone = boundary_zone(rng, direction, tolerance)
    if direction == Direction.SHORT:
        entry, stop_loss = rng.top, rng.top + instrument.sl_buffer
        take_profit = rng.bottom + instrument.sl_buffer
        swept = rng.swept_top
    else:
        entry, stop_loss = rng.bottom, rng.bottom - instrument.sl_buffer
        take_profit = rng.top - instrument.sl_buffer
        swept = rng.swept_bottom

    def named(sentence: str) -> Reason:
        return _LIVE_ZONE, sentence, (
            round(zone.bottom, d), round(zone.top, d), direction.value,
        )

    risk = abs(entry - stop_loss)
    if risk <= 0:
        return None, named(
            "Range boundary is live, but entry and stop coincide — no risk "
            "to measure"
        )
    reward = (
        (take_profit - entry) if direction == Direction.LONG
        else (entry - take_profit)
    )
    if reward <= 0:
        return None, named(
            "Range boundary is live, but the opposite boundary sits inside "
            "the SL buffer — no positive reward"
        )
    rr = reward / risk
    if rr < min_rr:
        return None, named(
            f"Range boundary is live, but the target gives 1:{rr:.1f} — "
            "waiting for other structure"
        )
    return PlanScenario(
        direction=direction,
        entry=round(entry, d),
        stop_loss=round(stop_loss, d),
        take_profit=round(take_profit, d),
        rr=round(rr, 2),
        zone_bottom=round(zone.bottom, d),
        zone_top=round(zone.top, d),
        speculative=False,
        kind="RANGE",
        swept=swept,
    ), None


def _range_scenarios(
    instrument: Instrument, rng: Range, min_rr: float,
) -> Tuple[List[PlanScenario], List[Reason]]:
    """Both boundaries of an in-play range, one scenario each (D12: these
    REPLACE the speculative both-way brackets, they never join them)."""
    scenarios: List[PlanScenario] = []
    reasons: List[Reason] = []
    for direction in (Direction.SHORT, Direction.LONG):
        scenario, reason = _range_scenario(instrument, rng, direction, min_rr)
        if scenario:
            scenarios.append(scenario)
        elif reason:
            reasons.append(reason)
    return scenarios, reasons


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
    range_in_play = False
    range_scenarios: List[PlanScenario] = []
    range_reasons: List[Reason] = []
    if trend == Trend.UP:
        directions = [(Direction.LONG, False)]
    elif trend == Trend.DOWN:
        directions = [(Direction.SHORT, False)]
    else:
        # Engine parity (engine.evaluate Rule 1): H1 trend first, then —
        # aggressive only — the H4 CHoCH first leg. The zone alert quotes
        # this plan's numbers, so the plan may not disagree with the engine
        # about which side of the market is in play.
        h1_trend = detect_trend(h1)
        if h1_trend == Trend.UP:
            directions = [(Direction.LONG, False)]
            plan.direction_note = "H4 flat — direction from H1 uptrend"
        elif h1_trend == Trend.DOWN:
            directions = [(Direction.SHORT, False)]
            plan.direction_note = "H4 flat — direction from H1 downtrend"
        else:
            # D11 (owner decision 2026-08-18): both H4 and H1 read FLAT —
            # exactly the engine's Rule 1 flat branch (engine.py, the `else`
            # reached only once H1 UP/DOWN are both ruled out). A range in
            # play REPLACES the speculative both-way brackets entirely
            # (D12) rather than joining them.
            #
            # DELIBERATE DIVERGENCE from the engine, and the one place this
            # module does not mirror it (review 2026-08-18). The engine
            # falls through to the aggressive H4-CHoCH branch when a box is
            # live but price sits MID-range, because it can only trade the
            # boundary price is actually at. The plan has no such state: it
            # projects BOTH boundaries as scenarios precisely because it
            # cannot know which edge price will visit, so "mid-range" is
            # the normal case a range plan is written for, not a gap in it.
            # Adding a CHoCH bracket beside the two boundary scenarios
            # would put two different plans for one pair in one message —
            # the exact outcome D12 rejects. Unreachable in production
            # either way: only the conservative profile ships, and it has
            # `allow_h4_choch_entry` off.
            rng = detect_range(h1, instrument.min_fvg)
            if rng is not None and not rng.broken:
                range_in_play = True
                range_scenarios, range_reasons = _range_scenarios(
                    instrument, rng, min_rr,
                )
                plan.direction_note = (
                    "H4 and H1 are both flat — trading the range boundaries"
                )
            elif profile.allow_h4_choch_entry:
                choch = h4_choch_direction(h4)
                if choch is not None:
                    directions = [(choch, False)]  # aggressive: first-leg direction
                    plan.direction_note = (
                        "H4 flat — direction from CHoCH (first leg, not "
                        "with-trend)"
                    )
    if range_in_play:
        plan.scenarios = range_scenarios
    elif not directions and trend == Trend.FLAT:
        directions = [(Direction.LONG, True), (Direction.SHORT, True)]

    reasons: List[Reason] = list(range_reasons)
    for direction, speculative in directions:
        scenario, reason = _scenario(
            instrument, h4, h1, direction, price, speculative, min_rr, profile,
        )
        if scenario:
            plan.scenarios.append(scenario)
        elif reason:
            reasons.append(reason)

    if not plan.scenarios:
        reasons.sort(key=lambda r: r[0])
        best = reasons[0] if reasons else None
        # A direction the plan only guessed at (both-way flat brackets) is not
        # a missing zone — the missing thing is the H4 structure. A reason
        # about a zone that is actually live still beats it: that is the more
        # specific stage. A range in play is never speculative-only: its
        # reasons are rank _LIVE_ZONE by construction (the boundary IS the
        # zone), so they always qualify.
        speculative_only = not range_in_play and all(spec for _, spec in directions)
        if best and (best[0] == _LIVE_ZONE or not speculative_only):
            plan.blocker, plan.blocker_zone = best[1], best[2]
        else:
            plan.blocker = H4_STRUCTURE_NOTE
        plan.note = plan.blocker
    return plan
