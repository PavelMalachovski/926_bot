"""Pending (limit) entries and the approach read — owner decision D25
(2026-09-05): the bot works in two modes.

* **By notification** — the 🚨 alert says a setup has fully formed and the
  owner enters AT MARKET: the message carries the market price, the Rule 6
  stop and TP1/TP2/TP3 (the three nearest unswept pools, `take_profits`).
  Before that, one get-ready message per zone per day says price is
  *almost there* (`approach_read`) with the projected limit bracket.
* **By button** — the pair buttons under the 08:05/14:05 summary answer
  with a *Setup analysis*: two pending entries, MAIN and DEEP, each with its
  own entry, stop, TP1-3 and RR (`build_pending`), computed on a fresh fetch.

Everything here is pure geometry over an `AnalysisResult` the engine already
produced plus the candles it was produced from: no network, no DB, no
messages. It never decides whether a setup exists — that stays Rule 1-6's
job in `engine.py` — it only prices the places the owner could rest an
order, in the strategy's own vocabulary: the M5 imbalance (Rule 5), the M5
order block, the H1 zone of interest (Rule 2), the untested zones further
out (the zone ladder), or a range boundary (D11).
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from app.services.smc.instruments import Instrument
from app.services.smc.liquidity import (
    LiquidityLevel,
    TakeProfit,
    find_liquidity,
    take_profits,
)
from app.services.smc.models import (
    AnalysisResult,
    Candle,
    Direction,
    Verdict,
    Zone,
)
from app.services.smc.range import Range, boundary_zone
from app.services.smc.structure import zone_ladder

APPROVED = (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET)

ROLE_MAIN = "main"
ROLE_DEEP = "deep"
ROLE_MARKET = "market"
# A range read mid-box: one bracket per boundary, neither is "deeper" than
# the other — they trade opposite ways (D11/D12).
ROLE_BOUNDARY = "boundary"


@dataclass
class PendingEntry:
    """One place to rest an order, fully priced."""

    role: str
    label: str  # where the price comes from, in the owner's words
    direction: Direction
    entry: float
    stop_loss: float
    targets: List[TakeProfit] = field(default_factory=list)
    zone: Optional[Tuple[float, float]] = None  # the band, when there is one
    kind: str = ""  # Zone.kind of that band ("OB"/"FVG"/"RANGE"), or ""

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)


@dataclass
class PendingAnalysis:
    """What the Setup-analysis button answers with."""

    direction: Optional[Direction]
    entries: List[PendingEntry] = field(default_factory=list)
    # The "enter now" reference — set only once a setup has formed, so the
    # analysis can put the market entry the 🚨 alert quoted next to the
    # limit alternatives. None while the bot is still waiting.
    market: Optional[PendingEntry] = None
    # Why there is nothing pending, in the engine's own words, when there is
    # nothing — an empty `entries` always comes with a note.
    note: Optional[str] = None
    range_mode: bool = False

    @property
    def main(self) -> Optional[PendingEntry]:
        return next((e for e in self.entries if e.role == ROLE_MAIN), None)

    @property
    def deep(self) -> Optional[PendingEntry]:
        return next((e for e in self.entries if e.role == ROLE_DEEP), None)


@dataclass
class Approach:
    """Price is almost at the zone Rule 2 is waiting at (the get-ready
    moment, D25). `distance` is measured from the near edge and is 0.0
    while price is inside the band."""

    direction: Direction
    zone_bottom: float
    zone_top: float
    kind: str
    distance: float
    height: float
    inside: bool
    bracket: PendingEntry  # the projected limit order at the zone
    range_box: Optional[Range] = None


# ----------------------------------------------------------------- helpers


def _is_long(direction: Direction) -> bool:
    return direction == Direction.LONG


def _levels(
    instrument: Instrument,
    h4: Sequence[Candle],
    h1: Sequence[Candle],
    m5: Optional[Sequence[Candle]] = None,
) -> List[LiquidityLevel]:
    """The pools the targets are picked from. M5 joins only once a setup has
    formed — hours before price reaches a zone its swings will have been
    swept long before the entry, which is also why `plan.py` never scans it.
    Tolerance is the raw per-instrument minimum FVG, like every other sweep
    measurement in this package."""
    tolerance = instrument.min_fvg
    levels = (
        find_liquidity(list(h1), "H1", tolerance)
        + find_liquidity(list(h4), "H4", tolerance)
    )
    if m5:
        levels = find_liquidity(list(m5), "M5", tolerance) + levels
    return levels


def _range_targets(
    box: Range, direction: Direction, entry: float, stop_loss: float,
    sl_buffer: float,
) -> List[TakeProfit]:
    """D14: a range trade has ONE objective, the opposite boundary one
    buffer short, full size. Empty when it lands inside the buffer."""
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return []
    if _is_long(direction):
        tp = box.top - sl_buffer
        reward = tp - entry
    else:
        tp = box.bottom + sl_buffer
        reward = entry - tp
    if reward <= 0:
        return []
    return [TakeProfit(price=tp, rr=reward / risk, level=None)]


def _zone_bracket(
    zone: Zone, direction: Direction, sl_buffer: float
) -> Tuple[float, float]:
    """The plan's preliminary bracket for a zone nobody has traded yet: the
    near edge as the entry, one buffer beyond the far edge as the stop —
    exactly `plan._scenario`'s geometry, so the approach message and the
    morning plan can never quote different numbers for one zone. The live
    🚨 alert re-anchors the stop to the swept extreme (Rule 6)."""
    if _is_long(direction):
        return zone.top, zone.bottom - sl_buffer
    return zone.bottom, zone.top + sl_buffer


def _boundary_bracket(
    box: Range, direction: Direction, sl_buffer: float
) -> Tuple[float, float]:
    """A range boundary's bracket (D11): the boundary itself is the entry and
    the stop sits one buffer beyond it — `plan._range_scenario`'s geometry."""
    if _is_long(direction):
        return box.bottom, box.bottom - sl_buffer
    return box.top, box.top + sl_buffer


