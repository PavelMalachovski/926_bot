"""Trade journal service.

Parses MetaTrader history screenshots with OpenAI Vision, stores the trades in
the database behind a confirmation step, and produces trading statistics for the
`/journal` command.
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.models import TradeModel

logger = structlog.get_logger(__name__)


# Keys we expect back from the vision model for every parsed trade.
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
    "- Use null for anything not visible. Do not invent values."
)


class JournalService:
    """Business logic for the personal trade journal."""

    def __init__(self) -> None:
        self.api_key = settings.api.openai_api_key
        self.model = settings.api.openai_model or "gpt-4o-mini"
        self.vision_url = "https://api.openai.com/v1/chat/completions"

    # ------------------------------------------------------------------ #
    # Screenshot parsing                                                 #
    # ------------------------------------------------------------------ #
    async def parse_screenshot(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        """Extract a list of normalized trade dicts from a screenshot."""
        if not self.api_key:
            logger.error("OpenAI API key not configured; cannot parse screenshot")
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

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.vision_url, headers=headers, json=payload, timeout=90.0
            )
            response.raise_for_status()
            result = response.json()

        content = result["choices"][0]["message"]["content"]
        data = json.loads(content)
        raw_trades = data.get("trades", []) if isinstance(data, dict) else []
        return [self._normalize(t) for t in raw_trades if isinstance(t, dict)]

    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce a raw parsed trade into typed values."""
        trade: Dict[str, Any] = {k: raw.get(k) for k in _TRADE_KEYS}

        # Strings
        trade["ticket"] = self._as_str(trade.get("ticket"))
        trade["symbol"] = (self._as_str(trade.get("symbol")) or "UNKNOWN").upper()
        direction = self._as_str(trade.get("direction"))
        trade["direction"] = direction.lower() if direction else None

        # Numbers
        for key in ("volume", "open_price", "close_price", "sl", "tp"):
            trade[key] = self._as_float(trade.get(key))
        for key in ("profit", "swap", "commission", "taxes"):
            trade[key] = self._as_float(trade.get(key)) or 0.0

        # Dates
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
    def _as_dt(value: Any) -> Optional[datetime]:
        if not value:
            return None
        text = str(value).strip().replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------ #
    # Persistence (confirmation workflow)                                #
    # ------------------------------------------------------------------ #
    async def save_pending_batch(
        self, db: AsyncSession, telegram_id: int, trades: List[Dict[str, Any]]
    ) -> str:
        """Persist parsed trades as a pending batch, return its batch id."""
        batch_id = uuid.uuid4().hex[:16]
        for t in trades:
            db.add(
                TradeModel(
                    telegram_id=telegram_id,
                    ticket=t.get("ticket"),
                    symbol=t.get("symbol") or "UNKNOWN",
                    direction=t.get("direction"),
                    volume=t.get("volume"),
                    open_price=t.get("open_price"),
                    close_price=t.get("close_price"),
                    open_time=t.get("open_time"),
                    close_time=t.get("close_time"),
                    sl=t.get("sl"),
                    tp=t.get("tp"),
                    profit=t.get("profit") or 0.0,
                    swap=t.get("swap") or 0.0,
                    commission=t.get("commission") or 0.0,
                    taxes=t.get("taxes") or 0.0,
                    closed_by_sl=bool(t.get("closed_by_sl")),
                    status="pending",
                    batch_id=batch_id,
                )
            )
        await db.commit()
        return batch_id

    async def confirm_batch(
        self, db: AsyncSession, telegram_id: int, batch_id: str
    ) -> Dict[str, int]:
        """Confirm a pending batch, dropping duplicates by ticket.

        Returns {"saved": n, "duplicates": m}.
        """
        result = await db.execute(
            select(TradeModel).where(
                TradeModel.telegram_id == telegram_id,
                TradeModel.batch_id == batch_id,
                TradeModel.status == "pending",
            )
        )
        pending = list(result.scalars().all())
        if not pending:
            return {"saved": 0, "duplicates": 0}

        # Existing confirmed tickets for this user (for de-duplication).
        existing_result = await db.execute(
            select(TradeModel.ticket).where(
                TradeModel.telegram_id == telegram_id,
                TradeModel.status == "confirmed",
                TradeModel.ticket.isnot(None),
            )
        )
        existing_tickets = {t for (t,) in existing_result.all() if t}

        saved = 0
        duplicates = 0
        seen_in_batch: set = set()
        for trade in pending:
            ticket = trade.ticket
            if ticket and (ticket in existing_tickets or ticket in seen_in_batch):
                await db.delete(trade)
                duplicates += 1
                continue
            trade.status = "confirmed"
            if ticket:
                seen_in_batch.add(ticket)
            saved += 1

        await db.commit()
        return {"saved": saved, "duplicates": duplicates}

    async def discard_batch(
        self, db: AsyncSession, telegram_id: int, batch_id: str
    ) -> int:
        """Delete a pending batch. Returns number of rows removed."""
        result = await db.execute(
            delete(TradeModel).where(
                TradeModel.telegram_id == telegram_id,
                TradeModel.batch_id == batch_id,
                TradeModel.status == "pending",
            )
        )
        await db.commit()
        return result.rowcount or 0

    # ------------------------------------------------------------------ #
    # Statistics                                                         #
    # ------------------------------------------------------------------ #
    async def get_stats(self, db: AsyncSession, telegram_id: int) -> Dict[str, Any]:
        """Compute journal statistics for a user's confirmed trades."""
        result = await db.execute(
            select(TradeModel)
            .where(
                TradeModel.telegram_id == telegram_id,
                TradeModel.status == "confirmed",
            )
            .order_by(TradeModel.close_time.desc().nullslast())
        )
        trades = list(result.scalars().all())

        total = len(trades)
        if total == 0:
            return {"total": 0}

        def net(t: TradeModel) -> float:
            return (
                (t.profit or 0.0)
                + (t.swap or 0.0)
                + (t.commission or 0.0)
                - (t.taxes or 0.0)
            )

        wins = [t for t in trades if net(t) > 0]
        losses = [t for t in trades if net(t) < 0]
        total_net = sum(net(t) for t in trades)
        gross_profit = sum(net(t) for t in wins)
        gross_loss = sum(net(t) for t in losses)  # negative

        by_symbol: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            s = by_symbol.setdefault(
                t.symbol or "UNKNOWN",
                {"count": 0, "wins": 0, "net": 0.0},
            )
            s["count"] += 1
            s["net"] += net(t)
            if net(t) > 0:
                s["wins"] += 1

        return {
            "total": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / total * 100.0) if total else 0.0,
            "total_net": total_net,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": (
                gross_profit / abs(gross_loss) if gross_loss else None
            ),
            "best": max((net(t) for t in trades), default=0.0),
            "worst": min((net(t) for t in trades), default=0.0),
            "by_symbol": by_symbol,
            "recent": trades[:5],
        }

    # ------------------------------------------------------------------ #
    # Formatting (HTML)                                                  #
    # ------------------------------------------------------------------ #
    def format_preview(self, trades: List[Dict[str, Any]]) -> str:
        """Human-readable preview of parsed trades before saving."""
        if not trades:
            return (
                "🔍 На скриншоте не удалось распознать ни одной сделки.\n"
                "Попробуй прислать более чёткий скрин истории MT4."
            )

        lines = [f"📸 <b>Распознано сделок: {len(trades)}</b>\n"]
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
            ct = t.get("close_time")
            ct_str = ct.strftime("%Y.%m.%d %H:%M") if ct else "—"
            lines.append(
                f"{i}. {emoji} <b>{t.get('symbol')}</b> {direction} {vol_str} "
                f"| {op} → {cp} | <b>{profit:+.2f}</b>{sl_mark}\n"
                f"    🎫 {t.get('ticket') or '—'} · {ct_str}"
            )
        lines.append(f"\n💰 <b>Итог по батчу: {total:+.2f}</b>")
        lines.append("\nСохранить эти сделки в журнал?")
        return "\n".join(lines)

    def format_journal(self, stats: Dict[str, Any]) -> str:
        """Format the /journal statistics message."""
        if stats.get("total", 0) == 0:
            return (
                "📓 <b>Журнал сделок пуст</b>\n\n"
                "Пришли скриншот истории сделок из MetaTrader — "
                "я распознаю и сохраню их сюда."
            )

        win_rate = stats["win_rate"]
        pf = stats.get("profit_factor")
        pf_str = f"{pf:.2f}" if pf is not None else "∞"
        total_net = stats["total_net"]
        result_emoji = "🟢" if total_net >= 0 else "🔴"

        lines = [
            "📓 <b>Журнал сделок</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{result_emoji} <b>Итоговый P/L:</b> {total_net:+.2f}",
            f"📊 <b>Всего сделок:</b> {stats['total']}",
            f"✅ <b>Прибыльных:</b> {stats['wins']}   "
            f"❌ <b>Убыточных:</b> {stats['losses']}",
            f"🎯 <b>Win rate:</b> {win_rate:.1f}%",
            f"⚖️ <b>Profit factor:</b> {pf_str}",
            f"🏆 <b>Лучшая:</b> {stats['best']:+.2f}   "
            f"💥 <b>Худшая:</b> {stats['worst']:+.2f}",
        ]

        by_symbol = stats.get("by_symbol", {})
        if by_symbol:
            lines.append("\n<b>По символам:</b>")
            for sym, s in sorted(
                by_symbol.items(), key=lambda kv: kv[1]["net"], reverse=True
            ):
                wr = (s["wins"] / s["count"] * 100.0) if s["count"] else 0.0
                se = "🟢" if s["net"] >= 0 else "🔴"
                lines.append(
                    f"  {se} <b>{sym}</b>: {s['net']:+.2f} "
                    f"({s['count']} сд., WR {wr:.0f}%)"
                )

        recent = stats.get("recent", [])
        if recent:
            lines.append("\n<b>Последние сделки:</b>")
            for t in recent:
                profit = t.profit or 0.0
                emoji = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪️")
                ct = t.close_time.strftime("%m.%d %H:%M") if t.close_time else "—"
                direction = (t.direction or "?").upper()
                lines.append(
                    f"  {emoji} {t.symbol} {direction} {profit:+.2f} · {ct}"
                )

        return "\n".join(lines)

    @staticmethod
    def _fmt_price(value: Optional[float]) -> str:
        if value is None:
            return "—"
        return f"{value:g}"
