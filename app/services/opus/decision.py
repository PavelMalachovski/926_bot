"""The model's answer and the hard limits the code holds it to.

Claude Opus decides — the direction, the order type, every level — and the
code checks only what the owner asked it to check (2026-09-13): the session
window, the red-news blackout and the minimum RR to TP1, plus the geometry
that makes an order an order at all (a stop on the right side, a buy limit
below the market). A decision that breaks a limit is sent back to the
model once with the violations spelled out; if the second answer still
breaks one, the action is downgraded to WAIT and the card says why. The
model's numbers are never silently "fixed".
"""

import json
from dataclasses import dataclass, field
from typing import Any, List, Optional

ACTIONS = ("limit", "market", "wait", "no_trade")
DIRECTIONS = ("long", "short", "none")
BIASES = ("long", "short", "neutral")
VALIDITY = ("session", "day")


def _nullable(kind: str) -> dict:
    return {"anyOf": [{"type": kind}, {"type": "null"}]}


# The answer's shape, bounded so the parser and the card can rely on it.
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "bias": {"type": "string", "enum": list(BIASES)},
        "entry": _nullable("number"),
        "stop_loss": _nullable("number"),
        "tp1": _nullable("number"),
        "tp2": _nullable("number"),
        "invalidation": _nullable("number"),
        "watch_low": _nullable("number"),
        "watch_high": _nullable("number"),
        "valid_for": {"type": "string", "enum": list(VALIDITY)},
        "confidence": {"type": "integer"},
        "read": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "action", "direction", "bias", "entry", "stop_loss", "tp1", "tp2",
        "invalidation", "watch_low", "watch_high", "valid_for", "confidence",
        "read", "reasons", "risks",
    ],
    "additionalProperties": False,
}


@dataclass
class Decision:
    action: str
    direction: str  # "long" | "short" | "none"
    bias: str
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    invalidation: Optional[float] = None
    watch_low: Optional[float] = None
    watch_high: Optional[float] = None
    valid_for: str = "session"
    confidence: int = 3
    read: str = ""
    reasons: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    # Filled by the code, never by the model.
    notes: List[str] = field(default_factory=list)  # hard-limit violations
    downgraded_from: Optional[str] = None  # the action the model wanted
    model: str = ""

    @property
    def is_order(self) -> bool:
        return self.action in ("limit", "market")

    @property
    def is_long(self) -> bool:
        return self.direction == "long"

    @property
    def risk(self) -> Optional[float]:
        if self.entry is None or self.stop_loss is None:
            return None
        return abs(self.entry - self.stop_loss)

    def rr(self, target: Optional[float]) -> Optional[float]:
        risk = self.risk
        if target is None or not risk:
            return None
        reward = (target - self.entry) if self.is_long else (self.entry - target)
        return reward / risk

    @property
    def has_watch_zone(self) -> bool:
        return self.watch_low is not None and self.watch_high is not None


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strings(value: Any, limit: int, width: int) -> List[str]:
    if not isinstance(value, list):
        return []
    out = [str(v).strip()[:width] for v in value if str(v).strip()]
    return out[:limit]


