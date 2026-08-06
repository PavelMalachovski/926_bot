"""Telegram message formatting and delivery for SMC analysis results."""

from typing import List, Optional

import httpx
import structlog

from app.services.smc.instruments import Instrument, get_instrument
from app.services.smc.liquidity import LiquidityLevel
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


def format_target(level: LiquidityLevel, decimals: int) -> str:
    """Name a liquidity objective: 'H1 swing high 3221.00 (EQH x2)'."""
    kind = "swing high" if level.is_high else "swing low"
    pool = ""
    if level.equal_count > 1:
        pool = f" (EQ{'H' if level.is_high else 'L'} x{level.equal_count})"
    return f"{level.timeframe} {kind} {level.price:.{decimals}f}{pool}"


def format_distance(value: float, instrument: Instrument) -> str:
    """Distances read in the instrument's own units — the same split
    `engine._fmt_size` makes. "1008 pips" for a $10 ETH move is how a level
    gets misjudged at a glance."""
    if instrument.source == "crypto":
        return f"${value:,.2f}"
    return f"{value / instrument.pip:.1f} pips"


def _rr_cell(rr: float) -> str:
    """One RR cell: a ratio, or a dash when the rung is behind the entry."""
    return f"1:{rr:.1f}" if rr > 0 else "—"


def _ladder_lines(setup, instrument: Instrument) -> List[str]:
    """One rung per pool: price, distance, RR from the FVG edge and — when an
    order block exists — from the block. Two entries, two RRs; the owner picks.

    Note the two RRs are two different trades, not one number that got better:
    the deeper entry risks less in price terms, so the same market noise is a
    larger fraction of it, and the limit may never fill. Hence the header
    naming both entries rather than a single "RR" column.
    """
    d = instrument.price_decimals
    risk = abs(setup.entry - setup.stop_loss)
    is_long = setup.direction == Direction.LONG
    ob_entry = None
    if setup.order_block:
        ob_entry = setup.order_block.top if is_long else setup.order_block.bottom
    ob_risk = abs(ob_entry - setup.stop_loss) if ob_entry is not None else None

    header = "🎯 Unswept liquidity ahead"
    header += "      RR from FVG / from OB" if ob_risk else "      RR from FVG"
    out = [header]
    for lv in setup.ladder:
        tp = (
            lv.price - instrument.sl_buffer if is_long
            else lv.price + instrument.sl_buffer
        )
        rr = (tp - setup.entry if is_long else setup.entry - tp) / risk
        # A pool inside the stop buffer gives a non-positive reward — the
        # engine clears the objective for it but keeps the rung. "1:-0.0" is
        # not a number to read on a line about money; the dash says "no".
        cell = _rr_cell(rr)
        if ob_risk:
            rr_ob = (tp - ob_entry if is_long else ob_entry - tp) / ob_risk
            cell += f" / {_rr_cell(rr_ob)}"
        pool = (
            f" · EQ{'H' if lv.is_high else 'L'} x{lv.equal_count}"
            if lv.equal_count > 1 else ""
        )
        out.append(
            f"     {lv.price:.{d}f}   "
            f"{format_distance(abs(lv.price - setup.entry), instrument):>10}   "
            f"{cell}   {escape_html(lv.timeframe + pool)}"
        )
    if not setup.ladder:
        out.append("     — none ahead")
    return out


def _zone_lines(setup, decimals: int) -> List[str]:
    """The untested zones further out — the owner asked to see the block the
    bot is not entering from (USDCAD 1.40710, 2026-08-06)."""
    out = ["🧱 Untested zones further out"]
    for zone in setup.zones_ahead:
        touches = f"{zone.touches} touch{'' if zone.touches == 1 else 'es'}"
        out.append(
            f"     {zone.bottom:.{decimals}f} – {zone.top:.{decimals}f}   ({touches})"
        )
    return out


