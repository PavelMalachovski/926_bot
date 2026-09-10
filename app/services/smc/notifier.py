"""Telegram message formatting and delivery for SMC analysis results."""

import re
from typing import List, Optional

import httpx
import structlog

from app.services.smc.engine import position_size, trends_disagree
from app.services.smc.i18n import get_language, t, tier_label
from app.services.smc.instruments import Instrument, get_instrument
from app.services.smc.liquidity import LiquidityLevel, take_profits
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.sessions import session_end_utc, to_prague

logger = structlog.get_logger(__name__)

TREND_LABEL = {Trend.UP: "uptrend", Trend.DOWN: "downtrend", Trend.FLAT: "flat"}


def trend_label(trend: Trend) -> str:
    """'uptrend' / 'downtrend' / 'flat' in the bot's language."""
    return t(TREND_LABEL[trend])


def side_label(is_long: bool) -> str:
    """'Demand' / 'Supply' in the bot's language (a zone's side)."""
    return t("Demand") if is_long else t("Supply")


def bias_label(is_long: bool) -> str:
    """'bullish' / 'bearish' in the bot's language."""
    return t("bullish") if is_long else t("bearish")


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
    kind = t("swing high") if level.is_high else t("swing low")
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
    return t("{pips} pips", pips=f"{value / instrument.pip:.1f}")


# Owner decision D22 (2026-08-30): a setup can now form without a valid M5
# imbalance, so every line that used to assume one has to say what it found
# instead. Short human names for `best_rejected_fvg`'s problem codes.
_FVG_PROBLEMS = {
    "size": "too small",
    "fill": "over half filled",
    "closed": "closed through",
    "session": "from an earlier session",
}


def _imbalance_flaw(problems) -> str:
    """'too small, over half filled' — why a gap failed Rule 4."""
    named = [t(_FVG_PROBLEMS[p]) for p in problems if p in _FVG_PROBLEMS]
    return ", ".join(named) if named else t("not valid")


def _entry_label(setup) -> str:
    """What the entry price is measured from — the ladder rung D22 used."""
    return {"fvg": "FVG", "ob": "OB", "market": t("market")}.get(
        setup.entry_source, "FVG"
    )


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

    header = t("🎯 Unswept liquidity ahead")
    # D22: the first RR column is measured from whatever rung supplied the
    # entry, so it must be named after that rung rather than always "FVG".
    entry_label = _entry_label(setup)
    header += (
        t("      RR from {entry} / from OB", entry=entry_label)
        if ob_risk
        else t("      RR from {entry}", entry=entry_label)
    )
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
        out.append(t("     — none ahead"))
    out.append("</pre>")
    return out


def _zone_lines(setup, decimals: int) -> List[str]:
    """The untested zones on the trade's own side, further out than the
    entry — alternative deeper entries, the same idea as the 🧱 M5 order
    block one line up. The owner asked to see the untested H1 block the bot
    was hiding (USDCAD 1.40710, 2026-08-06)."""
    out = [t("🧱 Untested zones further out   ← deeper entries")]
    for zone in setup.zones_ahead:
        if zone.kind == "FVG":
            # An imbalance never goes through `_mark_zone_state`, so its
            # `touches` is an untouched default, not a count anybody took —
            # and D10 admits a gap only while penetration is zero, so state
            # that instead of printing a number that was never measured.
            state = t("untouched")
        else:
            state = touches_label(zone.touches)
        out.append(
            f"     {zone.bottom:.{decimals}f} – {zone.top:.{decimals}f}"
            f"   ({zone.kind} · {state})"
        )
    return out


def touches_label(touches: int) -> str:
    """'1 touch' / '3 touches' — Russian needs three plural forms."""
    if touches == 1:
        return t("1 touch")
    few = 2 <= touches % 10 <= 4 and not 12 <= touches % 100 <= 14
    if few and get_language() == "ru":
        return t("{n} touches (few)", n=touches)
    return t("{n} touches", n=touches)


def _direction_source_label(result: AnalysisResult, is_long: bool) -> str:
    """Where the direction came from — the guard against a silent first-leg
    or lower-timeframe entry (spec §2, owner constraint: trend only)."""
    source = getattr(result, "direction_source", "h4")
    trend = t("uptrend") if is_long else t("downtrend")
    if source == "h1":
        return t("⚠️ H4 flat — direction from H1 {trend}", trend=trend)
    if source == "h1_counter":
        # D23 (owner decision 2026-08-31): H4 trends one way and H1 has
        # turned the other. The setup is shown so the owner sees what his
        # own eye sees on the lower timeframe, and it can never earn the ⭐
        # (D6 denies it) — so the header has to say plainly that this one
        # runs against the higher timeframe.
        return t("⚠️ against H4 — direction from the H1 {trend}", trend=trend)
    if source == "h4_choch":
        return t("⚠️ H4 flat — direction from CHoCH (first leg, not with-trend)")
    if source == "range":
        # D11: this direction only exists because BOTH H4 and H1 read FLAT
        # and a range was found — "H4 flat" alone would be true but would
        # hide that H1 was flat too and that the direction came from a
        # boundary, not a trend at all.
        return t("⚠️ H4/H1 flat — direction from the range boundary")
    return f"H4 {trend_label(result.h4_trend)}"


def _pd_line(result: AnalysisResult) -> Optional[str]:
    """Where the entry sits inside its dealing range, as a number.

    The ⭐'s `pd` condition used to be a verdict with nothing behind it: an
    alert said "Missed for ⭐: pd" and the owner had no way to see whether he
    was buying at 51% of the range or at 90%. This is that number (owner
    decision D17, 2026-08-26). None when no dealing range contains the entry
    — the same unmeasurable-and-passing case `sniper.classify` already has,
    and a line claiming a percentage of a range price has left would be a
    guess.
    """
    read = result.pd
    if read is None:
        return None
    d = result.price_decimals
    ote = f"OTE {read.ote_low:.{d}f}–{read.ote_high:.{d}f}"
    return (
        f"PD {read.pct}% {escape_html(t(read.label))} "
        f"({escape_html(read.range.timeframe)} "
        f"{read.range.low:.{d}f}–{read.range.high:.{d}f}) · "
        f"{ote}{' ✓' if read.in_ote else ''}"
    )


