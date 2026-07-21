"""Telegram message formatting and delivery for SMC analysis results."""

from typing import Optional

import httpx
import structlog

from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.sessions import to_prague

logger = structlog.get_logger(__name__)

TREND_LABEL = {Trend.UP: "uptrend", Trend.DOWN: "downtrend", Trend.FLAT: "flat"}


def escape_html(text: str) -> str:
    """Escape <, > and & for Telegram parse_mode=HTML.

    Plain strings (engine reasons like "fill < 50%", news titles like
    "S&P Global PMI") would otherwise be rejected by Telegram as broken tags.
    """
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


URGENT_HEADER = (
    "🚨🚨🚨 <b>URGENT! SETUP FOUND — READY TO TRADE!</b> 🚨🚨🚨"
)


def format_no_setup(result: AnalysisResult) -> str:
    """Compact heartbeat when there is no setup."""
    time_str = to_prague(result.checked_at).strftime("%H:%M")
    if result.verdict == Verdict.OFF_SESSION:
        return (
            f"😴 {result.symbol} {time_str} — off session, entries are not "
            "allowed. Will check again on schedule."
        )
    reason = escape_html(result.reasons[0] if result.reasons else "conditions not met")
    return f"🔍 {result.symbol} {time_str} — no setup. {reason}."


def format_setup_still_active(result: AnalysisResult) -> str:
    """Short reminder when the previously reported setup is still valid."""
    time_str = to_prague(result.checked_at).strftime("%H:%M")
    return (
        f"⏳ {result.symbol} {time_str} — the setup reported earlier is still "
        "active. Nothing new."
    )


def format_result(result: AnalysisResult) -> str:
    """Render an AnalysisResult as an HTML Telegram message (templates A/B)."""
    lines = []
    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
        lines.append(URGENT_HEADER)
        lines.append("")
    lines.append(f"<b>{result.symbol}</b> — Triple Sync + Imbalance")
    lines.append(
        f"🕐 {to_prague(result.checked_at).strftime('%d.%m.%Y %H:%M')} Prague"
        + (f" | Session: {result.session_name}" if result.session_name else "")
    )
    d = result.price_decimals
    if result.price:
        lines.append(f"💵 Price: {result.price:.{d}f}")
    lines.append("")
    lines.append(f"<b>H4 bias:</b> {TREND_LABEL[result.h4_trend]}")

    if result.h1_zone:
        zone_kind = "Demand" if result.h1_zone.is_demand else "Supply"
        lines.append(
            f"<b>H1 zone ({zone_kind}):</b> "
            f"{result.h1_zone.bottom:.{d}f}–{result.h1_zone.top:.{d}f}"
        )

    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
        setup = result.setup
        side = "Buy" if setup.direction == Direction.LONG else "Sell"
        lines.append(
            f"<b>Triple Sync:</b> confirmed ✅ | "
            f"<b>FVG:</b> {setup.fvg.size:.{d}f}, fill {setup.fvg.fill_pct * 100:.0f}%"
        )
        lines.append("")
        lines.append("<b>Verdict:</b>")
        if result.verdict == Verdict.APPROVED_MARKET:
            lines.append(
                f"✅ APPROVED (Market) — {side} now at ~{result.price:.{d}f} "
                f"(price is inside the FVG {setup.fvg.bottom:.{d}f}–{setup.fvg.top:.{d}f})"
            )
            lines.append(f"   Alternative: {side} Limit {setup.entry:.{d}f}")
        else:
            lines.append(f"✅ APPROVED (Limit) — {side} Limit: {setup.entry:.{d}f}")
        lines.append(
            f"🛑 SL: {setup.stop_loss:.{d}f} | 🎯 TP: {setup.take_profit:.{d}f}"
        )
        lines.append(f"📐 RR: 1:{setup.rr:.1f}")
        if setup.lot_hint:
            lines.append(f"⚖️ Size: {setup.lot_hint}")
        else:
            lines.append("⚖️ Size: calculate for 1.5–2% account risk")
        if result.funding_warning:
            lines.append(f"⚠️ {result.funding_warning}")
        elif result.funding_rate is not None:
            lines.append(f"💸 Funding: {result.funding_rate * 100:.3f}%/8h — normal")
        lines.append("")
        lines.append(
            "⏳ The limit order is valid only within the current session — "
            "cancel it if unfilled by session end."
        )
    elif result.verdict == Verdict.WATCH:
        lines.append("")
        lines.append("<b>No setup yet (Setup Watch):</b>")
        for reason in result.reasons:
            lines.append(f"• {escape_html(reason)}")
        if result.watch_notes:
            lines.append("")
            lines.append("<b>What is needed for an entry:</b>")
            for note in result.watch_notes:
                lines.append(f"→ {escape_html(note)}")
    else:
        lines.append("")
        lines.append("<b>Verdict:</b> ❌ SKIP")
        for reason in result.reasons:
            lines.append(f"• {escape_html(reason)}")

    return "\n".join(lines)


