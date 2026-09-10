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
"""

import base64
import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

import structlog

from app.services.smc.i18n import get_language
from app.services.smc.instruments import Instrument
from app.services.smc.models import AnalysisResult, Direction, Verdict
from app.services.smc.sessions import to_prague

logger = structlog.get_logger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"

STANCES = ("agree", "caution", "against")
ENTRIES = ("market", "main", "deep", "wait")

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
    },
    "required": ["stance", "preferred_entry", "confidence", "read", "risks"],
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
Confidence is 1 (weak) to 5 (strong)."""

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
class AIRead:
    stance: str
    preferred_entry: str
    confidence: int
    read: str
    risks: List[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    as_of: str = ""  # Prague HH:MM the read was made


# ------------------------------------------------------------- fact sheet


def _fmt(value: Optional[float], d: int) -> str:
    return "n/a" if value is None else f"{value:.{d}f}"


def describe_for_ai(
    result: AnalysisResult, instrument: Instrument, audit: Any = None,
) -> str:
    """The engine's picture as plain text — the ONLY source of numbers the
    model may quote. Same objects the alert and the audit print, so the
    read can never disagree with the message it is attached to."""
    d = instrument.price_decimals
    lines = [
        f"Pair: {result.symbol}",
        f"Time: {to_prague(result.checked_at).strftime('%Y-%m-%d %H:%M')} Prague"
        + (f", session {result.session_name}" if result.session_name else ", off session"),
        f"Price: {_fmt(result.price, d)}",
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
    return "\n".join(lines)


# ---------------------------------------------------------------- parsing


def parse_ai_read(
    payload: Any, model: str = DEFAULT_MODEL, as_of: str = ""
) -> Optional[AIRead]:
    """A validated AIRead from the model's JSON, or None when the shape is
    wrong — a half-parsed opinion is worse than none."""
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
    return AIRead(
        stance=stance, preferred_entry=entry, confidence=confidence,
        read=read[:600], risks=[r[:120] for r in risks], model=model, as_of=as_of,
    )


# ----------------------------------------------------------------- reader


class AIReader:
    """One call: fact sheet + charts in, an `AIRead` out (or None)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        timeout: float = 90.0,
        client: Any = None,
    ):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.timeout = timeout
        self._client = client

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
    ) -> Optional[AIRead]:
        """Ask for the read. Never raises: every failure logs and returns
        None, because a missing second opinion must never cost the owner
        the alert it was going to decorate."""
        if not self.enabled:
            return None
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
            response = await self._get_client().messages.create(
                model=self.model,
                max_tokens=2048,
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
            logger.warning("AI read failed", model=self.model, error=str(e))
            return None
        if getattr(response, "stop_reason", None) == "refusal":
            logger.warning("AI read refused", model=self.model)
            return None
        text = "".join(
            getattr(block, "text", "")
            for block in getattr(response, "content", [])
            if getattr(block, "type", "") == "text"
        )
        read = parse_ai_read(text, model=self.model, as_of=as_of)
        if read is None:
            logger.warning("AI read unparsable", model=self.model, text=text[:200])
        return read
