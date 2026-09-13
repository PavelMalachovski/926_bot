"""Persistence for the Opus bot on the same SQLite kv store the watcher
uses (`db.Database`, its own file: OPUS_DB_FILE). One plan per pair, the
trades those plans produced, a bounded history of decisions and the
per-day event-call counters. Every write goes through `save()` so a
crash between two mutations cannot leave half a state on disk."""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.services.opus.plan import OPEN, OpusPlan, Trade
from app.services.smc.db import Database
from app.services.smc.sessions import to_prague

HISTORY_LIMIT = 400
TRADES_LIMIT = 400


def prague_day(now: Optional[datetime] = None) -> str:
    return to_prague(now or datetime.now(tz=timezone.utc)).date().isoformat()


class PlanStore:
    def __init__(self, db: Database):
        self.db = db
        self.plans: Dict[str, OpusPlan] = {
            k: OpusPlan.from_dict(v)
            for k, v in (db.kv_get("opus_plans") or {}).items()
            if isinstance(v, dict)
        }
        self.trades: List[Trade] = [
            Trade.from_dict(v) for v in (db.kv_get("opus_trades") or []) if isinstance(v, dict)
        ]
        self.history: List[dict] = list(db.kv_get("opus_history") or [])
        self.event_calls: Dict[str, int] = dict(db.kv_get("opus_event_calls") or {})
        self.last_call: Dict[str, str] = dict(db.kv_get("opus_last_call") or {})
        self.last_block: Optional[str] = db.kv_get("opus_last_block")

    def save(self) -> None:
        self.db.kv_set("opus_plans", {k: p.to_dict() for k, p in self.plans.items()})
        self.db.kv_set("opus_trades", [t.to_dict() for t in self.trades[-TRADES_LIMIT:]])
        self.db.kv_set("opus_history", self.history[-HISTORY_LIMIT:])
        self.db.kv_set("opus_event_calls", self.event_calls)
        self.db.kv_set("opus_last_call", self.last_call)
        self.db.kv_set("opus_last_block", self.last_block)

    # -------------------------------------------------------------- plans

    def put_plan(self, plan: OpusPlan) -> Optional[OpusPlan]:
        """Store the pair's new plan; the previous one (if still live) is
        marked replaced and returned so the caller can say so."""
        previous = self.plans.get(plan.pair)
        if previous is not None and previous.live:
            previous.status = "replaced"
            previous.resolved_at = plan.created_at
        self.plans[plan.pair] = plan
        self.history.append({
            "pair": plan.pair,
            "day": prague_day(datetime.fromisoformat(plan.created_at)),
            "created_at": plan.created_at,
            "action": plan.action,
            "direction": plan.direction,
            "trigger": plan.trigger,
            "confidence": plan.confidence,
            "downgraded_from": plan.downgraded_from,
        })
        self.history = self.history[-HISTORY_LIMIT:]
        return previous

    def orders_today(self, pair: str, now: Optional[datetime] = None) -> int:
        day = prague_day(now)
        return sum(
            1 for h in self.history
            if h.get("pair") == pair and h.get("day") == day
            and h.get("action") in ("limit", "market")
        )

    # ------------------------------------------------------------- trades

    def open_trades(self, pair: Optional[str] = None) -> List[Trade]:
        return [
            t for t in self.trades
            if t.status == OPEN and (pair is None or t.pair == pair)
        ]

    def add_trade(self, trade: Trade) -> None:
        if any(t.id == trade.id for t in self.trades):
            return
        self.trades.append(trade)
        self.trades = self.trades[-TRADES_LIMIT:]

    # ------------------------------------------------------- call budget

    def event_calls_today(self, pair: str, now: Optional[datetime] = None) -> int:
        return int(self.event_calls.get(f"{prague_day(now)}:{pair}", 0))

    def bump_event_calls(self, pair: str, now: Optional[datetime] = None) -> int:
        key = f"{prague_day(now)}:{pair}"
        day = prague_day(now)
        # keep only today's counters
        self.event_calls = {
            k: v for k, v in self.event_calls.items() if k.startswith(day)
        }
        self.event_calls[key] = self.event_calls.get(key, 0) + 1
        return self.event_calls[key]

    def note_call(self, pair: str, now: datetime) -> None:
        self.last_call[pair] = now.astimezone(timezone.utc).isoformat()

    def minutes_since_call(self, pair: str, now: datetime) -> Optional[float]:
        raw = self.last_call.get(pair)
        if not raw:
            return None
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() / 60.0
