"""Claude Opus as the analyst: the brief in, a bounded decision out.

Unlike the watcher's AI read (D26, a comment on the engine's setup), here
the model IS the strategy: it reads the candles, names the bias, the
order type and every level. The code around it holds three limits the
owner chose (2026-09-13) — session, news blackout, minimum RR — and the
geometry that makes an order valid, sends a failing answer back once with
the violations, and downgrades a still-failing order to WAIT.

Best-effort everywhere: no key, a network error, a refusal or an
unparsable answer all return None and the bot says so in one line.
`anthropic` is imported lazily so the tests run without it.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional, Sequence

import structlog

from app.services.opus.decision import (
    DECISION_SCHEMA, Decision, Limits, downgrade, parse_decision, violations,
)
from app.services.smc.i18n import get_language

logger = structlog.get_logger(__name__)

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You are a senior discretionary Smart Money Concepts (ICT-style) trader \
managing one account for a trader who wants ONE OR TWO quality trades per \
day per pair, never more. You read the candles yourself — H4 for the \
higher-timeframe narrative, H1 for the zones and the liquidity, M5 for the \
entry — and you name the order. Nobody checks your levels except three \
hard limits the code enforces after you answer.

Your toolbox, in the order you use it:
1. Narrative: where is price being delivered on H4/H1 — which external \
liquidity (old highs/lows, equal highs/lows, session highs/lows, the \
previous day's high/low, the Asian range) is the draw, and is price in \
premium or discount of the dealing range that matters?
2. Zones: order blocks, breakers, fair value gaps / imbalances on H1 (and \
H4) that sit between price and the draw — fresh, untested ones first. \
Liquidity sweeps into them are what you want to see.
3. Confirmation: on M5, a displacement (a change of character / market \
structure shift) away from the zone, ideally leaving an imbalance you can \
enter into. A limit into the M5 imbalance or order block of that \
displacement is the best entry; a market entry only when the move is \
fresh and the stop is still tight.
4. Risk: the stop goes behind the swept extreme or the zone, not at a \
round number; TP1 is the nearest meaningful liquidity pool, TP2 the \
draw. If the nearest pool pays less than 1:2 from a sensible stop, the \
trade is not there — say WAIT and name the zone where it would be.

Hard limits the code checks (an answer that breaks one comes back to you \
once with the violation, then is downgraded to WAIT): trading hours are \
08:00-18:30 Prague (forex Mon-Fri) — no market entry outside them; no \
entries at all inside a red-news blackout (60 min before, 15 min after a \
high-impact release for the pair's currencies); RR to tp1 at least the \
minimum in the brief; a buy limit sits below the market, a sell limit \
above; stop and targets on the correct sides.

Answer rules: use ONLY prices you can derive from the candle rows (quote \
the exact level: a candle high/low, a zone edge, an imbalance edge, the \
midpoint of a gap); never invent numbers. `action` is one of limit / \
market / wait / no_trade. For wait and no_trade give `watch_low` / \
`watch_high` — the zone where you would want a fresh look — when there is \
one, else null. `invalidation` is the price where an M5 body close cancels \
the idea: for a LIMIT it sits on the TARGET side of the current market — a \
close there means the move left without filling you, pull the order (the \
stop covers the other side once filled); for WAIT / NO_TRADE it sits on \
the stop side of the watch zone — a close there breaks the zone. Null when \
you have none. \
`valid_for` is "session" (the order dies at the block end — 14:00 or \
18:30 Prague) or "day" (18:30). `confidence` 1-5. `read` is at most five \
sentences, plain prose, no markdown, no emoji. `reasons` — up to four \
short lines, the structure you are trading. `risks` — up to four short \
lines, what breaks the idea. A brief that carries a PREVIOUS PLAN and an \
EVENT is a follow-up: judge the previous idea on the new candles and \
answer with the CURRENT best action (keep it, move it, cancel it, enter \
now); say in `read` what changed. Write `read`, `reasons` and `risks` in \
the language the brief asks for; enum fields stay English."""

LANGUAGE_INSTRUCTION = {
    "ru": (
        "Write `read`, `reasons` and `risks` in Russian (по-русски, trader's "
        "vocabulary: CHoCH, FVG, OB, LONG/SHORT, SL/TP, PDH/PDL stay Latin)."
    ),
    "en": "Write `read`, `reasons` and `risks` in English.",
}


