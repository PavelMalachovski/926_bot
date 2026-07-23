"""Manual trade journal parsed from MetaTrader screenshots.

The user sends a screenshot of the MT4/MT5 history; it is parsed with OpenAI
Vision into structured trades, stored in SQLite behind a confirmation step, and
aggregated into statistics for the /journal command.
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import structlog

from app.core.config import settings
from app.services.smc.db import Database

logger = structlog.get_logger(__name__)

_VISION_URL = "https://api.openai.com/v1/chat/completions"

_TRADE_KEYS = (
    "ticket",
    "symbol",
    "direction",
    "volume",
    "open_price",
    "close_price",
    "open_time",
    "close_time",
    "sl",
    "tp",
    "profit",
    "swap",
    "commission",
    "taxes",
    "closed_by_sl",
)

_VISION_PROMPT = (
    "You are a precise data extraction engine for MetaTrader 4/5 trade history "
    "screenshots. Extract EVERY closed trade visible in the image.\n\n"
    "Each trade block typically looks like:\n"
    "  SYMBOL, buy/sell VOLUME        CLOSE_DATE CLOSE_TIME\n"
    "  OPEN_PRICE -> CLOSE_PRICE                 PROFIT\n"
    "  OPEN_DATE OPEN_TIME, [sl]\n"
    "  S/L: ...   Swap: ...\n"
    "  T/P: ...   Taxes: ...\n"
    "  ID: TICKET  Commission: ...\n\n"
    "Return STRICT JSON only, no markdown, in the shape:\n"
    '{"trades": [{'
    '"ticket": string|null, '
    '"symbol": string, '
    '"direction": "buy"|"sell"|null, '
    '"volume": number|null, '
    '"open_price": number|null, '
    '"close_price": number|null, '
    '"open_time": "YYYY-MM-DD HH:MM:SS"|null, '
    '"close_time": "YYYY-MM-DD HH:MM:SS"|null, '
    '"sl": number|null, '
    '"tp": number|null, '
    '"profit": number|null, '
    '"swap": number|null, '
    '"commission": number|null, '
    '"taxes": number|null, '
    '"closed_by_sl": boolean'
    "}]}\n\n"
    "Rules:\n"
    "- The 'A -> B' line means open_price=A, close_price=B.\n"
    "- profit is the colored number on the right of the close date (may be negative).\n"
    "- closed_by_sl is true when the '[sl]' marker is present near the open time.\n"
    "- Remove thousands separators/spaces from numbers (e.g. '1 814.32' -> 1814.32).\n"
    "- ticket is the numeric order id (the 'ID:' field). The compact history view "
    "does NOT show it — if no numeric id is visible for a trade, set ticket to "
    "null. Never output the literal word 'TICKET' or any placeholder.\n"
    "- Use null for anything not visible. Do not invent values."
)


class TradeJournal:
    """Parsing, storage and statistics for manually logged MT trades."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def api_key(self) -> Optional[str]:
        return settings.openai.api_key

    @property
    def model(self) -> str:
        return settings.openai.model or "gpt-4o-mini"

    # ------------------------------------------------------------------ #
    # Screenshot parsing                                                 #
    # ------------------------------------------------------------------ #
    async def parse_screenshot(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        """Extract a list of normalized trade dicts from a screenshot."""
        if not self.api_key:
            raise RuntimeError("OpenAI API key not configured")

        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _VISION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64}",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 2000,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(_VISION_URL, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()

        content = result["choices"][0]["message"]["content"]
        data = json.loads(content)
        raw_trades = data.get("trades", []) if isinstance(data, dict) else []
        return [self._normalize(t) for t in raw_trades if isinstance(t, dict)]

    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce a raw parsed trade into typed values."""
        trade: Dict[str, Any] = {k: raw.get(k) for k in _TRADE_KEYS}

        trade["ticket"] = self._clean_ticket(trade.get("ticket"))
        trade["symbol"] = (self._as_str(trade.get("symbol")) or "UNKNOWN").upper()
        direction = self._as_str(trade.get("direction"))
        trade["direction"] = direction.lower() if direction else None

        for key in ("volume", "open_price", "close_price", "sl", "tp"):
            trade[key] = self._as_float(trade.get(key))
        for key in ("profit", "swap", "commission", "taxes"):
            trade[key] = self._as_float(trade.get(key)) or 0.0

        trade["open_time"] = self._as_dt(trade.get("open_time"))
        trade["close_time"] = self._as_dt(trade.get("close_time"))
        trade["closed_by_sl"] = bool(trade.get("closed_by_sl"))
        return trade

    @staticmethod
    def _as_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _clean_ticket(value: Any) -> Optional[str]:
        """Keep only a genuine numeric MT order id; drop placeholders/None.

        The compact history view has no ID column, and the vision model tends to
        echo the literal word 'TICKET' from the prompt — treat anything that is
        not all-digits as "no ticket".
        """
        if value is None:
            return None
        text = str(value).strip()
        return text if text.isdigit() else None

    @classmethod
    def _dedup_key(cls, trade: Dict[str, Any]) -> str:
        """Stable identity for a trade, used to skip duplicates on re-upload.

        Prefer the numeric ticket; when absent (compact view), fall back to a
        signature of the trade's defining fields so distinct trades are kept but
        the same screenshot sent twice is de-duplicated.
        """
        ticket = cls._clean_ticket(trade.get("ticket"))
        if ticket:
            return f"t:{ticket}"
        parts = [
            str(trade.get("symbol")),
            str(trade.get("direction")),
            str(trade.get("volume")),
            str(trade.get("open_price")),
            str(trade.get("close_price")),
            str(trade.get("close_time")),
        ]
        return "s:" + "|".join(parts)

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return float(value)
        cleaned = str(value).replace(" ", "").replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _as_dt(value: Any) -> Optional[str]:
        """Normalize a date/time to an ISO-ish string, or None."""
        if not value:
            return None
        text = str(value).strip().replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ #
    # Persistence (confirmation workflow)                                #
    # ------------------------------------------------------------------ #
    def save_pending_batch(self, trades: List[Dict[str, Any]]) -> str:
        """Persist parsed trades as a pending batch, return its batch id."""
        batch_id = uuid.uuid4().hex[:16]
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for t in trades:
            self.db.trade_insert(
                {
                    "id": uuid.uuid4().hex,
                    "ticket": t.get("ticket"),
                    "symbol": t.get("symbol") or "UNKNOWN",
                    "direction": t.get("direction"),
                    "volume": t.get("volume"),
                    "open_price": t.get("open_price"),
                    "close_price": t.get("close_price"),
                    "open_time": t.get("open_time"),
                    "close_time": t.get("close_time"),
                    "sl": t.get("sl"),
                    "tp": t.get("tp"),
                    "profit": t.get("profit") or 0.0,
                    "swap": t.get("swap") or 0.0,
                    "commission": t.get("commission") or 0.0,
                    "taxes": t.get("taxes") or 0.0,
                    "closed_by_sl": 1 if t.get("closed_by_sl") else 0,
                    "status": "pending",
                    "batch_id": batch_id,
                    "created_at": now,
                }
            )
        return batch_id

    def confirm_batch(self, batch_id: str) -> Dict[str, int]:
        """Confirm a pending batch, dropping duplicates.

        De-duplication uses the numeric ticket when present, otherwise a
        signature of the trade's fields (compact history views have no ticket).

        Returns {"saved": n, "duplicates": m}.
        """
        pending = self.db.trades_by_batch(batch_id, "pending")
        if not pending:
            return {"saved": 0, "duplicates": 0}

        existing_keys = {
            self._dedup_key(t) for t in self.db.trades_by_status("confirmed")
        }
        saved = 0
        duplicates = 0
        seen_in_batch: set = set()
        for trade in pending:
            key = self._dedup_key(trade)
            if key in existing_keys or key in seen_in_batch:
                self.db.trade_delete(trade["id"])
                duplicates += 1
                continue
            self.db.trade_set_status(trade["id"], "confirmed")
            seen_in_batch.add(key)
            saved += 1
        return {"saved": saved, "duplicates": duplicates}

    def discard_batch(self, batch_id: str) -> int:
        """Delete a pending batch. Returns number of rows removed."""
        return self.db.trades_delete_batch(batch_id, "pending")

    # ------------------------------------------------------------------ #
    # Statistics                                                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _net(t: Dict[str, Any]) -> float:
        return (
            (t.get("profit") or 0.0)
            + (t.get("swap") or 0.0)
            + (t.get("commission") or 0.0)
            - (t.get("taxes") or 0.0)
        )

    def get_stats(self) -> Dict[str, Any]:
        """Compute journal statistics for all confirmed trades."""
        trades = self.db.trades_by_status("confirmed")
        total = len(trades)
        if total == 0:
            return {"total": 0}

        wins = [t for t in trades if self._net(t) > 0]
        losses = [t for t in trades if self._net(t) < 0]
        total_net = sum(self._net(t) for t in trades)
        gross_profit = sum(self._net(t) for t in wins)
        gross_loss = sum(self._net(t) for t in losses)  # negative

        by_symbol: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            s = by_symbol.setdefault(
                t.get("symbol") or "UNKNOWN", {"count": 0, "wins": 0, "net": 0.0}
            )
            s["count"] += 1
            s["net"] += self._net(t)
            if self._net(t) > 0:
                s["wins"] += 1

        return {
            "total": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / total * 100.0) if total else 0.0,
            "total_net": total_net,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": (gross_profit / abs(gross_loss) if gross_loss else None),
            "best": max((self._net(t) for t in trades), default=0.0),
            "worst": min((self._net(t) for t in trades), default=0.0),
            "by_symbol": by_symbol,
            "recent": trades[:5],  # already ordered by close_time DESC
        }

    def stats_text(self) -> str:
        """Convenience for the /journal command."""
        return self.format_journal(self.get_stats())

    # ------------------------------------------------------------------ #
    # Formatting (HTML)                                                  #
    # ------------------------------------------------------------------ #
    def format_preview(self, trades: List[Dict[str, Any]]) -> str:
        if not trades:
            return (
                "🔍 No trades could be recognized in the screenshot.\n"
                "Try sending a clearer screenshot of the MT4 history."
            )

        lines = [f"📸 <b>Recognized trades: {len(trades)}</b>\n"]
        total = 0.0
        for i, t in enumerate(trades, 1):
            profit = t.get("profit") or 0.0
            total += profit
            emoji = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪️")
            direction = (t.get("direction") or "?").upper()
            vol = t.get("volume")
            vol_str = f"{vol:g}" if vol is not None else "?"
            op = self._fmt_price(t.get("open_price"))
            cp = self._fmt_price(t.get("close_price"))
            sl_mark = " 🛑SL" if t.get("closed_by_sl") else ""
            ct = self._parse_dt(t.get("close_time"))
            ct_str = ct.strftime("%Y.%m.%d %H:%M") if ct else "—"
            ticket = t.get("ticket")
            ticket_str = f"🎫 {ticket} · " if ticket else ""
            lines.append(
                f"{i}. {emoji} <b>{t.get('symbol')}</b> {direction} {vol_str} "
                f"| {op} → {cp} | <b>{profit:+.2f}</b>{sl_mark}\n"
                f"    {ticket_str}{ct_str}"
            )
        lines.append(f"\n💰 <b>Batch total: {total:+.2f}</b>")
        lines.append("\nSave these trades to the journal?")
        return "\n".join(lines)

    def format_journal(self, stats: Dict[str, Any]) -> str:
        if stats.get("total", 0) == 0:
            return (
                "📓 <b>Trade journal is empty</b>\n\n"
                "Send a screenshot of your MetaTrader history — "
                "I'll recognize the trades and save them here."
            )

        pf = stats.get("profit_factor")
        pf_str = f"{pf:.2f}" if pf is not None else "∞"
        total_net = stats["total_net"]
        result_emoji = "🟢" if total_net >= 0 else "🔴"

        lines = [
            "📓 <b>Trade journal</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{result_emoji} <b>Total P/L:</b> {total_net:+.2f}",
            f"📊 <b>Total trades:</b> {stats['total']}",
            f"✅ <b>Winners:</b> {stats['wins']}   "
            f"❌ <b>Losers:</b> {stats['losses']}",
            f"🎯 <b>Win rate:</b> {stats['win_rate']:.1f}%",
            f"⚖️ <b>Profit factor:</b> {pf_str}",
            f"🏆 <b>Best:</b> {stats['best']:+.2f}   "
            f"💥 <b>Worst:</b> {stats['worst']:+.2f}",
        ]

        by_symbol = stats.get("by_symbol", {})
        if by_symbol:
            lines.append("\n<b>By symbol:</b>")
            for sym, s in sorted(
                by_symbol.items(), key=lambda kv: kv[1]["net"], reverse=True
            ):
                wr = (s["wins"] / s["count"] * 100.0) if s["count"] else 0.0
                se = "🟢" if s["net"] >= 0 else "🔴"
                lines.append(
                    f"  {se} <b>{sym}</b>: {s['net']:+.2f} "
                    f"({s['count']} trades, WR {wr:.0f}%)"
                )

        recent = stats.get("recent", [])
        if recent:
            lines.append("\n<b>Recent trades:</b>")
            for t in recent:
                profit = t.get("profit") or 0.0
                emoji = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪️")
                ct = self._parse_dt(t.get("close_time"))
                ct_str = ct.strftime("%m.%d %H:%M") if ct else "—"
                direction = (t.get("direction") or "?").upper()
                lines.append(
                    f"  {emoji} {t.get('symbol')} {direction} {profit:+.2f} · {ct_str}"
                )

        return "\n".join(lines)

    @staticmethod
    def _fmt_price(value: Optional[float]) -> str:
        if value is None:
            return "—"
        return f"{value:g}"