def _pending_side(direction: Direction, entry: float, price: float) -> bool:
    """A limit order rests on the pullback side of price: below it for a
    LONG, above for a SHORT. An entry price has already passed is a market
    order in disguise, not a pending one."""
    return entry < price if _is_long(direction) else entry > price


def _well_formed(direction: Direction, entry: float, stop_loss: float) -> bool:
    """Entry on the trading side of its own stop, with a risk to measure —
    the same two geometry checks Rule 6 applies before a setup is announced."""
    if abs(entry - stop_loss) <= 0:
        return False
    return entry > stop_loss if _is_long(direction) else entry < stop_loss


def _further_stop(direction: Direction, a: float, b: float) -> float:
    """The further-out of two stops on the trade's own side."""
    return min(a, b) if _is_long(direction) else max(a, b)


def _zone_name(zone: Zone) -> str:
    """Short, because it is a column header cell inside a <pre> block on a
    phone: 'H1 Demand OB', 'H1 Supply FVG', 'Range LOW'."""
    if zone.kind == "RANGE":
        return "Range LOW" if zone.is_demand else "Range HIGH"
    return f"H1 {'Demand' if zone.is_demand else 'Supply'} {zone.kind}"


def _pick_two(
    direction: Direction, rungs: List[PendingEntry]
) -> List[PendingEntry]:
    """MAIN is the shallowest pending rung, DEEP the deepest one whose price
    differs from it. Rungs at one price collapse — two names for one order
    are one rung. One rung alone is MAIN with no DEEP."""
    if not rungs:
        return []
    # shallowest first: highest entry for a LONG, lowest for a SHORT
    ordered = sorted(
        rungs, key=lambda r: -r.entry if _is_long(direction) else r.entry
    )
    seen: List[PendingEntry] = []
    for rung in ordered:
        if any(abs(rung.entry - s.entry) < 1e-9 for s in seen):
            continue
        seen.append(rung)
    main = seen[0]
    main.role = ROLE_MAIN
    if len(seen) == 1:
        return [main]
    deep = seen[-1]
    deep.role = ROLE_DEEP
    return [main, deep]


# ------------------------------------------------------------ build_pending