def _direction_source_label(result: AnalysisResult, is_long: bool) -> str:
    """Where the direction came from — the guard against a silent first-leg
    or lower-timeframe entry (spec §2, owner constraint: trend only)."""
    source = getattr(result, "direction_source", "h4")
    if source == "h1":
        return (
            "⚠️ H4 flat — direction from H1 "
            f"{'uptrend' if is_long else 'downtrend'}"
        )
    if source == "h4_choch":
        return "⚠️ H4 flat — direction from CHoCH (first leg, not with-trend)"
    return f"H4 {TREND_LABEL[result.h4_trend]}"


def _format_detector_alert(result: AnalysisResult, in_plan: Optional[bool]) -> str:
    """The announcement: four actionable lines, then the ladders.

    Detector mode (spec 2026-08-06): the bot says a setup has formed and shows
    the levels; the owner places his own orders. Nothing here recommends a
    trade — RR is a consequence of the levels, not an input to them.
    """
    setup = result.setup
    # The symbol always resolves: the engine that produced this setup was
    # itself built from the registry, so a KeyError here would mean the result
    # is not one this bot can trade — let it surface rather than fabricating
    # units and printing quietly wrong levels.
    instrument = get_instrument(result.symbol)
    d = result.price_decimals
    is_long = setup.direction == Direction.LONG
    side = "LONG" if is_long else "SHORT"

    lines = [
        f"🚨 <b>SETUP READY — {escape_html(result.symbol)} · {side}</b>"
        f" · {_direction_source_label(result, is_long)}"
    ]
    if in_plan is True:
        lines.append("   from this morning's plan")
    elif in_plan is False:
        lines.append("   new zone — not in the plan")
    lines.append("")

    # --- the four actionable lines, in the order the owner works them
    if result.h1_zone:
        kind = "Demand" if result.h1_zone.is_demand else "Supply"
        lines.append(
            f"📍 H1 {kind} zone      "
            f"{result.h1_zone.bottom:.{d}f} – {result.h1_zone.top:.{d}f}"
        )
    lines.append(
        f"⚡ M5 imbalance (FVG)   "
        f"{setup.fvg.bottom:.{d}f} – {setup.fvg.top:.{d}f}"
        f"   ← limit order ({setup.entry:.{d}f})"
    )
    if setup.order_block:
        ob_entry = setup.order_block.top if is_long else setup.order_block.bottom
        lines.append(
            f"🧱 M5 order block       "
            f"{setup.order_block.bottom:.{d}f} – {setup.order_block.top:.{d}f}"
            f"   ← deeper entry ({ob_entry:.{d}f})"
        )
    # The stop sits one buffer beyond the swept extreme; show the extreme
    # itself, because that wick is what the owner reads off the chart.
    #
    # COUPLING: `TradeSetup` carries only the stop, so the wick is
    # reconstructed with `Instrument.sl_buffer` — the same value
    # `TripleSyncEngine` used to subtract it. Exact only while no caller
    # overrides the engine's `sl_buffer=` argument (none does today; pinned by
    # test_swept_wick_is_reconstructed_from_the_instrument_buffer). If a real
    # override ever ships, carry the extreme on TradeSetup instead of widening
    # the guess here.
    extreme = (
        setup.stop_loss + instrument.sl_buffer if is_long
        else setup.stop_loss - instrument.sl_buffer
    )
    lines.append(
        f"🛑 Swept liquidity      {extreme:.{d}f}"
        f"   ← stop behind the wick ({setup.stop_loss:.{d}f} with buffer)"
    )

    lines.append("")
    lines.extend(_ladder_lines(setup, instrument))
    if setup.zones_ahead:
        lines.append("")
        lines.extend(_zone_lines(setup, d))

    # --- warnings: impossible to miss, but they do not push the levels down
    notes = []
    if setup.entry_is_market:
        notes.append("   ▶️ price is inside the imbalance right now")
    for warning in result.warnings:
        notes.append(f"   ⚠️ {escape_html(warning)}")
    if result.funding_warning:
        notes.append(f"   ⚠️ {escape_html(result.funding_warning)}")
    if notes:
        lines.append("")
        lines.extend(notes)

    # --- ref: measured context, not instruction
    lines.append("")
    fvg_ref = (
        f"   ref · FVG {setup.fvg.size:.{d}f}, "
        f"{setup.fvg.fill_pct * 100:.0f}% filled"
    )
    if result.session_name:
        fvg_ref += f" · {escape_html(result.session_name)}"
    lines.append(fvg_ref)
    if setup.take_profit is not None and setup.target is not None:
        lines.append(
            f"   ref · tracked objective {setup.take_profit:.{d}f} "
            f"(1:{setup.rr:.1f}) · {escape_html(format_target(setup.target, d))}"
        )
    if setup.lot_hint:
        lines.append(f"   ref · size {escape_html(setup.lot_hint)}")
    if result.funding_rate is not None and not result.funding_warning:
        # Rule 9.3: a benign funding reading is still measured context. The
        # actionable brackets already left as ⚠️ lines above.
        lines.append(f"   ref · funding {result.funding_rate * 100:.3f}%/8h")
    if getattr(result, "profile_key", "conservative") == "aggressive":
        lines.append("   ref · aggressive profile — first-leg entry")
    lines.append("   ref · a pending order expires with this session (Rule 10)")
    lines.append(
        f"   ref · {to_prague(result.checked_at).strftime('%d.%m %H:%M')} Prague"
        + (f" · price {result.price:.{d}f}" if result.price else "")
    )
    return "\n".join(lines)


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


