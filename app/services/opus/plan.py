"""The stored plan, the trades it produces, and the cheap price monitor.

A `/plan` press (or an event re-read) produces a `Decision`; the bot keeps
it as an `OpusPlan` per pair and, every five minutes in session, walks the
M5 candles printed since to see whether anything happened to it:

* a LIMIT plan fills (price touches the entry) → a `Trade` is opened and
  tracked to TP1 / SL for the journal — silently, the owner manages the
  position himself (owner decision 2026-09-13: Opus is consulted only
  until the entry);
* a LIMIT or WAIT plan is **invalidated** (an M5 body close beyond the
  level the model named) → the bot asks Opus again, once;
* a WAIT plan's **watch zone is reached** → the bot asks Opus again, once;
* a plan **expires** with its session (or day) → one "pull the limit"
  message.

Everything here is pure over candles and timestamps; the network and the
model live in `opus_bot.py`.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from app.services.opus.decision import Decision
from app.services.smc.models import Candle

# Plan statuses.
PENDING = "pending"      # a limit order waiting for its fill
WATCHING = "watching"    # wait / no_trade with a watch zone
IDLE = "idle"            # no order, no zone — nothing to monitor
FILLED = "filled"        # the limit filled → see the Trade
ENTERED = "entered"      # a market entry → see the Trade
EXPIRED = "expired"
REPLACED = "replaced"    # a newer plan took over
LIVE_STATUSES = (PENDING, WATCHING)

# Event codes the monitor emits.
EV_FILLED = "filled"
EV_INVALIDATED = "invalidated"
EV_ZONE_REACHED = "zone_reached"
EV_EXPIRED = "expired"
EV_SESSION_OPENED = "session_opened"  # emitted by the watcher, not here

# Trade statuses.
OPEN = "open"
TP = "tp"
SL = "sl"
TIMEOUT = "timeout"
TRADE_TIMEOUT_DAYS = 5


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class OpusPlan:
    pair: str
    created_at: str  # ISO UTC
    as_of: str  # Prague "dd.mm HH:MM" the decision was made at
    price: float  # market reference at the decision
    action: str
    direction: str
    bias: str
    entry: Optional[float]
    stop_loss: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    invalidation: Optional[float]
    watch_low: Optional[float]
    watch_high: Optional[float]
    valid_for: str
    valid_until: Optional[str]  # ISO UTC
    confidence: int
    read: str
    reasons: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    downgraded_from: Optional[str] = None
    model: str = ""
    trigger: str = "plan"  # "plan" | an event code
    status: str = IDLE
    events: List[str] = field(default_factory=list)
    message_id: Optional[int] = None
    resolved_at: Optional[str] = None

    # ------------------------------------------------------------ helpers

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

    @property
    def live(self) -> bool:
        return self.status in LIVE_STATUSES

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "OpusPlan":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in fields})


@dataclass
class Trade:
    id: str
    pair: str
    direction: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: Optional[float]
    opened_at: str  # ISO UTC
    plan_created_at: str
    kind: str  # "limit" | "market"
    confidence: int
    status: str = OPEN
    result_r: Optional[float] = None
    closed_at: Optional[str] = None

    @property
    def is_long(self) -> bool:
        return self.direction == "long"

    @property
    def rr1(self) -> float:
        risk = abs(self.entry - self.stop_loss)
        reward = (self.tp1 - self.entry) if self.is_long else (self.entry - self.tp1)
        return reward / risk if risk else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Trade":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in fields})


def plan_from_decision(
    pair: str, decision: Decision, now: datetime, price: float,
    valid_until: Optional[datetime], as_of: str, trigger: str = "plan",
) -> OpusPlan:
    if decision.action == "limit":
        status = PENDING
    elif decision.action == "market":
        status = ENTERED
    elif decision.has_watch_zone:
        status = WATCHING
    else:
        status = IDLE
    return OpusPlan(
        pair=pair,
        created_at=_iso(now),
        as_of=as_of,
        price=price,
        action=decision.action,
        direction=decision.direction,
        bias=decision.bias,
        entry=decision.entry,
        stop_loss=decision.stop_loss,
        tp1=decision.tp1,
        tp2=decision.tp2,
        invalidation=decision.invalidation,
        watch_low=decision.watch_low,
        watch_high=decision.watch_high,
        valid_for=decision.valid_for,
        valid_until=_iso(valid_until),
        confidence=decision.confidence,
        read=decision.read,
        reasons=list(decision.reasons),
        risks=list(decision.risks),
        notes=list(decision.notes),
        downgraded_from=decision.downgraded_from,
        model=decision.model,
        trigger=trigger,
        status=status,
    )


def trade_from_plan(plan: OpusPlan, opened_at: datetime, kind: str) -> Trade:
    return Trade(
        id=f"{plan.pair}:{plan.created_at}",
        pair=plan.pair,
        direction=plan.direction,
        entry=float(plan.entry),
        stop_loss=float(plan.stop_loss),
        tp1=float(plan.tp1),
        tp2=plan.tp2,
        opened_at=_iso(opened_at),
        plan_created_at=plan.created_at,
        kind=kind,
        confidence=plan.confidence,
    )


# ------------------------------------------------------------------ monitor


@dataclass
class Event:
    kind: str
    pair: str
    price: float  # the candle close the event was seen on
    at: datetime  # the candle's open time (UTC)


def _candles_since(candles: Sequence[Candle], since: Optional[datetime]) -> List[Candle]:
    """Candles that could have moved the plan: open time at or after the
    5-minute slot the decision was made in (the candle in progress at the
    press can still touch the entry — the owner places the order within a
    minute of the card)."""
    if since is None:
        return list(candles)
    floor = since.replace(second=0, microsecond=0)
    floor -= timedelta(minutes=floor.minute % 5)
    return [c for c in candles if c.timestamp >= floor]


def _side(plan: OpusPlan) -> Optional[str]:
    """Which way the plan leans: the order's direction, else the bias."""
    if plan.direction in ("long", "short"):
        return plan.direction
    if plan.bias in ("long", "short"):
        return plan.bias
    return None


