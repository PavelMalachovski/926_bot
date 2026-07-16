"""Signal journal: records every APPROVED setup and tracks its outcome.

Lifecycle of a signal:

    pending  — limit order waiting for price to reach the entry
    open     — entry touched, position "live"
    tp / sl  — take-profit or stop-loss hit first (same candle -> sl,
               conservative)
    expired  — entry never touched before the session ended (Rule 10:
               a pending order does not survive its session)
    timeout  — open too long without resolution (safety valve)

Signals live in the SQLite database (see db.py); outcomes are evaluated from
closed M5 candles, incrementally per cycle.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import structlog

from app.services.smc.db import Database
from app.services.smc.models import AnalysisResult, Candle, Direction
from app.services.smc.sessions import session_end_utc, to_prague

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
    """SQLite-backed list of signals with summary statistics."""

    def __init__(self, db: Database):
        self.db = db
        self.signals: List[Dict] = db.signals_all()

    def save(self) -> None:
        for signal in self.signals:
            self.db.signal_upsert(signal)

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
            "taken": None,
            "message_id": None,
            "alert_text": None,
        }
        self.signals.append(signal)
        self.save()
        logger.info("Signal recorded", id=signal["id"], pair=signal["pair"])
        return signal

    def get(self, signal_id: str) -> Optional[Dict]:
        for signal in self.signals:
            if signal["id"] == signal_id:
                return signal
        return None

    def attach_message(
        self, signal_id: str, message_id: int, alert_text: str
    ) -> None:
        """Link the Telegram alert message to the signal (live setup card)."""
        signal = self.get(signal_id)
        if signal:
            signal["message_id"] = message_id
            signal["alert_text"] = alert_text
            self.save()

    def mark_taken(self, signal_id: str, taken: bool) -> Optional[Dict]:
        """Owner pressed ✅ Took it / ❌ Skipped on the alert."""
        signal = self.get(signal_id)
        if signal:
            signal["taken"] = 1 if taken else 0
            self.save()
            logger.info("Signal marked", id=signal_id, taken=taken)
        return signal

    # ---------------------------------------------------------- discipline

    def discipline_block(
        self, pair: str, direction: str, session: Optional[str], now: datetime
    ) -> Optional[str]:
        """Kill-switch proxies based on trades the owner marked as taken.

        Rule 10: no re-entry on the same pair+direction in the same session
        after a taken stop-loss. Rule 0.2: two taken stops in one day close
        the trading day.
        """
        today = to_prague(now).date()
        taken_sl_today = [
            s
            for s in self.signals
            if s.get("taken") == 1
            and s["status"] == "sl"
            and s.get("resolved_at")
            and to_prague(_parse(s["resolved_at"])).date() == today
        ]
        if len(taken_sl_today) >= 2:
            return "Rule 0.2: two taken stop-losses today — trading day is closed"
        for s in taken_sl_today:
            if (
                s["pair"] == pair
                and s["direction"] == direction
                and s.get("session") == session
            ):
                return (
                    f"Rule 10: {pair} {direction} already stopped out this "
                    "session — no re-entry"
                )
        return None

    def taken_sl_count_today(self, now: datetime) -> int:
        today = to_prague(now).date()
        return sum(
            1
            for s in self.signals
            if s.get("taken") == 1
            and s["status"] == "sl"
            and s.get("resolved_at")
            and to_prague(_parse(s["resolved_at"])).date() == today
        )

    def unresolved_pairs(self) -> List[str]:
        return sorted(
            {
                s["pair"]
                for s in self.signals
                if s["status"] in ("pending", "open")
            }
        )

    def update_pair(self, pair: str, candles: List[Candle]) -> List[Tuple[Dict, str]]:
        """Evaluate all unresolved signals of a pair.

        Returns state-change events as (signal, event) tuples, where event is
        "filled", "tp", "sl", "expired" or "timeout" — used to live-update the
        alert card in Telegram.
        """
        now = datetime.now(tz=timezone.utc)
        events: List[Tuple[Dict, str]] = []
        for signal in self.signals:
            if signal["pair"] != pair or signal["status"] not in ("pending", "open"):
                continue
            before = signal["status"]
            evaluate_signal(signal, candles, now)
            after = signal["status"]
            if before == "pending" and after in ("open", "tp", "sl"):
                events.append((signal, "filled"))
            if after != before and after in ("tp", "sl", "expired", "timeout"):
                events.append((signal, after))
                logger.info(
                    "Signal resolved", id=signal["id"], pair=pair, outcome=after
                )
        if events:
            self.save()
        return events

    def stats_text(self, days: int = 30) -> str:
        """Human summary for /stats."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        recent = [s for s in self.signals if _parse(s["created_at"]) >= cutoff]
        if not recent:
            return f"📒 Journal is empty for the last {days} days — no setups yet."

        def count(status, pool=recent):
            return sum(1 for s in pool if s["status"] == status)

        tp, sl = count("tp"), count("sl")
        taken = [s for s in recent if s.get("taken") == 1]
        t_tp, t_sl = count("tp", taken), count("sl", taken)

        lines = [
            f"📒 <b>Signal journal — last {days} days</b>",
            f"Signals: {len(recent)} | marked taken: {len(taken)}",
            f"🎯 TP {tp} | 🛑 SL {sl} | ⏳ active "
            f"{count('pending') + count('open')} | 🗑 expired {count('expired')}",
            f"Winrate (signals): {_winrate_bar(tp, sl)}",
        ]
        if t_tp + t_sl:
            lines.append(f"Winrate (taken):   {_winrate_bar(t_tp, t_sl)}")
        spark = _sparkline(recent)
        if spark:
            lines.append(f"Last outcomes: {spark}")

        by_pair: Dict[str, List[Dict]] = {}
        for s in recent:
            by_pair.setdefault(s["pair"], []).append(s)
        lines.append("")
        lines.append("<b>By pair:</b>")
        for pair in sorted(by_pair):
            group = by_pair[pair]
            lines.append(
                f"• {pair}: {len(group)} setups, "
                f"TP {count('tp', group)} / SL {count('sl', group)}"
            )
        avg_rr = sum(s["rr"] for s in recent) / len(recent)
        lines.append("")
        lines.append(f"Average planned RR: 1:{avg_rr:.1f}")
        return "\n".join(lines)


def _winrate_bar(tp: int, sl: int) -> str:
    """'57% ▰▰▰▰▰▱▱▱' or an em dash when nothing closed yet."""
    closed = tp + sl
    if not closed:
        return "—"
    pct = tp / closed * 100
    filled = round(pct / 12.5)
    return f"{pct:.0f}% {'▰' * filled}{'▱' * (8 - filled)}"


def _sparkline(signals: List[Dict], limit: int = 10) -> str:
    """Emoji strip of the most recent closed outcomes: 🟩 tp, 🟥 sl, ⬜ expired."""
    icons = {"tp": "🟩", "sl": "🟥", "expired": "⬜", "timeout": "⬜"}
    closed = [s for s in signals if s["status"] in icons]
    closed.sort(key=lambda s: s.get("resolved_at") or s["created_at"])
    return "".join(icons[s["status"]] for s in closed[-limit:])