def format_plan(plan, min_rr: float = 2.0) -> str:
    """Render a PairPlan as an HTML pre-market briefing message (Шаблон B)."""
    from app.services.smc.plan import PairPlan  # noqa: F401 (type hint only)

    d = plan.price_decimals
    trend_label = TREND_LABEL[plan.h4_trend]
    lines = [f"📋 <b>{plan.pair}</b> — Pre-Market Plan (H4 {trend_label})"]
    if plan.price:
        lines.append(f"💵 {plan.price:.{d}f}")

    if plan.note and not plan.scenarios:
        lines.append("")
        lines.append(f"ℹ️ {escape_html(plan.note)}")
        return "\n".join(lines)

    for s in plan.scenarios:
        is_long = s.direction == Direction.LONG
        arrow = "🔼" if is_long else "🔽"
        side = "Buy" if is_long else "Sell"
        head = (
            f"{arrow} <b>{'LONG' if is_long else 'SHORT'}</b>"
            + (" (speculative)" if s.speculative else " plan")
        )
        lines.append("")
        lines.append(head)
        lines.append(
            f"   Zone {'Demand' if is_long else 'Supply'} "
            f"{s.zone_bottom:.{d}f}–{s.zone_top:.{d}f}"
        )
        lines.append(
            f"   {side} Limit {s.entry:.{d}f} | 🛑 SL {s.stop_loss:.{d}f} "
            f"| 🎯 TP {s.take_profit:.{d}f}"
        )
        rr_note = "" if s.rr >= min_rr else "  ⚠️ below 1:2 — likely SKIP"
        lines.append(f"   📐 RR ~1:{s.rr:.1f} (approx){rr_note}")
        lines.append(
            f"   Trigger: M5 {'bullish' if is_long else 'bearish'} CHoCH + "
            "FVG inside the zone"
        )

    lines.append("")
    lines.append(
        "⚠️ Preliminary plan: SL is beyond the H1 zone; the live 🚨 alert will "
        "tighten it to the M5 pivot. Order lives only within its session."
    )
    return "\n".join(lines)


class TelegramNotifier:
    """Minimal standalone Telegram sender (no DB dependencies)."""

    def __init__(self, bot_token: str, chat_id: str):
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    async def _api(self, method: str, **payload) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(f"{self.base_url}/{method}", json=payload)
                data = response.json()
                if response.status_code == 200 and data.get("ok"):
                    return data.get("result")
                logger.error(
                    "Telegram API call failed",
                    method=method,
                    status_code=response.status_code,
                    response=response.text[:300],
                )
                return None
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Telegram API error", method=method, error=str(e))
            return None

    async def send(
        self, text: str, reply_markup: Optional[dict] = None
    ) -> Optional[int]:
        """Send a message; returns its message_id or None on failure."""
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self._api("sendMessage", **payload)
        return result.get("message_id") if result else None

    async def edit_message(
        self, message_id: int, text: str, reply_markup: Optional[dict] = None
    ) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self._api("editMessageText", **payload) is not None

    async def send_photo(
        self,
        photo: bytes,
        caption: Optional[str] = None,
        reply_to: Optional[int] = None,
    ) -> Optional[int]:
        """Send a PNG photo (multipart); returns message_id or None."""
        data = {"chat_id": self.chat_id}
        if caption:
            data["caption"] = caption
        if reply_to:
            data["reply_to_message_id"] = str(reply_to)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/sendPhoto",
                    data=data,
                    files={"photo": ("setup.png", photo, "image/png")},
                )
                payload = response.json()
                if response.status_code == 200 and payload.get("ok"):
                    return payload["result"].get("message_id")
                logger.error("Telegram sendPhoto failed", response=response.text[:300])
                return None
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Telegram sendPhoto error", error=str(e))
            return None

    async def pin(self, message_id: int) -> None:
        await self._api(
            "pinChatMessage",
            chat_id=self.chat_id,
            message_id=message_id,
            disable_notification=True,
        )

    async def unpin(self, message_id: int) -> None:
        await self._api(
            "unpinChatMessage", chat_id=self.chat_id, message_id=message_id
        )