def format_pd_alert(
    pair: str,
    read,
    decimals: int,
    zone=None,
    target: Optional[LiquidityLevel] = None,
) -> str:
    """PD radar: price reached the half of its range the bias wants.

    Owner request 2026-08-26 — "сообщение когда цена в дискаунте для покупки,
    и наоборот на верхах для продажи". A get-ready message, not a setup: it
    says where price is and what to watch, and the M5 trigger is still the
    engine's job. Fires only in the direction of the H4/H1 bias (owner
    choice), once per pair per session block.

    `zone` is the H1 zone of interest when Rule 2 already found one, `target`
    the nearest unswept pool ahead — both optional, because a discount is
    worth knowing about before either exists.
    """
    d = decimals
    is_long = read.direction == Direction.LONG
    rng = read.range
    head = "🟢" if is_long else "🔴"
    lines = [
        f"{head} <b>{escape_html(pair)} — {escape_html(t(read.label).upper())}</b>"
        + t(" · bias {side}", side="LONG" if is_long else "SHORT"),
        f"{escape_html(rng.timeframe + ' ' + t('range')):<12} "
        f"{rng.low:.{d}f} – {rng.high:.{d}f}",
        f"{t('Price'):<12} {read.price:.{d}f}   ← "
        + t("{pct}% of the range", pct=read.pct),
        f"{'OTE':<12} {read.ote_low:.{d}f} – {read.ote_high:.{d}f}"
        + (t("   ⭐ price is inside") if read.in_ote else ""),
        "",
    ]
    if zone is not None:
        lines.append(
            t("📍 H1 {kind} zone      {lo} – {hi}", kind=side_label(is_long),
              lo=f"{zone.bottom:.{d}f}", hi=f"{zone.top:.{d}f}")
        )
    if target is not None:
        lines.append(
            t("🎯 Liquidity ahead   {target}",
              target=escape_html(format_target(target, d)))
        )
    lines.append(t("Watching M5 for a {bias} CHoCH + FVG.", bias=bias_label(is_long)))
    return "\n".join(lines)


def _targets_line(targets, decimals: int) -> str:
    """'🎯 TP1 2489.00 (1:1.2) · TP2 2500.37 (1:2.0) · TP3 2529.26 (1:3.5)'
    — or the honest empty case."""
    if not targets:
        return t("🎯 no unswept liquidity ahead")
    return "🎯 " + " · ".join(
        f"TP{i} {tp.price:.{decimals}f} (1:{tp.rr:.1f})"
        for i, tp in enumerate(targets, start=1)
    )


def _market_entry_lines(
    result: AnalysisResult, instrument: Instrument, is_range: bool
) -> List[str]:
    """The 'enter at market' block of the 🚨 alert (D25): price now, the
    setup's stop, the risk, then TP1-3. A range setup keeps its single D14
    target and quotes the RR to it from the market price."""
    setup = result.setup
    d = result.price_decimals
    price = result.price or setup.entry
    is_long = setup.direction == Direction.LONG
    risk = price - setup.stop_loss if is_long else setup.stop_loss - price
    if risk <= 0:
        return [
            t("📈 Market entry         {price}"
              "   ✗ price is already beyond the stop — no market entry",
              price=f"{price:.{d}f}"),
        ]
    head = t(
        "📈 Enter at market      {price}   ← SL {sl} · risk {risk}",
        price=f"{price:.{d}f}", sl=f"{setup.stop_loss:.{d}f}",
        risk=escape_html(format_distance(risk, instrument)),
    )
    if is_range:
        if setup.take_profit is None:
            return [head, t("🎯 no positive reward to the opposite boundary")]
        reward = (
            setup.take_profit - price if is_long else price - setup.take_profit
        )
        cell = _rr_cell(reward / risk) if reward > 0 else "—"
        return [
            head,
            t("🎯 Range target         {tp}   ({rr})",
              tp=f"{setup.take_profit:.{d}f}", rr=cell),
        ]
    targets = take_profits(
        setup.ladder, setup.direction, price, setup.stop_loss,
        instrument.sl_buffer, tolerance=instrument.min_fvg,
    )
    return [head, _targets_line(targets, d)]


def took_skipped_keyboard(signal_id: str) -> dict:
    """The ✅/❌ buttons under a setup alert — the ONLY input discipline
    reads (CLAUDE.md: Rule 10 / Rule 0.2 count taken marks). One builder,
    used by the send and by every live-card edit, so the two cannot drift."""
    return {
        "inline_keyboard": [
            [
                {"text": t("✅ Took it"), "callback_data": f"take_{signal_id}"},
                {"text": t("❌ Skipped"), "callback_data": f"skip_{signal_id}"},
            ]
        ]
    }


_STANCES = {"agree": "agree", "caution": "caution", "against": "against"}


def format_ai_read(read) -> str:
    """The 🧠 block (D26): Claude's stance, confidence and preferred entry
    on one line, the read itself, then the risks. Appended to the 🚨 card
    and to the audit; every field is model output and goes through
    escape_html like any other dynamic string."""
    stance = t(_STANCES.get(read.stance, read.stance)).upper()
    head = (
        f"🧠 <b>{t('AI read')}</b> ({escape_html(read.model)}"
        + (f" · {escape_html(read.as_of)} {t('Prague')}" if read.as_of else "")
        + f"): {escape_html(stance)} · "
        + t("confidence {n}/5 · prefers {entry}", n=int(read.confidence),
            entry=escape_html(read.preferred_entry.upper()))
    )
    lines = [head, escape_html(read.read)]
    if read.risks:
        lines.append("⚠️ " + " · ".join(escape_html(r) for r in read.risks))
    return "\n".join(lines)


