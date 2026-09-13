"""The AI read (owner decision D26, 2026-09-06): a second opinion from Claude
on top of the rule engine — never a gate.

The engine decides whether a setup exists (rules 1-6, detector mode) and
prices every level. Claude is then shown the SAME picture the owner sees —
the chart PNGs plus a plain-text fact sheet of the engine's own numbers —
and asked to comment as a senior Smart-Money analyst: does the context
agree, what could go wrong, which of the priced entries it would prefer,
how confident it is. The answer is appended to the 🚨 alert (by editing the
card after it was sent, so the alert itself is never delayed) and stored
with the Strategy audit at 08:05/14:05.

Strict boundaries, in order:

* **Comment only.** Nothing here suppresses, delays, re-labels or re-prices
  a setup. `stance == "against"` is a sentence in a message, not a verdict.
* **Best-effort.** Every failure — no key, network, refusal, a malformed
  answer — returns None and the bot behaves exactly as before D26.
* **No invented numbers.** The fact sheet is the only source of levels; the
  prompt says so and the answer is bounded by a JSON schema.
* **Pure below the HTTP call.** `describe_for_ai` and `parse_ai_read` are
  plain functions on engine objects, unit-testable without a client.

`anthropic` is imported lazily: the watcher must start (and every test must
run) on a box without the package or a key — the read simply stays off.

**The AI setup** (owner decision D28, 2026-09-13). On top of the comment,
Claude now proposes ONE concrete order — limit by default, market only
when price is still at the rung — with an entry, a stop, a target and the
band it rests on. The numbers are bounded twice: the prompt lists the only
levels it may use (`LevelCatalog`, printed into the fact sheet), and
`validate_proposal` snaps every price back onto that catalog within the
instrument's own tolerance and computes the RR itself. A proposal that
does not fit the catalog is dropped — the read survives without it. The
floor is `min_rr` (SMC_AI_MIN_RR, 1:2 by the owner's choice): a proposal
under it is still shown, flagged "below 1:2 — wait", so the owner sees
what the model liked and why the rule says no. Still a comment: the
proposal is drawn on the chart and printed under the card, it never
changes what the engine announced.
"""

import base64
import json
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from app.services.smc.i18n import get_language
from app.services.smc.instruments import Instrument
from app.services.smc.models import AnalysisResult, Candle, Direction, Verdict
from app.services.smc.sessions import session_end_utc, to_prague

logger = structlog.get_logger(__name__)

# Owner decision D28 (2026-09-13): Opus 5 at xhigh — a /plan press is a
# handful of calls a day, and the setup proposal is reasoning over levels,
# which is exactly where the stronger model earns its price.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "xhigh"
DEFAULT_MIN_RR = 2.0

STANCES = ("agree", "caution", "against")
ENTRIES = ("market", "main", "deep", "wait")
ORDERS = ("limit", "market", "none")
# Where a proposed entry rests, in the strategy's own vocabulary. Every
# band in `LevelCatalog.entries` carries one of these, so the model's label
# and the code's snapping speak the same names.
BASES = ("m5_fvg", "m5_ob", "h1_zone", "zone_next", "range_boundary", "market", "none")
DIRECTIONS = ("long", "short", "none")

# How many closed candles the fact sheet prints as numbers (D28): the
# model used to read swings off the PNG by eye; with OHLC rows it can
# place a level on an actual wick. ~3 hours of M5 and a day of H1.
FACT_M5_CANDLES = 36
FACT_H1_CANDLES = 24

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "order": {"type": "string", "enum": list(ORDERS)},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "basis": {"type": "string", "enum": list(BASES)},
        "entry": {"type": ["number", "null"]},
        "stop": {"type": ["number", "null"]},
        "target": {"type": ["number", "null"]},
        "invalidation": {"type": "string"},
    },
    "required": ["order", "direction", "basis", "entry", "stop", "target", "invalidation"],
    "additionalProperties": False,
}

