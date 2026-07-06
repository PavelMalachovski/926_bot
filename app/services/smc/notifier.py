"""Telegram message formatting and delivery for SMC analysis results."""

from typing import Optional

import httpx
import structlog

from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict

logger = structlog.get_logger(__name__)

TREND_RU = {Trend.UP: "аптренд", Trend.DOWN: "даунтренд", Trend.FLAT: "флет"}


def format_result(result: AnalysisResult) -> str:
    """Render an AnalysisResult as an HTML Telegram message (Шаблон A/В)."""
    lines = [f"<b>{result.symbol}</b> — Triple Sync + Imbalance"]
    lines.append(
        f"🕐 {result.checked_at.strftime('%d.%m.%Y %H:%M UTC')}"
        + (f" | Сессия: {result.session_name}" if result.session_name else "")
    )
    if result.price:
        lines.append(f"💵 Цена: {result.price:.2f}")
    lines.append("")
    lines.append(f"<b>Диагноз H4:</b> {TREND_RU[result.h4_trend]}")

    if result.h1_zone:
        zone_kind = "Demand" if result.h1_zone.is_demand else "Supply"
        lines.append(
            f"<b>Зона H1 ({zone_kind}):</b> "
            f"{result.h1_zone.bottom:.2f}–{result.h1_zone.top:.2f}"
        )

    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
        setup = result.setup
        side = "Buy" if setup.direction == Direction.LONG else "Sell"
        lines.append(
            f"<b>Triple Sync:</b> Подтверждён ✅ | "
            f"<b>FVG:</b> ${setup.fvg.size:.2f}, заполнение {setup.fvg.fill_pct * 100:.0f}%"
        )
        lines.append("")
        lines.append("<b>Вердикт Бати:</b>")
        if result.verdict == Verdict.APPROVED_MARKET:
            lines.append(
                f"✅ APPROVED (Market) — {side} сейчас по ~{result.price:.2f} "
                f"(цена в зоне FVG {setup.fvg.bottom:.2f}–{setup.fvg.top:.2f})"
            )
            lines.append(f"   Альтернатива: {side} Limit {setup.entry:.2f}")
        else:
            lines.append(f"✅ APPROVED (Limit) — {side} Limit: {setup.entry:.2f}")
        lines.append(f"🛑 SL: {setup.stop_loss:.2f} | 🎯 TP: {setup.take_profit:.2f}")
        lines.append(f"📐 RR: 1:{setup.rr:.1f}")
        if setup.lot_hint:
            lines.append(f"⚖️ Лот: {setup.lot_hint}")
        else:
            lines.append("⚖️ Лот: рассчитай под 1.5–2% риска от депозита")
        if result.funding_warning:
            lines.append(f"⚠️ {result.funding_warning}")
        elif result.funding_rate is not None:
            lines.append(f"💸 Фандинг: {result.funding_rate * 100:.3f}%/8h — норма")
        lines.append("")
        lines.append(
            "⏳ Лимитный ордер действует только в рамках текущей сессии — "
            "не сработал до конца сессии, удали."
        )
    elif result.verdict == Verdict.WATCH:
        lines.append("")
        lines.append("<b>Сетапа пока нет (Setup Watch):</b>")
        for reason in result.reasons:
            lines.append(f"• {reason}")
        if result.watch_notes:
            lines.append("")
            lines.append("<b>Что нужно для входа:</b>")
            for note in result.watch_notes:
                lines.append(f"→ {note}")
    else:
        lines.append("")
        lines.append("<b>Вердикт:</b> ❌ SKIP")
        for reason in result.reasons:
            lines.append(f"• {reason}")

    return "\n".join(lines)


class TelegramNotifier:
    """Minimal standalone Telegram sender (no DB dependencies)."""

    def __init__(self, bot_token: str, chat_id: str):
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    async def send(self, text: str) -> bool:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(f"{self.base_url}/sendMessage", json=payload)
                if response.status_code == 200:
                    return True
                logger.error(
                    "Telegram send failed",
                    status_code=response.status_code,
                    response=response.text,
                )
                return False
        except httpx.HTTPError as e:
            logger.error("Telegram send error", error=str(e))
            return False