def build_pending(
    result: AnalysisResult,
    instrument: Instrument,
    h4: Sequence[Candle],
    h1: Sequence[Candle],
    m5: Sequence[Candle],
) -> PendingAnalysis:
    """Price the pending entries for the state the engine left `result` in.

    * A completed setup (APPROVED): the rungs Rule 5/D22 already knows —
      the M5 imbalance edge and its 50% level, the M5 order block, the H1
      zone of interest, the untested zones further out — priced from the
      setup's own Rule 6 stop (or the zone's far edge when that is further
      out), with TP1-3 off the same M5/H1/H4 ladder the alert shows. Plus
      the market reference the 🚨 alert told the owner to take.
    * Waiting at or for a zone (WATCH with an H1 zone): the zone's plan
      bracket as MAIN and the next untested zone on the same side as DEEP,
      targets from H1/H4 liquidity only.
    * Mid-range (WATCH with a live box and no boundary in play, D11): one
      bracket per boundary, SHORT at the HIGH and LONG at the LOW, each
      aiming at the opposite boundary (D14).
    * Anything else: no entries, and the engine's first reason as the note.

    Rungs price has already passed are dropped (they would be market
    orders), as are malformed ones (Rule 6's geometry checks).
    """
    price = result.price or (m5[-1].close if m5 else 0.0)
    buffer = instrument.sl_buffer
    setup = result.setup
    box = result.market_range
    range_mode = result.direction_source == "range" and box is not None

    if result.verdict in APPROVED and setup is not None:
        direction = setup.direction
        levels = _levels(instrument, h4, h1, m5)

        def priced(label, entry, stop, zone=None, kind="") -> Optional[PendingEntry]:
            if not _well_formed(direction, entry, stop):
                return None
            targets = (
                _range_targets(box, direction, entry, stop, buffer)
                if range_mode
                else take_profits(levels, direction, entry, stop, buffer)
            )
            return PendingEntry(
                role="", label=label, direction=direction, entry=entry,
                stop_loss=stop, targets=targets, zone=zone, kind=kind,
            )

        rungs: List[PendingEntry] = []
        if setup.fvg is not None:
            band = (setup.fvg.bottom, setup.fvg.top)
            rungs.append(priced("M5 FVG edge", setup.entry, setup.stop_loss, band, "FVG"))
            rungs.append(priced(
                "M5 FVG 50%", (setup.fvg.bottom + setup.fvg.top) / 2,
                setup.stop_loss, band, "FVG",
            ))
        elif setup.entry_source == "ob":
            # D22's second rung: the block IS the entry, and the engine
            # leaves `order_block` empty — nothing deeper on M5 to add.
            rungs.append(priced("M5 OB", setup.entry, setup.stop_loss, None, "OB"))
        if setup.order_block is not None:
            ob = setup.order_block
            edge = ob.top if _is_long(direction) else ob.bottom
            rungs.append(priced(
                "M5 OB", edge, setup.stop_loss, (ob.bottom, ob.top), "OB",
            ))
        zone = result.h1_zone
        if zone is not None and range_mode:
            # The boundary itself, with the setup's own stop — Rule 6 already
            # put it beyond the boundary or the swept extreme, whichever is
            # further (engine.py).
            edge, _ = _boundary_bracket(box, direction, buffer)
            rungs.append(priced(
                _zone_name(zone), edge, setup.stop_loss, (zone.bottom, zone.top), "RANGE",
            ))
        elif zone is not None:
            edge, far_stop = _zone_bracket(zone, direction, buffer)
            rungs.append(priced(
                _zone_name(zone), edge,
                _further_stop(direction, setup.stop_loss, far_stop),
                (zone.bottom, zone.top), zone.kind,
            ))
        for deeper in setup.zones_ahead[:2]:
            edge, far_stop = _zone_bracket(deeper, direction, buffer)
            rungs.append(priced(
                _zone_name(deeper) + " · next", edge,
                _further_stop(direction, setup.stop_loss, far_stop),
                (deeper.bottom, deeper.top), deeper.kind,
            ))
        pending = [
            r for r in rungs
            if r is not None and _pending_side(direction, r.entry, price)
        ]
        market = priced("market", price, setup.stop_loss)
        if market is not None:
            market.role = ROLE_MARKET
        entries = _pick_two(direction, pending)
        note = None
        if not entries:
            note = (
                "price has run past every limit rung of this setup — the "
                "market entry is what is left"
            )
        return PendingAnalysis(
            direction=direction, entries=entries, market=market, note=note,
            range_mode=range_mode,
        )

    if result.verdict == Verdict.WATCH and result.h1_zone is not None:
        zone = result.h1_zone
        direction = Direction.LONG if zone.is_demand else Direction.SHORT
        levels = _levels(instrument, h4, h1)
        rungs = []
        if zone.kind == "RANGE" and box is not None:
            entry, stop = _boundary_bracket(box, direction, buffer)
            if _well_formed(direction, entry, stop):
                rungs.append(PendingEntry(
                    role="", label=_zone_name(zone), direction=direction,
                    entry=entry, stop_loss=stop,
                    targets=_range_targets(box, direction, entry, stop, buffer),
                    zone=(zone.bottom, zone.top), kind="RANGE",
                ))
            entries = _pick_two(direction, [
                r for r in rungs if _pending_side(direction, r.entry, price)
            ])
            return PendingAnalysis(
                direction=direction, entries=entries, range_mode=True,
                note=None if entries else (result.reasons[0] if result.reasons else None),
            )
        entry, stop = _zone_bracket(zone, direction, buffer)
        for label, at in (
            (_zone_name(zone), entry),
            (_zone_name(zone) + " 50%", (zone.bottom + zone.top) / 2),
        ):
            if _well_formed(direction, at, stop):
                rungs.append(PendingEntry(
                    role="", label=label, direction=direction, entry=at,
                    stop_loss=stop,
                    targets=take_profits(levels, direction, at, stop, buffer),
                    zone=(zone.bottom, zone.top), kind=zone.kind,
                ))
        for deeper in zone_ladder(list(h1), direction, entry, exclude=zone)[:2]:
            d_entry, d_stop = _zone_bracket(deeper, direction, buffer)
            if _well_formed(direction, d_entry, d_stop):
                rungs.append(PendingEntry(
                    role="", label=_zone_name(deeper) + " · next",
                    direction=direction, entry=d_entry, stop_loss=d_stop,
                    targets=take_profits(levels, direction, d_entry, d_stop, buffer),
                    zone=(deeper.bottom, deeper.top), kind=deeper.kind,
                ))
        entries = _pick_two(direction, [
            r for r in rungs if _pending_side(direction, r.entry, price)
        ])
        return PendingAnalysis(
            direction=direction, entries=entries,
            note=None if entries else (result.reasons[0] if result.reasons else None),
        )

    if result.verdict == Verdict.WATCH and box is not None:
        # Mid-range (D11): both edges, each its own trade.
        entries = []
        for direction in (Direction.SHORT, Direction.LONG):
            entry, stop = _boundary_bracket(box, direction, buffer)
            if not _well_formed(direction, entry, stop):
                continue
            band = boundary_zone(box, direction, instrument.min_fvg)
            entries.append(PendingEntry(
                role=ROLE_BOUNDARY, label=_zone_name(band), direction=direction,
                entry=entry, stop_loss=stop,
                targets=_range_targets(box, direction, entry, stop, buffer),
                zone=(band.bottom, band.top), kind="RANGE",
            ))
        return PendingAnalysis(
            direction=None, entries=entries, range_mode=True,
            note=None if entries else (result.reasons[0] if result.reasons else None),
        )

    return PendingAnalysis(
        direction=None,
        note=result.reasons[0] if result.reasons else "no direction",
    )