def format_result(result: AnalysisResult, in_plan: Optional[bool] = None) -> str:
    """Render an AnalysisResult as an HTML Telegram message.

    `in_plan` is the plan provenance of the announced zone: True renders "from
    this morning's plan", False "new zone — not in the plan", and None omits
    the line entirely — no `/plan` ran today, so the bot does not claim a
    provenance it cannot know.
    """
    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
        if result.setup is not None:
            return _format_detector_alert(result, in_plan)
        logger.error("Approved result without a setup", symbol=result.symbol)
    lines = []
    lines.append(f"<b>{escape_html(result.symbol)}</b> — Triple Sync + Imbalance")
    if getattr(result, "profile_key", "conservative") == "aggressive":
        lines.append("⚡ <b>Aggressive profile</b> — first-leg entry, lower-probability")
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

    if result.verdict == Verdict.WATCH:
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


def format_plan(plan, live_line: str = None, as_of: str = None) -> str:
    """Render a PairPlan as an HTML pre-market briefing message (Шаблон B).

    `live_line` (optional) folds in the watcher's live checklist status so the
    plan and the live view are one picture. `as_of` is the Prague time of the
    last closed M5 candle, shown so data freshness is visible.
    """
    from app.services.smc.plan import PairPlan  # noqa: F401 (type hint only)

    d = plan.price_decimals
    trend_label = TREND_LABEL[plan.h4_trend]
    lines = [f"📋 <b>{plan.pair}</b> — Pre-Market Plan (H4 {trend_label})"]
    if plan.price:
        suffix = f"  ·  M5 close {as_of} Prague" if as_of else ""
        lines.append(f"💵 {plan.price:.{d}f}{suffix}")
    if live_line:
        lines.append(f"📍 <b>Live now:</b> {live_line}")

    if not plan.scenarios and (plan.note or plan.blocker):
        # No setup in the plan: say which stage is missing, in the live
        # checklist's own words (spec 2026-08-06 §6). "→" is the same marker
        # format_result uses for its watch notes.
        lines.append("")
        if plan.blocker:
            lines.append(f"→ {escape_html(plan.blocker)}")
        else:  # no structural blocker (market closed) — just the note
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
        lines.append(f"   📐 RR ~1:{s.rr:.1f} (approx)")
        lines.append(
            f"   Trigger: M5 {'bullish' if is_long else 'bearish'} CHoCH + "
            "FVG inside the zone"
        )

    lines.append("")
    lines.append(
        "⚠️ SL is preliminary (beyond the H1 zone); the live 🚨 alert "
        "re-anchors it to the swept extreme and it may be wider. Order "
        "lives only within its session."
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