def _analysis_columns(
    analysis, instrument: Instrument, deposit=None, risk_pct: float = 2.0,
) -> List[str]:
    """The pending-entry table, one column per entry, inside <pre> so the
    numbers line up in Telegram's proportional font. Every cell is escaped
    (labels are built from Zone.kind strings and could in principle carry
    anything)."""
    d = instrument.price_decimals
    entries = list(analysis.entries)
    if not entries:
        return []

    def header(e) -> str:
        if e.role == "main":
            return "MAIN"
        if e.role == "deep":
            return "DEEP"
        return "LONG" if e.direction == Direction.LONG else "SHORT"

    depth = max((len(e.targets) for e in entries), default=0)
    rows = [("", [header(e) for e in entries])]
    rows.append((t("Where"), [e.label for e in entries]))
    if any(e.zone for e in entries):
        rows.append((t("Zone"), [
            f"{e.zone[0]:.{d}f}–{e.zone[1]:.{d}f}" if e.zone else "—"
            for e in entries
        ]))
    rows.append((t("Entry"), [f"{e.entry:.{d}f}" for e in entries]))
    rows.append(("SL", [f"{e.stop_loss:.{d}f}" for e in entries]))
    rows.append((t("Risk"), [format_distance(e.risk, instrument) for e in entries]))
    if deposit:
        # Rule 8 size per rung (2026-09-10): the deeper rung risks fewer
        # dollars per unit, so it carries a bigger position for the same
        # risk — the two cells make that trade-off visible.
        rows.append((t("Size"), [
            position_size(instrument, e.entry, e.risk, deposit, risk_pct, compact=True)
            or "—"
            for e in entries
        ]))
    for i in range(depth):
        rows.append((f"TP{i + 1}", [
            f"{e.targets[i].price:.{d}f}  1:{e.targets[i].rr:.1f}"
            if i < len(e.targets) else "—"
            for e in entries
        ]))
    if depth == 0:
        rows.append(("TP", [t("no unswept liquidity ahead") for _ in entries]))
    widths = [
        max(len(row[1][col]) for row in rows) for col in range(len(entries))
    ]
    label_width = max(6, max(len(label) for label, _ in rows))
    out = ["<pre>"]
    for label, cells in rows:
        padded = "   ".join(
            f"{escape_html(cell):<{widths[i]}}" for i, cell in enumerate(cells)
        )
        out.append(f"{escape_html(label):<{label_width}} {padded}".rstrip())
    out.append("</pre>")
    return out