# The answer's shape. Kept to type/enum/required so any structured-output
# validator accepts it; numeric clamps happen in `parse_ai_read`.
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": list(STANCES)},
        "preferred_entry": {"type": "string", "enum": list(ENTRIES)},
        "confidence": {"type": "integer"},
        "read": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "proposal": PROPOSAL_SCHEMA,
    },
    "required": ["stance", "preferred_entry", "confidence", "read", "risks", "proposal"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a senior Smart Money Concepts (ICT-style) analyst giving a second \
opinion to a discretionary trader. His rule engine has already read the \
charts and priced every level; you are shown its fact sheet and the charts.

His system, "Triple Sync + Imbalance": direction from the H4 trend (HH+HL / \
LH+LL), or from H1 when H4 is flat, or a range between clustered H1 pivots \
when both are flat; an H1 zone of interest (an untested order block, or an \
untouched H1 imbalance); a pullback into that zone; an M5 change of \
character (CHoCH) in the trade direction; the M5 imbalance of the impulse \
supplies the best limit entry (else the M5 order block, else market); the \
stop sits beyond the swept extreme of the pullback; targets are the nearest \
unswept liquidity pools (swing highs/lows, equal highs/lows). Sessions \
08:00-18:30 Prague. A "sniper" setup also needs room to the first pool, a \
liquidity sweep before the CHoCH, and an entry in the discount (long) or \
premium (short) half of the dealing range.

When the fact sheet carries YOUR EARLIER PLAN for the pair (the trader's \
/plan press, his primary picture for the day), treat it as the reference: \
say whether this setup is the one you planned and, if not, what changed. \
Your job is to COMMENT, never to decide: say whether the higher-timeframe \
context and the order flow agree with the setup, what could go wrong (a \
sweep still ahead, an opposing zone or pool on the way to TP1, premium vs \
discount, a counter-trend read, news), and which of the priced entries you \
would prefer — "market" (enter now), "main" (the shallow limit), "deep" \
(the deeper limit) or "wait" (no entry you like). Use ONLY the levels in the \
fact sheet; never invent prices. Be concrete and brief: `read` is at most \
three sentences (under 450 characters), each risk under 90 characters, at \
most three risks. Plain prose, no markdown, no emoji. Write `read` and \
`risks` in the language the message asks for (English unless told \
otherwise); `stance` and `preferred_entry` stay the English enum values. \
Confidence is 1 (weak) to 5 (strong).

Then propose ONE order in `proposal` — the trade you would actually place \
right now, as a limit order resting at one of the ALLOWED ENTRY BANDS the \
fact sheet lists (its `basis` names the band). A market order is allowed \
only when price is still inside or within tolerance of a band. The stop \
must be one of the ALLOWED STOPS, the target one of the ALLOWED TARGETS \
(prefer the nearest unswept pool that still pays); the trader's floor is \
the MINIMUM RR the fact sheet states — if no allowed entry reaches it, \
answer order "none" with direction "none", null prices, and say in `read` \
what would have to happen for a trade to appear. `invalidation` is one \
short sentence (under 120 characters): the candle event that cancels the \
order. Every price you give is checked against the allowed lists and \
snapped or rejected; a rejected proposal is simply dropped.

When the fact sheet says RE-READ, the picture changed after your previous \
read (the trigger is named): say plainly whether your previous read and \
order still stand, and if not, what changed and what you would do now — \
keep the order when nothing material moved; do not re-price it for the \
sake of novelty. YOUR PENDING ORDERS lists the orders you proposed earlier \
that are still resting or open; a new proposal at the same price means \
"keep it", order "none" means "pull it"."""

# The per-language instruction appended to the user turn (owner request
# 2026-09-10: the read follows the bot's language). The JSON enums are
# untouched — the parser and the card keep working on English codes.
LANGUAGE_INSTRUCTION = {
    "ru": (
        "Write `read` and `risks` in Russian (по-русски, trader's vocabulary: "
        "CHoCH, FVG, OB, LONG/SHORT, SL/TP stay Latin)."
    ),
    "en": "Write `read` and `risks` in English.",
}


@dataclass
class AIProposal:
    """The one order Claude would place (D28), after validation: every
    price snapped onto the level catalog, RR computed here, never by the
    model. `order == "none"` is a deliberate "no trade" and carries no
    prices."""

    order: str  # limit | market | none
    direction: str  # long | short | none
    basis: str  # one of BASES
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    invalidation: str = ""
    rr: float = 0.0
    below_floor: bool = False  # rr < min_rr — shown, flagged, never hidden

    @property
    def is_trade(self) -> bool:
        return self.order != "none" and self.entry is not None

    def to_dict(self) -> dict:
        return {
            "order": self.order, "direction": self.direction, "basis": self.basis,
            "entry": self.entry, "stop": self.stop, "target": self.target,
            "invalidation": self.invalidation, "rr": round(self.rr, 2),
            "below_floor": self.below_floor,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["AIProposal"]:
        if not isinstance(raw, dict) or raw.get("order") not in ORDERS:
            return None
        try:
            return cls(
                order=str(raw["order"]),
                direction=str(raw.get("direction") or "none"),
                basis=str(raw.get("basis") or "none"),
                entry=float(raw["entry"]) if raw.get("entry") is not None else None,
                stop=float(raw["stop"]) if raw.get("stop") is not None else None,
                target=float(raw["target"]) if raw.get("target") is not None else None,
                invalidation=str(raw.get("invalidation") or ""),
                rr=float(raw.get("rr") or 0.0),
                below_floor=bool(raw.get("below_floor")),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class AIRead:
    stance: str
    preferred_entry: str
    confidence: int
    read: str
    risks: List[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    as_of: str = ""  # Prague HH:MM the read was made
    # D28: the proposed order, None when the model gave none or the one it
    # gave did not fit the catalog (`proposal_note` says which, for logs).
    proposal: Optional[AIProposal] = None
    proposal_note: str = ""


@dataclass
class LevelCatalog:
    """Every price the model may use (D28), built from the same objects
    the card and the audit print — so a proposal can never name a level
    the owner cannot see. Bands are (basis, low, high); a single price is
    a band of zero height. `tolerance` is the raw per-instrument min_fvg,
    the sweep tolerance the rest of the engine uses."""

    direction: Optional[Direction]
    price: float
    tolerance: float
    entries: List[Tuple[str, float, float]] = field(default_factory=list)
    stops: List[float] = field(default_factory=list)
    targets: List[float] = field(default_factory=list)
    decimals: int = 2

    def _add_entry(self, basis: str, low: float, high: float) -> None:
        lo, hi = (low, high) if low <= high else (high, low)
        if (basis, lo, hi) not in self.entries:
            self.entries.append((basis, lo, hi))

    def _add(self, bucket: List[float], value: Optional[float]) -> None:
        if value is None:
            return
        if all(abs(value - v) > 1e-9 for v in bucket):
            bucket.append(float(value))


def build_catalog(
    result: AnalysisResult, instrument: Instrument, audit: Any = None,
) -> LevelCatalog:
    """The allowed bands, stops and targets for this state of the pair."""
    setup = result.setup
    direction = setup.direction if setup is not None else (
        getattr(audit, "direction", None) if audit is not None else None
    )
    cat = LevelCatalog(
        direction=direction, price=float(result.price or 0.0),
        tolerance=float(instrument.min_fvg), decimals=instrument.price_decimals,
    )
    zone = result.h1_zone
    formed = result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET) and setup
    if formed:
        cat._add_entry("market", cat.price, cat.price)
        if setup.fvg is not None:
            cat._add_entry("m5_fvg", setup.fvg.bottom, setup.fvg.top)
        elif setup.entry_source == "ob":
            cat._add_entry("m5_ob", setup.entry, setup.entry)
        if setup.order_block is not None:
            cat._add_entry("m5_ob", setup.order_block.bottom, setup.order_block.top)
        cat._add(cat.stops, setup.stop_loss)
        cat._add(cat.targets, setup.take_profit)
        for lv in setup.ladder:
            cat._add(cat.targets, lv.price)
        for z in setup.zones_ahead:
            cat._add_entry("zone_next", z.bottom, z.top)
    if zone is not None:
        cat._add_entry(
            "range_boundary" if zone.kind == "RANGE" else "h1_zone",
            zone.bottom, zone.top,
        )
    if audit is not None:
        for e in getattr(audit, "entries", None) or []:
            basis = {
                "RANGE": "range_boundary", "FVG": "m5_fvg", "OB": "m5_ob",
            }.get(e.kind, "h1_zone")
            if e.zone is not None:
                if e.kind in ("OB", "FVG") and zone is not None and (
                    min(e.zone) >= min(zone.bottom, zone.top) - cat.tolerance
                    and max(e.zone) <= max(zone.bottom, zone.top) + cat.tolerance
                    and getattr(e, "role", "") == "deep"
                ):
                    basis = "h1_zone"
                if "next" in (e.label or ""):
                    basis = "zone_next"
                cat._add_entry(basis, e.zone[0], e.zone[1])
            else:
                cat._add_entry(basis, e.entry, e.entry)
            cat._add(cat.stops, e.stop_loss)
            for tp in e.targets:
                cat._add(cat.targets, tp.price)
        market = getattr(audit, "market", None)
        if market is not None:
            cat._add_entry("market", market.entry, market.entry)
            cat._add(cat.stops, market.stop_loss)
            for tp in market.targets:
                cat._add(cat.targets, tp.price)
    return cat


# ------------------------------------------------------------- fact sheet


def _fmt(value: Optional[float], d: int) -> str:
    return "n/a" if value is None else f"{value:.{d}f}"


def _candle_rows(candles: Sequence[Candle], count: int, d: int, fmt: str) -> str:
    rows = list(candles)[-count:]
    return "; ".join(
        f"{to_prague(c.timestamp).strftime(fmt)} "
        f"{c.open:.{d}f}/{c.high:.{d}f}/{c.low:.{d}f}/{c.close:.{d}f}"
        for c in rows
    )


def _day_level_lines(result: AnalysisResult, d: int) -> List[str]:
    """PDH/PDL and today's Asia range off the same candles the ⭐'s sweep
    label reads (sniper.py) — the day-level pools the model kept asking
    about without being given."""
    from app.services.smc import sniper

    m5 = list(result.m5_candles or [])
    h1 = list(result.h1_candles or [])
    if not m5 and not h1:
        return []
    rows = sniper._session_candles(m5, h1, result.checked_at)
    if not rows:
        return []
    today = sniper._prague_date(result.checked_at)
    days = sorted({sniper._prague_date(c.timestamp) for c in rows if sniper._prague_date(c.timestamp) < today})
    out = []
    if days:
        prev = sniper._day_extremes(rows, days[-1])
        if prev is not None:
            out.append(
                f"Previous day ({days[-1].strftime('%d.%m')}): PDL {_fmt(prev[0], d)}, "
                f"PDH {_fmt(prev[1], d)}"
            )
    asia = sniper._asia_extremes(rows, today)
    if asia is not None:
        out.append(f"Asia range today (00:00-08:00 Prague): {_fmt(asia[0], d)}-{_fmt(asia[1], d)}")
    return out


def describe_catalog(catalog: LevelCatalog, min_rr: float) -> str:
    """The ALLOWED lists the prompt refers to, in the fact sheet's own
    words. Printed last so the model reads the context first."""
    d = catalog.decimals
    lines = ["", "ALLOWED ENTRY BANDS (basis: low-high):"]
    for basis, lo, hi in catalog.entries:
        lines.append(
            f"  {basis}: {_fmt(lo, d)}" + (f"-{_fmt(hi, d)}" if hi != lo else "")
        )
    lines.append("ALLOWED STOPS: " + (", ".join(_fmt(p, d) for p in catalog.stops) or "none"))
    lines.append("ALLOWED TARGETS: " + (", ".join(_fmt(p, d) for p in catalog.targets) or "none"))
    lines.append(
        f"Tolerance: {_fmt(catalog.tolerance, d)}. MINIMUM RR: 1:{min_rr:.1f} to the target "
        "(below it: order none)."
        + (
            f" Trade direction is fixed: {'LONG' if catalog.direction == Direction.LONG else 'SHORT'}."
            if catalog.direction is not None else
            " No direction yet (range mid-box): pick the boundary you would trade and its direction."
        )
    )
    return "\n".join(lines)


def describe_orders(orders: Sequence[dict], d: int) -> List[str]:
    """YOUR PENDING ORDERS — the shadow rows still in play, in the fact
    sheet's words (D28 re-read). Empty list when there are none."""
    lines = []
    for o in orders:
        try:
            lines.append(
                f"  {'open (filled)' if o.get('status') == 'open' else 'pending (resting)'}: "
                f"{str(o.get('direction') or '').upper()} at {_fmt(float(o['entry']), d)}, "
                f"stop {_fmt(float(o['stop_loss']), d)}, target {_fmt(o.get('take_profit'), d)}"
                f" (proposed {to_prague(datetime.fromisoformat(o['created_at'])).strftime('%H:%M')} "
                f"Prague from the {o.get('profile_key') or 'alert'} read)"
            )
        except (KeyError, TypeError, ValueError):
            continue
    return (["YOUR PENDING ORDERS:"] + lines) if lines else []


def describe_reread(trigger: str, previous: Optional["AIRead"], d: int) -> List[str]:
    """The RE-READ block (D28 re-read): what changed and what the model
    said last time, so it can say whether that still stands."""
    lines = ["", f"RE-READ, trigger: {trigger}"]
    if previous is not None:
        lines.append(
            f"Your previous read ({previous.as_of or 'earlier'} Prague): stance "
            f"{previous.stance}, preferred {previous.preferred_entry}, confidence "
            f"{previous.confidence}; read: {previous.read}"
        )
        p = previous.proposal
        if p is not None and p.is_trade:
            lines.append(
                f"Your previous order: {p.order} {p.direction.upper()} at {_fmt(p.entry, d)}, "
                f"stop {_fmt(p.stop, d)}, target {_fmt(p.target, d)} (1:{p.rr:.1f})"
            )
        elif p is not None:
            lines.append("Your previous order: none (wait)")
    lines.append("Say whether that still stands.")
    return lines


def describe_for_ai(
    result: AnalysisResult, instrument: Instrument, audit: Any = None,
    plan: Any = None, news: Optional[str] = None,
    catalog: Optional[LevelCatalog] = None, min_rr: float = DEFAULT_MIN_RR,
    candles: bool = True, orders: Sequence[dict] = (),
    reread: Optional[str] = None, previous: Optional["AIRead"] = None,
) -> str:
    """The engine's picture as plain text — the ONLY source of numbers the
    model may quote. Same objects the alert and the audit print, so the
    read can never disagree with the message it is attached to.

    D28 additions: the day levels (PDH/PDL, Asia), the next red-news line
    the watcher hands in, the recent candles as OHLC rows, and — when a
    `catalog` is given — the ALLOWED lists the proposal is validated
    against, with the RR floor."""
    d = instrument.price_decimals
    lines = [
        f"Pair: {result.symbol}",
        f"Time: {to_prague(result.checked_at).strftime('%Y-%m-%d %H:%M')} Prague"
        + (f", session {result.session_name}" if result.session_name else ", off session"),
        f"Price: {_fmt(result.price, d)}",
    ]
    if result.session_name:
        end = session_end_utc(result.checked_at)
        if end is not None:
            minutes = max(int((end - result.checked_at).total_seconds() // 60), 0)
            lines.append(
                f"Session ends at {to_prague(end).strftime('%H:%M')} Prague "
                f"({minutes} min left); a pending order placed now expires then "
                "(Rule 10)"
            )
    lines += [
        f"H4 trend: {result.h4_trend.value}; H1 trend: "
        f"{result.h1_trend.value if result.h1_trend is not None else 'n/a'}; "
        f"direction source: {result.direction_source}",
    ]
    box = result.market_range
    if box is not None:
        lines.append(f"Range box: {_fmt(box.bottom, d)}-{_fmt(box.top, d)}")
    zone = result.h1_zone
    if zone is not None:
        side = "demand" if zone.is_demand else "supply"
        lines.append(
            f"H1 zone of interest: {side} {zone.kind} "
            f"{_fmt(zone.bottom, d)}-{_fmt(zone.top, d)}, touches {zone.touches}"
        )
    pd = result.pd
    if pd is not None:
        lines.append(
            f"Premium/discount: {pd.pct}% {pd.label} of the {pd.range.timeframe} "
            f"range {_fmt(pd.range.low, d)}-{_fmt(pd.range.high, d)}; "
            f"OTE {_fmt(pd.ote_low, d)}-{_fmt(pd.ote_high, d)}"
            + (" (entry inside OTE)" if pd.in_ote else "")
        )
    setup = result.setup
    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET) and setup:
        is_long = setup.direction == Direction.LONG
        lines.append(
            f"SETUP FORMED: {'LONG' if is_long else 'SHORT'}, entry rung "
            f"{setup.entry_source} at {_fmt(setup.entry, d)}, stop {_fmt(setup.stop_loss, d)}, "
            f"risk {_fmt(abs(setup.entry - setup.stop_loss), d)}"
        )
        if setup.fvg is not None:
            lines.append(
                f"M5 imbalance: {_fmt(setup.fvg.bottom, d)}-{_fmt(setup.fvg.top, d)}, "
                f"{setup.fvg.fill_pct * 100:.0f}% filled"
            )
        elif setup.rejected_fvg is not None:
            lines.append(
                f"M5 imbalance rejected: {_fmt(setup.rejected_fvg.bottom, d)}-"
                f"{_fmt(setup.rejected_fvg.top, d)} "
                f"({', '.join(setup.rejected_fvg_problems) or 'not valid'})"
            )
        else:
            lines.append("M5 imbalance: none in the impulse")
        if setup.order_block is not None:
            lines.append(
                f"M5 order block: {_fmt(setup.order_block.bottom, d)}-"
                f"{_fmt(setup.order_block.top, d)}"
            )
        if setup.ladder:
            pools = "; ".join(
                f"{lv.timeframe} {'high' if lv.is_high else 'low'} {_fmt(lv.price, d)}"
                + (f" (EQ x{lv.equal_count})" if lv.equal_count > 1 else "")
                for lv in setup.ladder
            )
            lines.append(f"Unswept liquidity ahead: {pools}")
        else:
            lines.append("Unswept liquidity ahead: none")
        if setup.zones_ahead:
            lines.append(
                "Untested zones further out: " + "; ".join(
                    f"{z.kind} {_fmt(z.bottom, d)}-{_fmt(z.top, d)}"
                    for z in setup.zones_ahead
                )
            )
        lines.append(
            "Tier: " + ("sniper (star)" if setup.tier_star else
                        "regular, star missed on: " + (", ".join(setup.tier_missed) or "n/a"))
        )
        if result.warnings:
            lines.append("Engine warnings: " + "; ".join(result.warnings))
        if result.funding_warning:
            lines.append("Funding: " + result.funding_warning)
    else:
        lines.append(
            f"Checklist state: {result.verdict.value} — "
            + (result.reasons[0] if result.reasons else "no detail")
        )
    if audit is not None and getattr(audit, "entries", None):
        for e in audit.entries:
            tps = ", ".join(
                f"TP{i} {_fmt(tp.price, d)} ({tp.rr:.1f}R)"
                for i, tp in enumerate(e.targets, start=1)
            ) or "no target"
            lines.append(
                f"Pending entry [{e.role}] {e.label}: "
                f"{'LONG' if e.direction == Direction.LONG else 'SHORT'} "
                f"{_fmt(e.entry, d)}, stop {_fmt(e.stop_loss, d)}, {tps}"
            )
        market = getattr(audit, "market", None)
        if market is not None:
            lines.append(
                f"Market reference: {_fmt(market.entry, d)}, stop "
                f"{_fmt(market.stop_loss, d)}"
            )
    if plan is not None:
        # Owner decision 2026-09-10: the /plan is the primary picture, so
        # the alert read compares against it — the model sees its own
        # earlier stance and levels, and whether the engine's setup is the
        # one that plan projected.
        lines.append("")
        lines.append(
            f"YOUR EARLIER PLAN for this pair ({plan.when} Prague): direction "
            f"{(plan.direction or 'none').upper()}; pending entries MAIN "
            f"{_fmt(plan.main, d)}, DEEP {_fmt(plan.deep, d)}"
        )
        if plan.ai_stance:
            lines.append(
                f"Your stance then: {plan.ai_stance}, preferred {plan.ai_entry or 'n/a'}, "
                f"confidence {plan.ai_confidence if plan.ai_confidence is not None else 'n/a'}"
            )
        if plan.ai_read:
            lines.append(f"Your read then: {plan.ai_read}")
        if plan.ai_risks:
            lines.append("Your risks then: " + "; ".join(plan.ai_risks))
        proposal = getattr(plan, "ai_proposal", None)
        if isinstance(proposal, dict) and proposal.get("order") in ("limit", "market"):
            lines.append(
                f"Your proposed order then: {proposal['order']} "
                f"{str(proposal.get('direction') or '').upper()} at {_fmt(proposal.get('entry'), d)}, "
                f"stop {_fmt(proposal.get('stop'), d)}, target {_fmt(proposal.get('target'), d)}"
            )
        lines.append(
            "This setup MATCHES that plan's direction and zone."
            if plan.matches
            else "This setup does NOT match that plan's direction/zone."
        )
    lines.extend(_day_level_lines(result, d))
    if news:
        lines.append(news)
    if orders:
        lines.append("")
        lines.extend(describe_orders(orders, d))
    if reread:
        lines.extend(describe_reread(reread, previous, d))
    if candles:
        if result.h1_candles:
            lines.append(
                f"Recent H1 candles, oldest first, Prague time, O/H/L/C: "
                + _candle_rows(result.h1_candles, FACT_H1_CANDLES, d, "%d.%m %H:%M")
            )
        if result.m5_candles:
            lines.append(
                f"Recent M5 candles, oldest first, Prague time, O/H/L/C: "
                + _candle_rows(result.m5_candles, FACT_M5_CANDLES, d, "%H:%M")
            )
    if catalog is not None:
        lines.append(describe_catalog(catalog, min_rr))
    return "\n".join(lines)


# ---------------------------------------------------------------- parsing


def _near(value: float, level: float, tolerance: float) -> bool:
    return abs(value - level) <= tolerance + 1e-9


def _snap(value: float, levels: Sequence[float], tolerance: float) -> Optional[float]:
    """The listed level within `tolerance` of `value` (the closest), else None."""
    best = None
    for lv in levels:
        if _near(value, lv, tolerance) and (best is None or abs(value - lv) < abs(value - best)):
            best = lv
    return best


def validate_proposal(
    raw: Any, catalog: LevelCatalog, min_rr: float = DEFAULT_MIN_RR,
) -> Tuple[Optional[AIProposal], str]:
    """Snap the model's order onto the catalog, or reject it (D28).

    Returns (proposal, note). The note is English, for the logs: why the
    proposal was dropped, or "" when it stands. Rules, in order: a "none"
    order is accepted as-is; the direction must be the catalog's when it
    has one; the entry must sit inside an allowed band (±tolerance — an
    entry just outside is pulled to the edge); the stop and the target
    must each be within tolerance of a listed one and on the right side of
    the entry; "market" is only honest while price is at the entry; RR is
    computed here and compared with the floor."""
    if not isinstance(raw, dict):
        return None, "proposal missing"
    order = str(raw.get("order") or "none").lower()
    if order not in ORDERS:
        return None, f"unknown order {order!r}"
    invalidation = str(raw.get("invalidation") or "").strip()[:160]
    if order == "none":
        return AIProposal(order="none", direction="none", basis="none",
                          invalidation=invalidation), ""
    direction = str(raw.get("direction") or "none").lower()
    if catalog.direction is not None:
        wanted = catalog.direction.value
        if direction != wanted:
            return None, f"direction {direction} vs engine {wanted}"
    elif direction not in ("long", "short"):
        return None, "no direction"
    is_long = direction == "long"
    try:
        entry = float(raw["entry"])
        stop = float(raw["stop"])
        target = float(raw["target"])
    except (KeyError, TypeError, ValueError):
        return None, "prices missing"
    tol = catalog.tolerance
    basis = str(raw.get("basis") or "none").lower()
    # the band: the model's own basis when its band holds the entry, else
    # the first band that does — the code names the rung, not the model
    bands = [b for b in catalog.entries if b[1] - tol <= entry <= b[2] + tol]
    if not bands:
        return None, f"entry {entry} outside every allowed band"
    band = next((b for b in bands if b[0] == basis), bands[0])
    basis = band[0]
    entry = min(max(entry, band[1]), band[2])
    snapped_stop = _snap(stop, catalog.stops, tol)
    if snapped_stop is None:
        return None, f"stop {stop} not an allowed stop"
    snapped_target = _snap(target, catalog.targets, tol)
    if snapped_target is None:
        return None, f"target {target} not an allowed target"
    stop, target = snapped_stop, snapped_target
    risk = entry - stop if is_long else stop - entry
    reward = target - entry if is_long else entry - target
    if risk <= 0:
        return None, "stop on the wrong side of the entry"
    if reward <= 0:
        return None, "target on the wrong side of the entry"
    if order == "market" and not _near(entry, catalog.price, tol):
        order = "limit"
    if order == "limit" and catalog.price and (
        (is_long and entry > catalog.price + tol) or (not is_long and entry < catalog.price - tol)
    ):
        # a buy limit above price (or a sell limit below it) fills at
        # market the moment it is placed — that is not the order it claims
        return None, "limit on the wrong side of price"
    rr = reward / risk
    d = catalog.decimals
    return AIProposal(
        order=order, direction=direction, basis=basis,
        entry=round(entry, d), stop=round(stop, d), target=round(target, d),
        invalidation=invalidation, rr=round(rr, 2), below_floor=rr < min_rr,
    ), ""


def parse_ai_read(
    payload: Any, model: str = DEFAULT_MODEL, as_of: str = "",
    catalog: Optional[LevelCatalog] = None, min_rr: float = DEFAULT_MIN_RR,
) -> Optional[AIRead]:
    """A validated AIRead from the model's JSON, or None when the shape is
    wrong — a half-parsed opinion is worse than none. The proposal (D28)
    is validated against `catalog` when one is given and dropped on its
    own when it does not fit; without a catalog it is ignored, because an
    unchecked price must never reach the card."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    stance = str(payload.get("stance", "")).lower()
    entry = str(payload.get("preferred_entry", "")).lower()
    read = str(payload.get("read", "")).strip()
    if stance not in STANCES or entry not in ENTRIES or not read:
        return None
    try:
        confidence = max(1, min(5, int(payload.get("confidence", 0))))
    except (TypeError, ValueError):
        return None
    risks_raw = payload.get("risks") or []
    if not isinstance(risks_raw, list):
        risks_raw = []
    risks = [str(r).strip() for r in risks_raw if str(r).strip()][:3]
    proposal, note = None, ""
    if catalog is not None and "proposal" in payload:
        proposal, note = validate_proposal(payload.get("proposal"), catalog, min_rr)
    return AIRead(
        stance=stance, preferred_entry=entry, confidence=confidence,
        read=read[:600], risks=[r[:120] for r in risks], model=model, as_of=as_of,
        proposal=proposal, proposal_note=note,
    )


# ----------------------------------------------------------------- reader


class AIReader:
    """One call: fact sheet + charts in, an `AIRead` out (or None)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        timeout: float = 90.0,
        client: Any = None,
        min_rr: float = DEFAULT_MIN_RR,
    ):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.timeout = timeout
        self.min_rr = min_rr
        self._client = client
        # Why the LAST read returned None, in a few words ("api: 401 …",
        # "refusal", "max_tokens", "unparsable") — the /plan audit prints it
        # so the owner sees the reason in Telegram without opening the
        # Railway logs (2026-09-10: the first live /plan came back without
        # a 🧠 block and nothing said why). None after a successful read.
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self._client is not None or bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy: optional at runtime, absent in tests

            self._client = anthropic.AsyncAnthropic(
                api_key=self.api_key, timeout=self.timeout, max_retries=1,
            )
        return self._client

    async def read(
        self, facts: str, images: Sequence[bytes] = (), as_of: str = "",
        catalog: Optional[LevelCatalog] = None,
    ) -> Optional[AIRead]:
        """Ask for the read. Never raises: every failure logs and returns
        None, because a missing second opinion must never cost the owner
        the alert it was going to decorate. `catalog` (D28) is what the
        proposal is validated against; without one no proposal is kept."""
        if not self.enabled:
            self.last_error = "no ANTHROPIC_API_KEY"
            return None
        self.last_error = None
        content: List[dict] = []
        for png in images:
            if not png:
                continue
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode("ascii"),
                },
            })
        content.append({
            "type": "text",
            "text": "Fact sheet from the rule engine:\n" + facts
            + "\n\nGive your read as JSON. "
            + LANGUAGE_INSTRUCTION.get(get_language(), LANGUAGE_INSTRUCTION["en"]),
        })
        try:
            # max_tokens covers the THINKING too: with adaptive thinking on
            # Sonnet 5 a 2048 cap was spent on reasoning about two charts
            # before a single JSON byte came out (stop_reason max_tokens,
            # empty text, "unparsable"). 16000 is the SDK guidance for a
            # non-streaming call; the JSON itself is a few hundred tokens.
            response = await self._get_client().messages.create(
                model=self.model,
                max_tokens=16000,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": READ_SCHEMA},
                },
                messages=[{"role": "user", "content": content}],
            )
        except Exception as e:  # network, auth, 4xx/5xx, timeout — all best-effort
            self.last_error = f"api: {e}"[:160]
            logger.warning("AI read failed", model=self.model, error=str(e))
            return None
        stop_reason = getattr(response, "stop_reason", None)
        request_id = getattr(response, "_request_id", None)
        if stop_reason == "refusal":
            self.last_error = "refusal"
            logger.warning("AI read refused", model=self.model, request_id=request_id)
            return None
        text = "".join(
            getattr(block, "text", "")
            for block in getattr(response, "content", [])
            if getattr(block, "type", "") == "text"
        )
        read = parse_ai_read(
            text, model=self.model, as_of=as_of, catalog=catalog, min_rr=self.min_rr,
        )
        if read is None:
            self.last_error = (
                "max_tokens" if stop_reason == "max_tokens" else "unparsable"
            )
            logger.warning(
                "AI read unparsable", model=self.model, stop_reason=stop_reason,
                request_id=request_id, text=text[:200],
            )
            return None
        usage = getattr(response, "usage", None)
        logger.info(
            "AI read ok", model=self.model, request_id=request_id,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            proposal=read.proposal.to_dict() if read.proposal else None,
            proposal_note=read.proposal_note or None,
        )
        return read