def parse_decision(payload: Any, model: str = "") -> Optional[Decision]:
    """A validated Decision from the model's JSON, or None when the shape is
    wrong — a half-parsed order is worse than none."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action", "")).lower()
    direction = str(payload.get("direction", "none") or "none").lower()
    bias = str(payload.get("bias", "neutral") or "neutral").lower()
    read = str(payload.get("read", "")).strip()
    if action not in ACTIONS or not read:
        return None
    if direction not in DIRECTIONS:
        direction = "none"
    if bias not in BIASES:
        bias = "neutral"
    valid_for = str(payload.get("valid_for", "session") or "session").lower()
    if valid_for not in VALIDITY:
        valid_for = "session"
    try:
        confidence = max(1, min(5, int(payload.get("confidence", 3))))
    except (TypeError, ValueError):
        confidence = 3
    return Decision(
        action=action,
        direction=direction,
        bias=bias,
        entry=_num(payload.get("entry")),
        stop_loss=_num(payload.get("stop_loss")),
        tp1=_num(payload.get("tp1")),
        tp2=_num(payload.get("tp2")),
        invalidation=_num(payload.get("invalidation")),
        watch_low=_num(payload.get("watch_low")),
        watch_high=_num(payload.get("watch_high")),
        valid_for=valid_for,
        confidence=confidence,
        read=read[:900],
        reasons=_strings(payload.get("reasons"), 4, 160),
        risks=_strings(payload.get("risks"), 4, 160),
        model=model,
    )


# ------------------------------------------------------------- hard limits


@dataclass
class Limits:
    """What the code checks after the model has answered."""

    price: float  # last closed M5 close — the market reference
    min_rr: float = 2.0
    session_open: bool = True
    blackout_until: Optional[str] = None  # Prague HH:MM while a blackout holds
    decimals: int = 2


def violations(decision: Decision, limits: Limits) -> List[str]:
    """Every hard limit the decision breaks, as English sentences the model
    can act on (they go back to it verbatim) and the card can print."""
    out: List[str] = []
    d = limits.decimals
    if decision.action == "market":
        # a market entry IS the current price, whatever the model typed
        decision.entry = limits.price
    if decision.is_order:
        if decision.direction not in ("long", "short"):
            out.append("an order needs a direction (long or short)")
        if decision.entry is None or decision.stop_loss is None or decision.tp1 is None:
            out.append("an order needs entry, stop_loss and tp1 as numbers")
        if not limits.session_open:
            out.append(
                "outside trading hours (08:00-18:30 Prague, Mon-Fri for forex): "
                "no market entry — plan a limit for the session or wait"
                if decision.action == "market"
                else "outside trading hours: an order can only be planned, "
                "so mark it valid_for the session and keep the stop wide "
                "enough for the open"
            )
        if limits.blackout_until:
            out.append(
                f"red-news blackout until {limits.blackout_until} Prague: no "
                "entries — answer wait (with a watch zone) or no_trade"
            )
    if out:
        return out
    if not decision.is_order:
        if (decision.watch_low is None) != (decision.watch_high is None):
            out.append("a watch zone needs both watch_low and watch_high")
        elif decision.has_watch_zone and decision.watch_low >= decision.watch_high:
            out.append("watch_low must be below watch_high")
        elif decision.has_watch_zone and decision.invalidation is not None:
            side = decision.direction if decision.direction != "none" else decision.bias
            if side == "long" and decision.invalidation >= decision.watch_low:
                out.append(
                    "for a long idea the invalidation sits BELOW the watch zone "
                    "(a close there breaks the zone) — or null"
                )
            if side == "short" and decision.invalidation <= decision.watch_high:
                out.append(
                    "for a short idea the invalidation sits ABOVE the watch zone "
                    "(a close there breaks the zone) — or null"
                )
        return out
    long = decision.is_long
    entry, sl, tp1, tp2 = decision.entry, decision.stop_loss, decision.tp1, decision.tp2
    if long and not (sl < entry < tp1):
        out.append(
            f"long geometry broken: need stop_loss < entry < tp1, got "
            f"{sl:.{d}f} / {entry:.{d}f} / {tp1:.{d}f}"
        )
    if not long and not (sl > entry > tp1):
        out.append(
            f"short geometry broken: need stop_loss > entry > tp1, got "
            f"{sl:.{d}f} / {entry:.{d}f} / {tp1:.{d}f}"
        )
    if tp2 is not None and ((long and tp2 <= tp1) or (not long and tp2 >= tp1)):
        out.append("tp2 must lie beyond tp1 in the trade direction (or be null)")
    if decision.action == "limit":
        if long and entry > limits.price:
            out.append(
                f"a buy limit must sit BELOW the market ({limits.price:.{d}f}); "
                f"{entry:.{d}f} is above it — use market, or a lower entry, or wait"
            )
        if not long and entry < limits.price:
            out.append(
                f"a sell limit must sit ABOVE the market ({limits.price:.{d}f}); "
                f"{entry:.{d}f} is below it — use market, or a higher entry, or wait"
            )
    if decision.action == "market":
        decision.invalidation = None  # a position has a stop, not a cancel level
    elif decision.invalidation is not None and (
        (long and decision.invalidation <= limits.price)
        or (not long and decision.invalidation >= limits.price)
    ):
        out.append(
            "for a limit the invalidation is the price where a body close means "
            "the move left WITHOUT filling you — it sits beyond the current "
            f"market ({limits.price:.{d}f}) on the target side, not on the stop "
            "side (the stop covers that once filled) — or null"
        )
    rr = decision.rr(tp1)
    if rr is not None and rr < limits.min_rr:
        out.append(
            f"RR to tp1 is 1:{rr:.1f}, below the minimum 1:{limits.min_rr:.1f} — "
            "move the entry deeper, tighten the stop behind real structure, "
            "pick a further tp1, or wait"
        )
    return out


def downgrade(decision: Decision, notes: List[str]) -> Decision:
    """Turn an order that still breaks a limit into WAIT, keeping the
    model's numbers on the card so the owner sees what it wanted."""
    decision.notes = list(notes)
    if decision.is_order:
        decision.downgraded_from = decision.action
        decision.action = "wait"
    return decision