def session_time_left(result: AnalysisResult):
    """(minutes left, end datetime UTC) of the session `result` was checked
    in, or None off session. A pending order placed now dies at that end
    (Rule 10) — the number the owner needs next to the pending table."""
    if not result.session_name:
        return None
    end = session_end_utc(result.checked_at)
    if end is None:
        return None
    minutes = int((end - result.checked_at).total_seconds() // 60)
    return max(minutes, 0), end


def format_duration(minutes: int) -> str:
    """'0h26' / '0ч26' — the hour marker is a word, so it is translated
    like any other string (2026-09-10: the Russian clock read '0h26')."""
    hh, mm = divmod(max(minutes, 0), 60)
    return t("{h}h{m}", h=hh, m=f"{mm:02d}")


def session_time_left_line(result: AnalysisResult) -> Optional[str]:
    left = session_time_left(result)
    if left is None:
        return None
    minutes, end = left
    return t(
        "⏱ {session} ends in {left} ({hhmm} Prague) — a pending order placed now "
        "expires then",
        session=escape_html(result.session_name), left=format_duration(minutes),
        hhmm=to_prague(end).strftime("%H:%M"),
    )


def format_setup_analysis(
    pair: str,
    result: AnalysisResult,
    analysis,
    instrument: Instrument,
    as_of: Optional[str] = None,
    ai_read=None,
    deposit=None,
    risk_pct: float = 2.0,
) -> str:
    """The Strategy audit the pair buttons under the 08:05/14:05 summary
    answer with (D25, owner decision 2026-09-05): the checklist state, the
    pending (limit) entries — MAIN and DEEP, each with entry / SL / TP1-3 /
    RR — and, once a setup has formed, the market reference the 🚨 alert
    quoted. Computed on schedule, delivered on demand.
    """
    d = instrument.price_decimals
    name = escape_html(pair)
    head = f"🔬 <b>{t('Strategy audit — {pair}', pair=name)}</b>"
    if analysis.direction is not None:
        head += f" · {'LONG' if analysis.direction == Direction.LONG else 'SHORT'}"
    if result.h1_trend is not None:
        head += (
            f" · H4 {trend_label(result.h4_trend)}"
            f" · H1 {trend_label(result.h1_trend)}"
        )
    lines = [head]
    if result.price:
        suffix = (
            " · " + t("M5 close {hhmm} Prague", hhmm=escape_html(as_of))
            if as_of else ""
        )
        lines.append(f"💵 {result.price:.{d}f}{suffix}")
        # PD is the most common star-blocker and the thing Claude keeps
        # naming in prose ("вход в 95% премиуме"): the card has printed it
        # since D17, the audit had not (2026-09-10).
        pd_line = _pd_line(result)
        if pd_line:
            lines.append(pd_line)
        main = analysis.main
        if main is not None and analysis.market is None:
            # How far the pullback still has to travel to the MAIN rung —
            # the number the owner reads first when deciding whether to
            # park a limit now or come back later (2026-09-10).
            gap = abs(result.price - main.entry)
            lines.append(t(
                "📏 To the MAIN entry {entry}: {distance} ({pct}%)",
                entry=f"{main.entry:.{d}f}",
                distance=escape_html(format_distance(gap, instrument)),
                pct=f"{gap / result.price * 100:.1f}",
            ))
    session_line = session_time_left_line(result)
    if session_line:
        lines.append(session_line)
    if result.market_range is not None:
        box = result.market_range
        lines.append(
            t("📦 Range box {lo}–{hi}", lo=f"{box.bottom:.{d}f}", hi=f"{box.top:.{d}f}")
        )
    market = analysis.market
    if market is not None:
        lines.append(
            t("🚨 <b>Setup formed</b> — market entry {entry} · SL {sl} · risk {risk}",
              entry=f"{market.entry:.{d}f}", sl=f"{market.stop_loss:.{d}f}",
              risk=escape_html(format_distance(market.risk, instrument)))
        )
        lines.append(_targets_line(market.targets, d))
        star = _tier_line(result)
        if star:
            lines.append(star)
        # The warnings the 🚨 card has always carried belong here too
        # (2026-09-10): the audit is the screen the owner plans from, and
        # "Setup formed" with a 2.5R-stale entry paying 1:0.1 read as a go
        # while only Claude's prose mentioned the problem.
        lines.extend(_warning_lines(result))
    elif result.reasons:
        icon = "👀" if result.verdict == Verdict.WATCH else "⛔"
        prefix = "" if result.session_name else t("(off session) ")
        lines.append(f"{icon} {prefix}{escape_html(result.reasons[0])}")
    lines.append("")
    if analysis.entries:
        lines.append(f"<b>{t('Pending (limit) entries')}</b>")
        lines.extend(_analysis_columns(analysis, instrument, deposit, risk_pct))
        if analysis.range_mode:
            lines.append(
                t("🎯 one target each — the opposite boundary, full size (D14)")
            )
        lines.append(t(
            "⚠️ A pending order lives only within its session (Rule 10); once "
            "the setup forms, the 🚨 alert re-anchors the SL to the swept "
            "extreme (Rule 6)."
        ))
    else:
        lines.append(
            t("→ No pending entry to place: {note}",
              note=escape_html(analysis.note or t("nothing to wait at")))
        )
    if ai_read is not None:
        lines.append("")
        lines.append(format_ai_read(ai_read))
    return "\n".join(lines)


def _tier_line(result: AnalysisResult) -> Optional[str]:
    """The ⭐ verdict of a formed setup — earned, or what it missed. Same
    two lines the 🚨 card prints; the audit had neither (2026-09-10), so
    "Setup formed" gave no hint that sweep and PD had failed."""
    setup = result.setup
    if setup is None:
        return None
    if setup.tier_star:
        return "⭐ <b>SNIPER</b>"
    if setup.tier_missed:
        return t("🔹 Missed for ⭐: {missed}",
                 missed=escape_html(missed_label(setup.tier_missed)))
    return None


def _warning_lines(result: AnalysisResult) -> List[str]:
    """The engine's ⚠️ labels (Rules 5.1 / 7, demoted to warnings by
    detector mode) plus funding — shared by the 🚨 card and the audit so
    the two can never disagree about what is wrong with a setup."""
    out = [f"⚠️ {escape_html(w)}" for w in result.warnings]
    if result.funding_warning:
        out.append(f"⚠️ {escape_html(result.funding_warning)}")
    return out


def _plan_line(plan, d: int) -> str:
    """The 📋 line (owner decision 2026-09-10, the plan is primary): does
    this setup continue the plan the owner pressed, and what did Claude
    say then. A mismatch is stated, never acted on — detector mode."""
    side = plan.direction.upper() if plan.direction else "—"
    if not plan.matches:
        zone = (
            f"{min(plan.zones[0][0], plan.zones[0][1]):.{d}f}–"
            f"{max(plan.zones[0][0], plan.zones[0][1]):.{d}f}"
            if plan.zones else t("no zone")
        )
        return t("📋 Not the {when} plan ({side} {zone} there)",
                 when=escape_html(plan.when), side=side, zone=zone)
    parts = [t("📋 Per the {when} plan: {side}", when=escape_html(plan.when), side=side)]
    if plan.main is not None:
        parts.append(f"MAIN {plan.main:.{d}f}")
    if plan.deep is not None:
        parts.append(f"DEEP {plan.deep:.{d}f}")
    if plan.ai_stance:
        stance = t(_STANCES.get(plan.ai_stance, plan.ai_stance)).upper()
        claude = f"Claude: {escape_html(stance)}"
        if plan.ai_confidence is not None:
            claude += f" {plan.ai_confidence}/5"
        if plan.ai_entry:
            claude += ", " + t("preferred {entry}", entry=escape_html(plan.ai_entry.upper()))
        parts.append(claude)
    return " · ".join(parts)


def _format_detector_alert(
    result: AnalysisResult, in_plan: Optional[bool], plan=None
) -> str:
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
        f"🚨 <b>{t('SETUP READY — {pair} · {side}', pair=escape_html(result.symbol), side=side)}</b>"
        f" · {_direction_source_label(result, is_long)}"
    ]
    if result.h1_trend is not None:
        # `h4_trend` defaults to Trend.FLAT and is never None, so only the
        # H1 half of this guard ever decided anything: h1_trend is None when
        # Rule 1 returned before computing the trends at all.
        # D6 (owner decision 2026-08-16): H4/H1 trend agreement, always
        # shown — the counter-hourly marker only appears when they actually
        # disagree, which is also what denies the ⭐ below. This never
        # suppresses; it only labels.
        agree = f"H4 {result.h4_trend.value} · H1 {result.h1_trend.value}"
        if trends_disagree(result.h4_trend, result.h1_trend):
            agree += t(" ⚠️ counter-hourly")
        lines.append(agree)
    pd_line = _pd_line(result)
    if pd_line:
        lines.append(pd_line)
    if setup.tier_star:
        # Phase 2 sniper redesign (owner decision 2026-08-12): the loud
        # ⭐-tier header — room + sweep + premium/discount + staleness all
        # cleared (app/services/smc/sniper.py). Detector mode: every
        # completed setup is still announced, this line only labels the
        # higher-confidence ones.
        lines.append("⭐ <b>SNIPER</b>")
    elif setup.tier_missed:
        # D25 (owner decision 2026-09-05): both tiers get this full card, so
        # the "what the star wanted" line the quiet one-liner used to carry
        # moves here — the most common blocker (pd) has its number on the PD
        # line just above.
        lines.append(
            t("🔹 Missed for ⭐: {missed}", missed=escape_html(missed_label(setup.tier_missed)))
        )
    if plan is not None:
        # the primary plan (2026-09-10) says more than the provenance flag
        lines.append(_plan_line(plan, d))
    elif in_plan is True:
        lines.append(t("   from this morning's plan"))
    elif in_plan is False:
        lines.append(t("   new zone — not in the plan"))
    lines.append("")

    # --- the four actionable lines, in the order the owner works them
    #
    # A range setup is announced as a range trade (models.AnalysisResult:
    # anything RENDERING one keys on `direction_source`, never on
    # `market_range` alone): the box he is trading between comes first, and
    # the band he entered at is a boundary, not an H1 Supply/Demand zone.
    is_range = result.direction_source == "range" and result.market_range is not None
    if is_range:
        box = result.market_range
        lines.append(
            t("📦 Range box            {lo} – {hi}",
              lo=f"{box.bottom:.{d}f}", hi=f"{box.top:.{d}f}")
        )
    if result.h1_zone:
        zlo, zhi = f"{result.h1_zone.bottom:.{d}f}", f"{result.h1_zone.top:.{d}f}"
        if is_range:
            lines.append(
                t("📍 Range LOW boundary  {lo} – {hi}", lo=zlo, hi=zhi)
                if result.h1_zone.is_demand
                else t("📍 Range HIGH boundary  {lo} – {hi}", lo=zlo, hi=zhi)
            )
        else:
            lines.append(
                t("📍 H1 {kind} zone ({zk})  {lo} – {hi}",
                  kind=side_label(result.h1_zone.is_demand),
                  zk=result.h1_zone.kind, lo=zlo, hi=zhi)
            )
    # D22: the entry line names the rung it came from, and the imbalance gets
    # a line of its own whenever it did not supply the entry — present but
    # rejected (with the flaw named), or absent altogether. The owner asked
    # for the gap to stay visible without being decisive.
    if setup.fvg is not None:
        lines.append(
            t("⚡ M5 imbalance (FVG)   {lo} – {hi}   ← limit order ({entry})",
              lo=f"{setup.fvg.bottom:.{d}f}", hi=f"{setup.fvg.top:.{d}f}",
              entry=f"{setup.entry:.{d}f}")
        )
    else:
        if setup.entry_source == "ob" and setup.order_block is None:
            # The block IS the entry on this rung, so the engine leaves
            # `order_block` empty (nothing deeper to advertise) and the band
            # is not repeated — the entry price says it.
            lines.append(
                t("🧱 M5 order block       {entry}   ← limit order (no imbalance)",
                  entry=f"{setup.entry:.{d}f}")
            )
        else:
            lines.append(
                t("📈 Market entry         {entry}   ← at the CHoCH (no imbalance)",
                  entry=f"{setup.entry:.{d}f}")
            )
        if setup.rejected_fvg is not None:
            gap = setup.rejected_fvg
            lines.append(
                t("⚡ M5 imbalance         {lo} – {hi}   ✗ {flaw}",
                  lo=f"{gap.bottom:.{d}f}", hi=f"{gap.top:.{d}f}",
                  flaw=escape_html(_imbalance_flaw(setup.rejected_fvg_problems)))
            )
        else:
            lines.append(
                t("⚡ M5 imbalance         none — the impulse left no gap")
            )
    if setup.order_block:
        ob_entry = setup.order_block.top if is_long else setup.order_block.bottom
        lines.append(
            t("🧱 M5 order block       {lo} – {hi}   ← deeper entry ({entry})",
              lo=f"{setup.order_block.bottom:.{d}f}",
              hi=f"{setup.order_block.top:.{d}f}", entry=f"{ob_entry:.{d}f}")
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
    if is_range:
        # In range mode Rule 6 takes the FURTHER of the swept wick and the
        # boundary itself (engine.py), so this level is often the boundary
        # and nothing was swept at it — "Swept liquidity" would assert
        # something false. Name what it actually is: the level the stop sits
        # beyond (review 2026-08-18).
        lines.append(
            t("🛑 Stop reference       {extreme}   ← stop beyond it ({sl} with buffer)",
              extreme=f"{extreme:.{d}f}", sl=f"{setup.stop_loss:.{d}f}")
        )
    else:
        lines.append(
            t("🛑 Swept liquidity      {extreme}   ← stop behind the wick ({sl} with buffer)",
              extreme=f"{extreme:.{d}f}", sl=f"{setup.stop_loss:.{d}f}")
        )
    if is_range and setup.take_profit is not None:
        # D14 (owner decision 2026-08-18): one target, the opposite
        # boundary, full size. It is the whole thesis of the trade, so it is
        # an actionable line here rather than a "ref ·" footnote — the ref
        # objective line below only fires for a liquidity `target`, which a
        # range setup deliberately has none of.
        # Padded to the same column as the 📦/📍/⚡/🛑 lines above, which are
        # hand-aligned one emoji = one cell — "HIGH" is a character wider
        # than "LOW", so the label is justified rather than fixed-spaced.
        label = t("🎯 Range HIGH target") if is_long else t("🎯 Range LOW target")
        lines.append(
            f"{label:<19}    {setup.take_profit:.{d}f}"
            + t("   ← full size, 1:{rr}", rr=f"{setup.rr:.1f}")
        )
    # D25 (owner decision 2026-09-05): the notification means "the setup has
    # formed — enter at market". The market price, the Rule 6 stop and TP1-3
    # (the three nearest unswept pools off the ladder below, RR from the
    # market price) are the lines the owner acts on; the structural lines
    # above say where the setup came from. The Phase 2 hybrid exit (TP1 at
    # 2R / runner at 3R) is still computed and still drives the journal —
    # it just no longer prints here; the pending (limit) alternatives live
    # behind the Setup-analysis button instead.
    lines.append("")
    lines.extend(_market_entry_lines(result, instrument, is_range))

    lines.append("")
    lines.extend(_ladder_lines(setup, instrument))
    if setup.zones_ahead:
        lines.append("")
        lines.extend(_zone_lines(setup, d))

    # --- warnings: impossible to miss, but they do not push the levels down
    notes = []
    if setup.entry_is_market:
        # D22: name the band price is actually inside (or say it is a plain
        # market entry, which has no band at all).
        inside = {
            "fvg": "   ▶️ price is inside the imbalance right now",
            "ob": "   ▶️ price is inside the order block right now",
        }.get(setup.entry_source, "   ▶️ market entry — price is at the CHoCH")
        notes.append(t(inside))
    for warning in result.warnings:
        notes.append(f"   ⚠️ {escape_html(warning)}")
    if result.funding_warning:
        notes.append(f"   ⚠️ {escape_html(result.funding_warning)}")
    if notes:
        lines.append("")
        lines.extend(notes)

    # --- ref: measured context, not instruction
    lines.append("")
    if setup.fvg is not None:
        fvg_ref = t(
            "   ref · FVG {size}, {pct}% filled",
            size=f"{setup.fvg.size:.{d}f}", pct=f"{setup.fvg.fill_pct * 100:.0f}",
        )
    elif setup.rejected_fvg is not None:
        # D22: still measured, still shown — it just does not count.
        flaw = escape_html(_imbalance_flaw(setup.rejected_fvg_problems))
        fvg_ref = t(
            "   ref · FVG {size}, {pct}% filled — {flaw}",
            size=f"{setup.rejected_fvg.size:.{d}f}",
            pct=f"{setup.rejected_fvg.fill_pct * 100:.0f}", flaw=flaw,
        )
    else:
        fvg_ref = t("   ref · no M5 imbalance in the impulse")
    if result.session_name:
        fvg_ref += f" · {escape_html(result.session_name)}"
    lines.append(fvg_ref)
    if setup.take_profit is not None and setup.target is not None:
        lines.append(
            t("   ref · tracked objective {tp} (1:{rr}) · {target}",
              tp=f"{setup.take_profit:.{d}f}", rr=f"{setup.rr:.1f}",
              target=escape_html(format_target(setup.target, d)))
        )
    if setup.lot_hint:
        lines.append(t("   ref · size {size}", size=escape_html(setup.lot_hint)))
    if result.funding_rate is not None and not result.funding_warning:
        # Rule 9.3: a benign funding reading is still measured context. The
        # actionable brackets already left as ⚠️ lines above.
        lines.append(t("   ref · funding {rate}%/8h", rate=f"{result.funding_rate * 100:.3f}"))
    if getattr(result, "profile_key", "conservative") == "aggressive":
        lines.append(t("   ref · aggressive profile — first-leg entry"))
    lines.append(t("   ref · a pending order expires with this session (Rule 10)"))
    lines.append(
        t("   ref · {when} Prague", when=to_prague(result.checked_at).strftime('%d.%m %H:%M'))
        + (t(" · price {price}", price=f"{result.price:.{d}f}") if result.price else "")
    )
    return "\n".join(lines)


def missed_label(codes) -> str:
    """'room, sweep' in the bot's language — the ⭐ conditions a setup missed."""
    return ", ".join(tier_label(c) for c in codes)


def format_no_setup(result: AnalysisResult) -> str:
    """Compact heartbeat when there is no setup."""
    time_str = to_prague(result.checked_at).strftime("%H:%M")
    if result.verdict == Verdict.OFF_SESSION:
        return t(
            "😴 {pair} {hhmm} — off session, entries are not allowed. "
            "Will check again on schedule.", pair=result.symbol, hhmm=time_str,
        )
    reason = escape_html(result.reasons[0] if result.reasons else t("conditions not met"))
    return t("🔍 {pair} {hhmm} — no setup. {reason}.",
             pair=result.symbol, hhmm=time_str, reason=reason)


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

    Most range setups land here rather than on the ⭐ card (the star needs a
    sweep), so this line must carry the box and its target: a range trade
    with no target and no bounds tells the owner to trade between two levels
    without naming either (review 2026-08-18). It has no hybrid exit to show
    in the first place (D14).
    """
    setup = result.setup
    d = result.price_decimals
    is_long = setup.direction == Direction.LONG
    side = "LONG" if is_long else "SHORT"
    missed = missed_label(setup.tier_missed) if setup.tier_missed else "—"
    time_str = to_prague(result.checked_at).strftime("%d.%m %H:%M")
    # "Missed for ⭐: pd" is the most common verdict on this line and the
    # least actionable one without a number behind it (owner decision D17).
    pd_read = result.pd
    pd_note = (
        f" · PD {pd_read.pct}% {escape_html(t(pd_read.label))}"
        f" ({escape_html(pd_read.range.timeframe)})"
        if pd_read is not None else ""
    )
    tail = t("Missed for ⭐: {missed}{pd} · {when} Prague",
             missed=escape_html(missed), pd=pd_note, when=time_str)
    if result.direction_source == "range" and result.market_range is not None:
        box = result.market_range
        target = (
            f"TP {setup.take_profit:.{d}f} (1:{setup.rr:.1f})"
            if setup.take_profit is not None
            else "TP n/a"
        )
        return (
            t("🔹 <b>{pair} {side}</b> · range {lo}–{hi} · entry {entry} · SL {sl} · {target}",
              pair=escape_html(result.symbol), side=side,
              lo=f"{box.bottom:.{d}f}", hi=f"{box.top:.{d}f}",
              entry=f"{setup.entry:.{d}f}", sl=f"{setup.stop_loss:.{d}f}", target=target)
            + "\n" + tail
        )
    tp1 = f"{setup.tp1:.{d}f}" if setup.tp1 is not None else "n/a"
    runner = f"{setup.runner_tp:.{d}f}" if setup.runner_tp is not None else "n/a"
    return (
        t("🔹 <b>{pair} {side}</b> · entry {entry} · SL {sl} · TP1 {tp1} · runner {runner}",
          pair=escape_html(result.symbol), side=side, entry=f"{setup.entry:.{d}f}",
          sl=f"{setup.stop_loss:.{d}f}", tp1=tp1, runner=runner)
        + "\n" + tail
    )


def format_setup_still_active(result: AnalysisResult) -> str:
    """Short reminder when the previously reported setup is still valid."""
    time_str = to_prague(result.checked_at).strftime("%H:%M")
    return t(
        "⏳ {pair} {hhmm} — the setup reported earlier is still active. Nothing new.",
        pair=result.symbol, hhmm=time_str,
    )


def format_result(
    result: AnalysisResult, in_plan: Optional[bool] = None, plan=None
) -> str:
    """Render an AnalysisResult as an HTML Telegram message.

    `in_plan` is the plan provenance of the announced zone: True renders "from
    this morning's plan", False "new zone — not in the plan", and None omits
    the line entirely — no `/plan` ran today, so the bot does not claim a
    provenance it cannot know. `plan` (a `planbook.PlanMatch`, owner decision
    2026-09-10) supersedes it: the card says whether this setup is the one
    the owner's last /plan projected and what Claude said then.
    """
    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
        if result.setup is not None:
            return _format_detector_alert(result, in_plan, plan)
        logger.error("Approved result without a setup", symbol=result.symbol)
    lines = []
    lines.append(f"<b>{escape_html(result.symbol)}</b> — Triple Sync + Imbalance")
    if getattr(result, "profile_key", "conservative") == "aggressive":
        lines.append(t("⚡ <b>Aggressive profile</b> — first-leg entry, lower-probability"))
    lines.append(
        f"🕐 {to_prague(result.checked_at).strftime('%d.%m.%Y %H:%M')} {t('Prague')}"
        + (f" | {t('Session')}: {result.session_name}" if result.session_name else "")
    )
    d = result.price_decimals
    if result.price:
        lines.append(f"💵 {t('Price')}: {result.price:.{d}f}")
    lines.append("")
    lines.append(f"<b>{t('H4 bias')}:</b> {trend_label(result.h4_trend)}")

    if result.market_range is not None:
        # Drawing the boundaries only — the one thing that may key on
        # `market_range` rather than on `direction_source` (models.py).
        box = result.market_range
        lines.append(
            f"<b>{t('Range box')}:</b> {box.bottom:.{d}f}–{box.top:.{d}f}"
        )
    if result.h1_zone:
        if result.h1_zone.kind == "RANGE":
            # A boundary is not an H1 Demand/Supply zone and must not be
            # described as one (review 2026-08-18). The box itself is on the
            # line above only when a range is live, which it always is here.
            edge = t("Range LOW boundary") if result.h1_zone.is_demand else t("Range HIGH boundary")
            lines.append(
                f"<b>{edge}:</b> "
                f"{result.h1_zone.bottom:.{d}f}–{result.h1_zone.top:.{d}f}"
            )
        else:
            zone_kind = side_label(result.h1_zone.is_demand)
            # The kind (OB / FVG) belongs here more than anywhere: this is
            # the screen the owner reads WHILE waiting for price to arrive,
            # which is the whole life of an imbalance zone — the loud alert
            # may never come.
            lines.append(
                f"<b>{t('H1 zone')} ({zone_kind} · {result.h1_zone.kind}):</b> "
                f"{result.h1_zone.bottom:.{d}f}–{result.h1_zone.top:.{d}f}"
            )

    if result.verdict == Verdict.WATCH:
        lines.append("")
        lines.append(f"<b>{t('No setup yet (Setup Watch)')}:</b>")
        for reason in result.reasons:
            lines.append(f"• {escape_html(reason)}")
        if result.watch_notes:
            lines.append("")
            lines.append(f"<b>{t('What is needed for an entry')}:</b>")
            for note in result.watch_notes:
                lines.append(f"→ {escape_html(note)}")
    else:
        lines.append("")
        lines.append(f"<b>{t('Verdict')}:</b> ❌ SKIP")
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
    lines = [
        f"📋 <b>{plan.pair}</b> — "
        + t("Pre-Market Plan (H4 {trend})", trend=trend_label(plan.h4_trend))
    ]
    if plan.price:
        suffix = "  ·  " + t("M5 close {hhmm} Prague", hhmm=as_of) if as_of else ""
        lines.append(f"💵 {plan.price:.{d}f}{suffix}")
    if live_line:
        lines.append(f"📍 <b>{t('Live now')}:</b> {live_line}")
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
        side = t("Buy") if is_long else t("Sell")
        head = (
            f"{arrow} <b>{'LONG' if is_long else 'SHORT'}</b>"
            + (t(" (speculative)") if s.speculative else t(" plan"))
        )
        lines.append("")
        lines.append(head)
        if s.kind == "RANGE":
            # A boundary band, not an H1 Demand/Supply zone — the plan says
            # which edge of the box it is (review 2026-08-18).
            lines.append(
                "   " + (t("Range LOW boundary") if is_long else t("Range HIGH boundary"))
                + f" {s.zone_bottom:.{d}f}–{s.zone_top:.{d}f}"
            )
        else:
            lines.append(
                f"   {t('Zone')} {side_label(is_long)} "
                f"{s.zone_bottom:.{d}f}–{s.zone_top:.{d}f}"
            )
        lines.append(
            f"   {side} Limit {s.entry:.{d}f} | 🛑 SL {s.stop_loss:.{d}f} "
            f"| 🎯 TP {s.take_profit:.{d}f}"
        )
        lines.append(t("   📐 RR ~1:{rr} (approx)", rr=f"{s.rr:.1f}"))
        lines.append(
            t("   Trigger: M5 {bias} CHoCH + FVG inside the zone", bias=bias_label(is_long))
        )
        if s.kind == "RANGE" and s.swept:
            # Range.swept_top/swept_bottom (D9/D16): this boundary was
            # pierced and reclaimed at some point — liquidity already taken
            # there, so the pool behind it may be thinner than a boundary
            # that has never been raided. D16 widened the pierce from
            # wick-only to any pierce price came back inside from, and the
            # sentence holds either way: it claims the level was raided,
            # never that the raid was shallow. A deeper raid took MORE of
            # the pool, which is the same warning only more so.
            lines.append(t(
                "   ⚠️ this boundary has already been swept once — "
                "liquidity may be thinner here"
            ))

    lines.append("")
    # A range plan holds RANGE scenarios only (D12: they replace the
    # speculative brackets, they never join them), so the footer names the
    # boundary the preliminary stop sits beyond instead of an H1 zone the
    # message never mentioned.
    anchor = (
        t("range boundary")
        if plan.scenarios and all(s.kind == "RANGE" for s in plan.scenarios)
        else t("H1 zone")
    )
    lines.append(t(
        "⚠️ SL is preliminary (beyond the {anchor}); the live 🚨 alert "
        "re-anchors it to the swept extreme and it may be wider. Order "
        "lives only within its session.", anchor=anchor,
    ))
    return "\n".join(lines)


def format_plan_summary(slot_hhmm, plans, updated_hhmm=None) -> str:
    """One-line-per-pair digest of the auto-built Pre-Market Plans.

    Silent by design (the send uses disable_notification): the owner sees
    that plans exist without being pushed their content — the buttons under
    this message deliver the full plan on demand (spec 2026-08-11 §2).
    """
    title = (
        f"📋 <b>{t('Pre-Market Plan')} {escape_html(slot_hhmm)}</b> "
        + t("— press a pair for its strategy audit (pending entries)")
    )
    if updated_hhmm:
        title += " · " + t("upd {hhmm}", hhmm=escape_html(updated_hhmm))
    lines = [title]
    for plan in plans:
        d = plan.price_decimals
        name = escape_html(plan.pair)
        if plan.market_closed:
            lines.append(f"{name} 😴 {t('market closed')}")
            continue
        if not plan.scenarios:
            reason = plan.blocker or plan.note or t("no plan")
            lines.append(f"{name} ⛔ {t('waiting')}: {escape_html(reason)}")
            continue
        for s in plan.scenarios:
            is_long = s.direction == Direction.LONG
            arrow = "🔼" if is_long else "🔽"
            spec = t(" (speculative)") if s.speculative else ""
            lines.append(
                f"{name} {arrow} {'LONG' if is_long else 'SHORT'} {t('zone')} "
                f"{s.zone_bottom:.{d}f}–{s.zone_top:.{d}f} (~1:{s.rr:.1f}){spec}"
            )
    return "\n".join(lines)


def plan_summary_keyboard(pairs) -> dict:
    """aplan_* buttons under the summary: two pairs per row, then All. Since
    D25 (2026-09-05) a press answers with the Strategy audit (pending
    entries, computed at the snapshot and refreshed every cycle), not the
    stored plan text."""
    rows, row = [], []
    for key in pairs:
        row.append({"text": key, "callback_data": f"aplan_{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": t("🌐 All pairs"), "callback_data": "aplan_ALL"}])
    return {"inline_keyboard": rows}


def format_zone_alert(pair, scenario, decimals: int, marks=None) -> str:
    """Price reached a plan zone: the get-ready moment, with the plan's own
    projected bracket so the owner sees the scenario without pressing
    anything (spec 2026-08-11 §5). SL here is the plan's preliminary one —
    the live 🚨 alert re-anchors it (Rule 6).

    A RANGE scenario (Task 3, spec §3.3) is not an H1 Demand/Supply zone, so
    it is not described as one — the header names the boundary itself
    ("price is at the range HIGH/LOW") and the plan line targets the
    OPPOSITE boundary rather than a projected TP off a zone. Everything
    else — the shape of the message, the 🔕 mute keyboard, the marks line —
    is the same for every scenario kind; only these two lines branch.

    `marks` is the optional `(order_block, fvg)` pair from `structure.m5_marks`
    (owner decision D5, spec §2.2): the M5 order block and imbalance inside
    the zone, i.e. what the owner would actually buy from. It rides along as
    one extra line on this same message, never a message of its own — with
    no marks the text is byte-identical to before this parameter existed.
    """
    d = decimals
    is_long = scenario.direction == Direction.LONG
    spec = t(" (speculative)") if scenario.speculative else ""
    watching = t("Watching M5 for a {bias} CHoCH + FVG.", bias=bias_label(is_long))
    if scenario.kind == "RANGE":
        this_side = "LOW" if is_long else "HIGH"
        far_side = "HIGH" if is_long else "LOW"
        lines = [
            t("🔔 <b>{pair}</b>: price is at the range {edge} {price}",
              pair=escape_html(pair), edge=this_side, price=f"{scenario.entry:.{d}f}"),
            t("📋 Plan: {side} — target the range {edge} {tp} | 🛑 SL {sl} | ~1:{rr}{spec}",
              side="LONG" if is_long else "SHORT", edge=far_side,
              tp=f"{scenario.take_profit:.{d}f}", sl=f"{scenario.stop_loss:.{d}f}",
              rr=f"{scenario.rr:.1f}", spec=spec),
            watching,
        ]
    else:
        side = t("Buy") if is_long else t("Sell")
        lines = [
            t("🔔 <b>{pair}</b>: price reached the {kind} zone {lo}–{hi}",
              pair=escape_html(pair), kind=side_label(is_long),
              lo=f"{scenario.zone_bottom:.{d}f}", hi=f"{scenario.zone_top:.{d}f}"),
            t("📋 Plan: {side} — {order} Limit {entry} | 🛑 SL {sl} | 🎯 TP {tp} | ~1:{rr}{spec}",
              side="LONG" if is_long else "SHORT", order=side,
              entry=f"{scenario.entry:.{d}f}", sl=f"{scenario.stop_loss:.{d}f}",
              tp=f"{scenario.take_profit:.{d}f}", rr=f"{scenario.rr:.1f}", spec=spec),
            watching,
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
        "text": t("🔕 Mute {pair} zone alerts till {hhmm}", pair=pair, hhmm=until_hhmm),
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