# ------------------------------------------------------------ approach_read


def approach_read(
    result: AnalysisResult,
    instrument: Instrument,
    h4: Sequence[Candle],
    h1: Sequence[Candle],
    factor: float = 1.0,
) -> Optional[Approach]:
    """Is price almost at the zone the checklist is waiting at?

    Reads the engine's own Rule 2 zone (`result.h1_zone`) — the plan's
    scenario zone is the same object by construction — or, mid-range, the
    nearer boundary. "Almost" is a distance from the near edge of at most
    `factor` zone-heights, floored at two min-FVG units so a thin band (a
    range boundary is one tolerance thick) still gives a heads-up before
    the touch. Inside the band counts as distance zero: the message must
    fire when price arrives even if it skipped the approach.

    None when nothing is being waited at (no zone, no box), when the
    verdict is not a WATCH (a completed setup has its own 🚨 message, a
    SKIP has nothing to get ready for), or when price sits BEYOND the zone
    — below a demand zone, above a supply one — which is not an approach
    but a zone price has left behind.
    """
    if result.verdict != Verdict.WATCH or not result.price:
        return None
    price = result.price
    buffer = instrument.sl_buffer
    box = result.market_range
    zone = result.h1_zone
    if zone is None:
        if box is None:
            return None
        # mid-range: the boundary price is closer to
        direction = (
            Direction.SHORT if box.top - price <= price - box.bottom
            else Direction.LONG
        )
        zone = boundary_zone(box, direction, instrument.min_fvg)
    direction = Direction.LONG if zone.is_demand else Direction.SHORT
    height = zone.top - zone.bottom
    if _is_long(direction):
        if price < zone.bottom:
            return None
        distance = max(0.0, price - zone.top)
    else:
        if price > zone.top:
            return None
        distance = max(0.0, zone.bottom - price)
    threshold = factor * max(height, 2 * instrument.min_fvg)
    if distance > threshold:
        return None
    if zone.kind == "RANGE" and box is not None:
        entry, stop = _boundary_bracket(box, direction, buffer)
        targets = _range_targets(box, direction, entry, stop, buffer)
    else:
        entry, stop = _zone_bracket(zone, direction, buffer)
        targets = take_profits(
            _levels(instrument, h4, h1), direction, entry, stop, buffer,
        )
    if not _well_formed(direction, entry, stop):
        return None
    bracket = PendingEntry(
        role=ROLE_MAIN, label=_zone_name(zone), direction=direction,
        entry=entry, stop_loss=stop, targets=targets,
        zone=(zone.bottom, zone.top), kind=zone.kind,
    )
    return Approach(
        direction=direction, zone_bottom=zone.bottom, zone_top=zone.top,
        kind=zone.kind, distance=distance, height=height,
        inside=distance == 0.0 and zone.bottom <= price <= zone.top,
        bracket=bracket, range_box=box if zone.kind == "RANGE" else None,
    )
