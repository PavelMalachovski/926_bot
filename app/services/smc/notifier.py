"""Telegram message formatting and delivery for SMC analysis results."""

import re
from typing import List, Optional

import httpx
import structlog

from app.services.smc.engine import trends_disagree
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


REDACTED = "***"

# `str(httpx.HTTPStatusError)` embeds the full request URL, and a data-source
# URL carries the API key as a query parameter — so an error detail forwarded
# to Telegram can print the owner's live key into his own chat, where the
# history is durable and syncs to every device he owns. The same class of bug
# was fixed for logs in bcd5728; this is the outbound-message guard. Fetchers
# must not build key-bearing messages in the first place (see twelvedata.py),
# but a future fetcher that forgets must not be able to leak through here.
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[\w.\-~+/]+=*")
# "…apikey=X", "token: X", "MY_SECRET=X" — a named credential and its value.
# "auth" is deliberately absent: "Authorization: Bearer X" is handled above,
# and matching it here would redact the word "Bearer" and leave the token.
_NAMED_SECRET_RE = re.compile(
    r"(?i)([\w.\-]*(?:apikey|api[_\-]?key|token|secret|password|passwd)"
    r"[\w.\-]*\s*[=:]\s*)[\"']?[^\s&\"'#,;]+"
)
# A bare `?key=` / `&sig=` query parameter, which the name rule above leaves
# alone because "key" on its own is far too common in ordinary prose.
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|sig|signature)=)[^&\s\"']+"
)


def redact_secrets(text: str) -> str:
    """Blank out anything credential-shaped in a string bound for Telegram.

    Deliberately narrow: it replaces only the *value* after a credential-ish
    name, so the message still says which pair failed and roughly why ("HTTP
    401 Unauthorized"). A warning the owner cannot act on is its own failure.
    """
    out = _BEARER_RE.sub(rf"\1{REDACTED}", str(text))
    out = _NAMED_SECRET_RE.sub(rf"\1{REDACTED}", out)
    return _QUERY_SECRET_RE.sub(rf"\1{REDACTED}", out)


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
    # The header carries the emoji and stays in the message's normal
    # proportional font, matching every other line (📍/⚡/🛑/🧱). Only the
    # data rows below it need a monospace font for the columns to line up
    # (spec 2026-08-06 §2), so only they go inside <pre>.
    rows = []
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
        rows.append((lv, cell, pool))

    # Every column is width-padded, including RR: a two-digit ratio on one
    # rung would otherwise push that rung's timeframe out of line and undo
    # the whole reason the block is wrapped in <pre>. The width is taken
    # from the widest cell actually present rather than guessed.
    rr_width = max((len(cell) for _, cell, _ in rows), default=0)
    out = [header, "<pre>"]
    for lv, cell, pool in rows:
        out.append(
            f"     {lv.price:.{d}f}   "
            f"{format_distance(abs(lv.price - setup.entry), instrument):>10}   "
            f"{cell:<{rr_width}}   {escape_html(lv.timeframe + pool)}"
        )
    if not rows:
        out.append("     — none ahead")
    out.append("</pre>")
    return out


