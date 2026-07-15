"""Signal journal: records every APPROVED setup and tracks its outcome.

Lifecycle of a signal:

    pending  — limit order waiting for price to reach the entry
    open     — entry touched, position "live"
    tp / sl  — take-profit or stop-loss hit first (same candle -> sl,
               conservative)
    expired  — entry never touched before the session ended (Rule 10:
               a pending order does not survive its session)
    timeout  — open too long without resolution (safety valve)

Everything is stored in one JSON file; outcomes are evaluated from closed M5
candles, incrementally per cycle.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import structlog

from app.services.smc.models import AnalysisResult, Candle, Direction
from app.services.smc.sessions import session_end_utc

logger = structlog.get_logger(__name__)

OPEN_TIMEOUT = timedelta(days=5)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def evaluate_signal(signal: Dict, candles: List[Candle], now: datetime) -> Dict:
    """Advance one signal's state using closed candles. Returns the signal.

    Only candles that finished after the last evaluation are considered.
    """
    if signal["status"] not in ("pending", "open"):
        return signal

    is_long = signal["direction"] == Direction.LONG.value
    entry, sl, tp = signal["entry"], signal["stop_loss"], signal["take_profit"]
    watermark = _parse(signal.get("checked_until") or signal["created_at"])

    for candle in candles:
        candle_end = candle.timestamp + timedelta(minutes=5)
        if candle_end <= watermark:
            continue

        if signal["status"] == "pending":
            touched = candle.low <= entry if is_long else candle.high >= entry
            if touched:
                signal["status"] = "open"
                signal["filled_at"] = candle.timestamp.isoformat()
            else:
                continue

        if signal["status"] == "open":
            hit_sl = candle.low <= sl if is_long else candle.high >= sl
            hit_tp = candle.high >= tp if is_long else candle.low <= tp
            if hit_sl:  # both in one candle -> conservative: count the stop
                signal["status"] = "sl"
            elif hit_tp:
                signal["status"] = "tp"
            if signal["status"] in ("tp", "sl"):
                signal["resolved_at"] = candle.timestamp.isoformat()
                break

    if candles:
        signal["checked_until"] = (
            candles[-1].timestamp + timedelta(minutes=5)
        ).isoformat()

    # Expiry rules
    if signal["status"] == "pending":
        expires = signal.get("expires_at")
        if expires and now > _parse(expires):
            signal["status"] = "expired"
            signal["resolved_at"] = now.isoformat()
    elif signal["status"] == "open":
        if now - _parse(signal["created_at"]) > OPEN_TIMEOUT:
            signal["status"] = "timeout"
            signal["resolved_at"] = now.isoformat()
    return signal


class SignalJournal:
    """JSON-backed list of signals with summary statistics."""

    def __init__(self, path: str):
        self.path = path
        self.signals: List[Dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.signals = json.load(f)
        except (OSError, ValueError):
            self.signals = []

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.signals, f, ensure_ascii=False, indent=1)
        except OSError as e:
            logger.warning("Failed to persist journal", error=str(e))

    def record(self, result: AnalysisResult) -> Dict:
        """Store a freshly approved setup."""
        setup = result.setup
        expires = session_end_utc(result.checked_at)
        signal = {
            "id": uuid.uuid4().hex[:10],
            "pair": result.symbol,
            "direction": setup.direction.value,
            "entry": setup.entry,
            "stop_loss": setup.stop_loss,
            "take_profit": setup.take_profit,
            "rr": setup.rr,
            "session": result.session_name,
            "created_at": result.checked_at.isoformat(),
            "expires_at": expires.isoformat() if expires else None,
            # market entries are considered filled immediately
            "status": "open" if setup.entry_is_market else "pending",
            "filled_at": (
                result.checked_at.isoformat() if setup.entry_is_market else None
            ),
            "resolved_at": None,
            "checked_until": None,
        }
        self.signals.append(signal)
        self.save()
        logger.info("Signal recorded", id=signal["id"], pair=signal["pair"])
        return signal

    def unresolved_pairs(self) -> List[str]:
        return sorted(
            {
                s["pair"]
                for s in self.signals
                if s["status"] in ("pending", "open")
            }
        )

    def update_pair(self, pair: str, candles: List[Candle]) -> List[Dict]:
        """Evaluate all unresolved signals of a pair; returns newly resolved."""
        now = datetime.now(tz=timezone.utc)
        resolved = []
        for signal in self.signals:
            if signal["pair"] != pair or signal["status"] not in ("pending", "open"):
                continue
            before = signal["status"]
            evaluate_signal(signal, candles, now)
            if signal["status"] != before and signal["status"] not in (
                "pending",
                "open",
            ):
                resolved.append(signal)
                logger.info(
                    "Signal resolved",
                    id=signal["id"],
                    pair=pair,
                    outcome=signal["status"],
                )
        if resolved:
            self.save()
        return resolved

    def stats_text(self, days: int = 30) -> str:
        """Human summary for /stats."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        recent = [s for s in self.signals if _parse(s["created_at"]) >= cutoff]
        if not recent:
            return (
                f"📒 Журнал пуст за последние {days} дн. — "
                "ни одного сетапа ещё не было."
            )

        def count(status):
            return sum(1 for s in recent if s["status"] == status)

        tp, sl = count("tp"), count("sl")
        closed = tp + sl
        winrate = f"{tp / closed * 100:.0f}%" if closed else "—"
        lines = [
            f"📒 <b>Журнал сигналов за {days} дн.</b>",
            f"Всего сетапов: {len(recent)}",
            f"🎯 TP: {tp} | 🛑 SL: {sl} | винрейт: {winrate}",
            f"⏳ Активных: {count('pending') + count('open')} "
            f"(ждут входа: {count('pending')}, в позиции: {count('open')})",
            f"🗑 Не исполнились (сессия истекла): {count('expired')}",
        ]
        by_pair: Dict[str, List[Dict]] = {}
        for s in recent:
            by_pair.setdefault(s["pair"], []).append(s)
        lines.append("")
        lines.append("<b>По парам:</b>")
        for pair in sorted(by_pair):
            group = by_pair[pair]
            g_tp = sum(1 for s in group if s["status"] == "tp")
            g_sl = sum(1 for s in group if s["status"] == "sl")
            lines.append(f"• {pair}: {len(group)} сетапов, TP {g_tp} / SL {g_sl}")
        avg_rr = sum(s["rr"] for s in recent) / len(recent)
        lines.append("")
        lines.append(f"Средний плановый RR: 1:{avg_rr:.1f}")
        return "\n".join(lines)