def cancel_side(plan: OpusPlan) -> Optional[str]:
    """Where a body close cancels the plan: "above" or "below" the level.

    A LIMIT is cancelled when the move leaves without filling it — the
    level sits on the target side of the market, so a long limit dies on
    a close ABOVE it and a short limit on a close BELOW it. A WAIT idea is
    cancelled when its zone breaks — a long idea on a close BELOW the
    level, a short one ABOVE. None when the plan has no side."""
    side = _side(plan)
    if plan.invalidation is None or side is None:
        return None
    if plan.action == "limit":
        return "above" if side == "long" else "below"
    return "below" if side == "long" else "above"


def _closed_beyond(plan: OpusPlan, candle: Candle) -> bool:
    side = cancel_side(plan)
    if side is None:
        return False
    if side == "above":
        return candle.close > plan.invalidation
    return candle.close < plan.invalidation


def _touched(plan: OpusPlan, candle: Candle) -> bool:
    if plan.is_long:
        return candle.low <= plan.entry
    return candle.high >= plan.entry


def _in_watch_zone(plan: OpusPlan, candle: Candle) -> bool:
    return candle.low <= plan.watch_high and candle.high >= plan.watch_low


def advance_plan(
    plan: OpusPlan, m5: Sequence[Candle], now: datetime
) -> List[Event]:
    """Walk the candles printed since the plan; mutate its status; return
    the events the caller should act on. Each event fires once per plan."""
    events: List[Event] = []
    if not plan.live:
        return events
    created = _parse(plan.created_at)
    for candle in _candles_since(m5, created):
        if plan.status == PENDING and _touched(plan, candle):
            plan.status = FILLED
            plan.resolved_at = _iso(candle.timestamp)
            plan.events.append(EV_FILLED)
            events.append(Event(EV_FILLED, plan.pair, candle.close, candle.timestamp))
            break
        if plan.status == WATCHING and plan.has_watch_zone and (
            EV_ZONE_REACHED not in plan.events and _in_watch_zone(plan, candle)
        ):
            plan.events.append(EV_ZONE_REACHED)
            events.append(Event(EV_ZONE_REACHED, plan.pair, candle.close, candle.timestamp))
        if EV_INVALIDATED not in plan.events and _closed_beyond(plan, candle):
            plan.events.append(EV_INVALIDATED)
            events.append(Event(EV_INVALIDATED, plan.pair, candle.close, candle.timestamp))
    valid_until = _parse(plan.valid_until)
    if plan.live and valid_until is not None and now >= valid_until:
        plan.status = EXPIRED
        plan.resolved_at = _iso(now)
        plan.events.append(EV_EXPIRED)
        events.append(Event(EV_EXPIRED, plan.pair, m5[-1].close if m5 else plan.price, now))
    return events


def advance_trade(trade: Trade, m5: Sequence[Candle], now: datetime) -> Optional[str]:
    """Track an open trade to TP1 or SL on the candles since it opened. TP
    and SL in one candle count as SL (conservative, same as the watcher's
    journal). Returns the new status when it changed."""
    if trade.status != OPEN:
        return None
    opened = _parse(trade.opened_at)
    for candle in _candles_since(m5, opened):
        if trade.is_long:
            hit_sl = candle.low <= trade.stop_loss
            hit_tp = candle.high >= trade.tp1
        else:
            hit_sl = candle.high >= trade.stop_loss
            hit_tp = candle.low <= trade.tp1
        if hit_sl:
            trade.status, trade.result_r = SL, -1.0
        elif hit_tp:
            trade.status, trade.result_r = TP, round(trade.rr1, 2)
        if trade.status != OPEN:
            trade.closed_at = _iso(candle.timestamp)
            return trade.status
    if opened is not None and now - opened > timedelta(days=TRADE_TIMEOUT_DAYS):
        trade.status = TIMEOUT
        trade.closed_at = _iso(now)
        return TIMEOUT
    return None