def _zone_lines(setup, decimals: int) -> List[str]:
    """The untested zones on the trade's own side, further out than the
    entry — alternative deeper entries, the same idea as the 🧱 M5 order
    block one line up. The owner asked to see the untested H1 block the bot
    was hiding (USDCAD 1.40710, 2026-08-06)."""
    out = ["🧱 Untested zones further out   ← deeper entries"]
    for zone in setup.zones_ahead:
        if zone.kind == "FVG":
            # An imbalance never goes through `_mark_zone_state`, so its
            # `touches` is an untouched default, not a count anybody took —
            # and D10 admits a gap only while penetration is zero, so state
            # that instead of printing a number that was never measured.
            state = "untouched"
        else:
            state = f"{zone.touches} touch{'' if zone.touches == 1 else 'es'}"
        out.append(
            f"     {zone.bottom:.{decimals}f} – {zone.top:.{decimals}f}"
            f"   ({zone.kind} · {state})"
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
    if result.h4_trend is not None and result.h1_trend is not None:
        # D6 (owner decision 2026-08-16): H4/H1 trend agreement, always
        # shown — the counter-hourly marker only appears when they actually
        # disagree, which is also what denies the ⭐ below. This never
        # suppresses; it only labels.
        agree = f"H4 {result.h4_trend.value} · H1 {result.h1_trend.value}"
        if trends_disagree(result.h4_trend, result.h1_trend):
            agree += " ⚠️ counter-hourly"
        lines.append(agree)
    if setup.tier_star:
        # Phase 2 sniper redesign (owner decision 2026-08-12): the loud
        # ⭐-tier header — room + sweep + premium/discount + staleness all
        # cleared (app/services/smc/sniper.py). Detector mode: every
        # completed setup is still announced (smc_watcher._send_alert routes
        # everything else through the quiet one-liner instead), this line
        # only labels the higher-confidence ones.
        lines.append("⭐ <b>SNIPER</b>")
    if in_plan is True:
        lines.append("   from this morning's plan")
    elif in_plan is False:
        lines.append("   new zone — not in the plan")
    lines.append("")

    # --- the four actionable lines, in the order the owner works them
    if result.h1_zone:
        kind = "Demand" if result.h1_zone.is_demand else "Supply"
        lines.append(
            f"📍 H1 {kind} zone ({result.h1_zone.kind})  "
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
    # Phase 2 hybrid exit (Task 2, engine.py): TP1 closes half the position
    # at tp1_r*risk, the runner rides to runner_r*risk. Both are computed for
    # every completed setup, star or not — only the ⭐ header above is tier-
    # gated, these levels are not.
    if setup.tp1 is not None and setup.runner_tp is not None:
        lines.append(
            f"🔫 TP1 (half): {setup.tp1:.{d}f} · runner: {setup.runner_tp:.{d}f}"
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


def format_quiet_setup(result: AnalysisResult) -> str:
    """One-line quiet alert for a non-⭐ ("regular") setup.

    Detector mode still announces every setup that fully forms (CLAUDE.md) —
    the two-tier split (Phase 2 sniper redesign, owner decision 2026-08-12)
    only changes how loud the announcement is. A setup that missed the ⭐ bar
    (room/sweep/premium-discount/staleness — see sniper.classify) gets this
    short message instead of the full card: no ladder, no `<pre>` block, no
    chart/pin/buttons (smc_watcher._send_alert routes those separately) —
    just the levels and which conditions it missed, so the owner can judge
    for himself whether it is still worth taking.
    """
    setup = result.setup
    d = result.price_decimals
    is_long = setup.direction == Direction.LONG
    side = "LONG" if is_long else "SHORT"
    tp1 = f"{setup.tp1:.{d}f}" if setup.tp1 is not None else "n/a"
    runner = f"{setup.runner_tp:.{d}f}" if setup.runner_tp is not None else "n/a"
    missed = ", ".join(setup.tier_missed) if setup.tier_missed else "—"
    time_str = to_prague(result.checked_at).strftime("%d.%m %H:%M")
    return (
        f"🔹 <b>{escape_html(result.symbol)} {side}</b> · "
        f"entry {setup.entry:.{d}f} · SL {setup.stop_loss:.{d}f} · "
        f"TP1 {tp1} · runner {runner}\n"
        f"Missed for ⭐: {escape_html(missed)} · {time_str} Prague"
    )


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
        # The kind (OB / FVG) belongs here more than anywhere: this is the
        # screen the owner reads WHILE waiting for price to arrive, which
        # is the whole life of an imbalance zone — the loud alert may never
        # come.
        lines.append(
            f"<b>H1 zone ({zone_kind} · {result.h1_zone.kind}):</b> "
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
    if getattr(plan, "direction_note", None):
        lines.append(f"⚠️ {escape_html(plan.direction_note)}")

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


def format_plan_summary(slot_hhmm, plans, updated_hhmm=None) -> str:
    """One-line-per-pair digest of the auto-built Pre-Market Plans.

    Silent by design (the send uses disable_notification): the owner sees
    that plans exist without being pushed their content — the buttons under
    this message deliver the full plan on demand (spec 2026-08-11 §2).
    """
    title = (
        f"📋 <b>Pre-Market Plan {escape_html(slot_hhmm)}</b> "
        "— press a pair for details"
    )
    if updated_hhmm:
        title += f" · upd {escape_html(updated_hhmm)}"
    lines = [title]
    for plan in plans:
        d = plan.price_decimals
        name = escape_html(plan.pair)
        if plan.market_closed:
            lines.append(f"{name} 😴 market closed")
            continue
        if not plan.scenarios:
            reason = plan.blocker or plan.note or "no plan"
            lines.append(f"{name} ⛔ waiting: {escape_html(reason)}")
            continue
        for s in plan.scenarios:
            is_long = s.direction == Direction.LONG
            arrow = "🔼" if is_long else "🔽"
            spec = " (speculative)" if s.speculative else ""
            lines.append(
                f"{name} {arrow} {'LONG' if is_long else 'SHORT'} zone "
                f"{s.zone_bottom:.{d}f}–{s.zone_top:.{d}f} (~1:{s.rr:.1f}){spec}"
            )
    return "\n".join(lines)


def plan_summary_keyboard(pairs) -> dict:
    """aplan_* buttons under the summary: two pairs per row, then All."""
    rows, row = [], []
    for key in pairs:
        row.append({"text": key, "callback_data": f"aplan_{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "🌐 All pairs", "callback_data": "aplan_ALL"}])
    return {"inline_keyboard": rows}


def format_zone_alert(pair, scenario, decimals: int, marks=None) -> str:
    """Price reached a plan zone: the get-ready moment, with the plan's own
    projected bracket so the owner sees the scenario without pressing
    anything (spec 2026-08-11 §5). SL here is the plan's preliminary one —
    the live 🚨 alert re-anchors it (Rule 6).

    `marks` is the optional `(order_block, fvg)` pair from `structure.m5_marks`
    (owner decision D5, spec §2.2): the M5 order block and imbalance inside
    the zone, i.e. what the owner would actually buy from. It rides along as
    one extra line on this same message, never a message of its own — with
    no marks the text is byte-identical to before this parameter existed.
    """
    d = decimals
    is_long = scenario.direction == Direction.LONG
    kind = "Demand" if is_long else "Supply"
    side = "Buy" if is_long else "Sell"
    spec = " (speculative)" if scenario.speculative else ""
    lines = [
        f"🔔 <b>{escape_html(pair)}</b>: price reached the {kind} zone "
        f"{scenario.zone_bottom:.{d}f}–{scenario.zone_top:.{d}f}",
        f"📋 Plan: {'LONG' if is_long else 'SHORT'} — {side} Limit "
        f"{scenario.entry:.{d}f} | 🛑 SL {scenario.stop_loss:.{d}f} "
        f"| 🎯 TP {scenario.take_profit:.{d}f} | ~1:{scenario.rr:.1f}{spec}",
        f"Watching M5 for a {'bullish' if is_long else 'bearish'} CHoCH + FVG.",
    ]
    block, gap = marks if marks else (None, None)
    if block or gap:
        parts = []
        if block:
            parts.append(f"5m OB {block.bottom:.{d}f}–{block.top:.{d}f}")
        if gap:
            parts.append(f"5m FVG {gap.bottom:.{d}f}–{gap.top:.{d}f}")
        lines.append("🔎 " + " · ".join(parts))
    return "\n".join(lines)


def zone_alert_keyboard(pair: str, until_hhmm: str, block_id: str) -> dict:
    """The 🔕 button under a zone alert (owner decision 2026-08-16).

    Silences this pair's ZONE alerts only — setup alerts, Rule 0.4 news
    warnings and the digest are unaffected (D3). `block_id` travels in the
    callback data so a press is anchored to the block the alert was sent
    in, not to whatever block the press itself happens to land in
    (2026-08-16 owner decision after a 13:55 alert's mute silenced the
    whole evening session when pressed at 14:02). Instrument keys contain
    no underscore, so `pair` and `block_id` split cleanly on the first `_`.
    """
    return {"inline_keyboard": [[{
        "text": f"🔕 Mute {pair} zone alerts till {until_hhmm}",
        "callback_data": f"zmute_{pair}_{block_id}",
    }]]}


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
        self,
        text: str,
        reply_markup: Optional[dict] = None,
        disable_notification: bool = False,
    ) -> Optional[int]:
        """Send a message; returns its message_id or None on failure."""
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if disable_notification:
            payload["disable_notification"] = True
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