@dataclass
class Outcome:
    """What one decide() produced, for logging and the card."""

    decision: Optional[Decision]
    attempts: int = 0
    error: Optional[str] = None
    first_violations: List[str] = field(default_factory=list)


class OpusAnalyst:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        timeout: float = 240.0,
        fallbacks: bool = True,
        client: Any = None,
    ):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.timeout = timeout
        self.fallbacks = fallbacks
        self._client = client
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

    # ---------------------------------------------------------------- API

    async def _ask(self, brief: str, images: Sequence[bytes]) -> Optional[str]:
        """One model call; the JSON text or None (with last_error set)."""
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
            "text": brief + "\n\nAnswer with the decision JSON. "
            + LANGUAGE_INSTRUCTION.get(get_language(), LANGUAGE_INSTRUCTION["en"]),
        })
        params = dict(
            model=self.model,
            # thinking shares this budget; the JSON itself is a few hundred
            # tokens, the reasoning over 430 candle rows is not
            max_tokens=24000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
            },
            messages=[{"role": "user", "content": content}],
        )
        client = self._get_client()
        try:
            if self.fallbacks:
                try:
                    response = await client.beta.messages.create(
                        betas=[FALLBACK_BETA], fallbacks="default", **params,
                    )
                except Exception as e:  # an API that does not know the beta
                    if "fallback" not in str(e).lower() and "beta" not in str(e).lower():
                        raise
                    logger.warning("Fallbacks rejected, retrying without", error=str(e))
                    self.fallbacks = False
                    response = await client.messages.create(**params)
            else:
                response = await client.messages.create(**params)
        except Exception as e:  # network, auth, 4xx/5xx, timeout — best-effort
            self.last_error = f"api: {e}"[:200]
            logger.warning("Opus decision failed", model=self.model, error=str(e))
            return None
        stop_reason = getattr(response, "stop_reason", None)
        request_id = getattr(response, "_request_id", None)
        if stop_reason == "refusal":
            self.last_error = "refusal"
            logger.warning("Opus decision refused", request_id=request_id)
            return None
        text = "".join(
            getattr(block, "text", "")
            for block in getattr(response, "content", [])
            if getattr(block, "type", "") == "text"
        )
        usage = getattr(response, "usage", None)
        logger.info(
            "Opus decision answered", model=getattr(response, "model", self.model),
            request_id=request_id, stop_reason=stop_reason,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read=getattr(usage, "cache_read_input_tokens", None),
        )
        if not text:
            self.last_error = "max_tokens" if stop_reason == "max_tokens" else "empty"
            return None
        return text

    async def decide(
        self, brief: str, images: Sequence[bytes], limits: Limits,
    ) -> Outcome:
        """Ask, check the hard limits, ask once more with the violations,
        downgrade what still fails. Never raises."""
        if not self.enabled:
            self.last_error = "no ANTHROPIC_API_KEY"
            return Outcome(None, 0, self.last_error)
        self.last_error = None
        text = await self._ask(brief, images)
        if text is None:
            return Outcome(None, 1, self.last_error)
        decision = parse_decision(text, model=self.model)
        if decision is None:
            self.last_error = "unparsable"
            logger.warning("Opus decision unparsable", text=text[:200])
            return Outcome(None, 1, self.last_error)
        broken = violations(decision, limits)
        if not broken:
            return Outcome(decision, 1)
        first = list(broken)
        logger.info("Opus decision breaks a limit, asking again", violations=broken)
        retry_brief = (
            brief
            + "\n\nYOUR PREVIOUS ANSWER broke these hard limits:\n- "
            + "\n- ".join(broken)
            + "\nAnswer again. If no order satisfies every limit, answer wait "
            "(with a watch zone) or no_trade."
        )
        text = await self._ask(retry_brief, images)
        second = parse_decision(text, model=self.model) if text else None
        if second is None:
            # keep the first answer, downgraded — the owner still sees the idea
            return Outcome(downgrade(decision, first), 2, self.last_error, first)
        broken = violations(second, limits)
        if broken:
            return Outcome(downgrade(second, broken), 2, None, first)
        return Outcome(second, 2, None, first)
