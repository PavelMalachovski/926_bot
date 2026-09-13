"""Every message the Opus bot sends, through `i18n.t` and `escape_html`.

The card is the model's decision as the owner reads it: what to do
(limit / market / wait / no trade), the levels with the risk and the RR,
the invalidation, the validity, the read, the reasons, the risks, and —
when the code downgraded an order — what the model wanted and which hard
limit it broke. Everything dynamic is escaped; only <b> and <pre> are
used, like the watcher.
"""

from datetime import datetime, timezone
from typing import List, Optional, Sequence

from app.services.opus.plan import OPEN, SL, TP, OpusPlan, Trade, cancel_side
from app.services.smc.i18n import t
from app.services.smc.instruments import Instrument
from app.services.smc.news import NewsEvent
from app.services.smc.notifier import escape_html, format_distance
from app.services.smc.sessions import to_prague

_ACTION_LABELS = {
    "limit": "LIMIT",
    "market": "MARKET",
    "wait": "WAIT",
    "no_trade": "NO TRADE",
}
_STATUS_LABELS = {
    "pending": "limit pending",
    "watching": "watching the zone",
    "idle": "no trade",
    "filled": "limit filled",
    "entered": "entered at market",
    "expired": "expired",
    "replaced": "replaced",
}
_EVENT_LABELS = {
    "zone_reached": "price reached the watch zone",
    "invalidated": "M5 closed beyond the invalidation",
    "session_opened": "a new session opened",
}


def _side(plan: OpusPlan) -> str:
    return "LONG" if plan.is_long else "SHORT"


def _f(value: Optional[float], d: int) -> str:
    return "—" if value is None else f"{value:.{d}f}"


def _rr(value: Optional[float]) -> str:
    return "—" if value is None else f"1:{value:.1f}"


def _validity_line(plan: OpusPlan) -> Optional[str]:
    if not plan.valid_until:
        return None
    try:
        until = datetime.fromisoformat(plan.valid_until)
    except ValueError:
        return None
    what = t("block end") if plan.valid_for == "session" else t("day end")
    return t(
        "⏳ Valid until {hhmm} Prague ({what})",
        hhmm=to_prague(until).strftime("%H:%M"), what=what,
    )


def _levels(plan: OpusPlan, instrument: Instrument, order: bool = True) -> List[str]:
    """The level lines. `order=False` (a WAIT / NO TRADE headline) keeps
    only the cancel level and the validity — the stop and the targets of
    a rejected idea belong to the `⚠️ Opus wanted` block, not here."""
    d = instrument.price_decimals
    lines = []
    if order and plan.stop_loss is not None and plan.entry is not None:
        lines.append(
            t("🛑 SL {sl} (risk {risk})", sl=_f(plan.stop_loss, d),
              risk=escape_html(format_distance(plan.risk, instrument)))
        )
    if order and plan.tp1 is not None:
        tp = t("🎯 TP1 {tp1} (RR {rr1})", tp1=_f(plan.tp1, d), rr1=_rr(plan.rr(plan.tp1)))
        if plan.tp2 is not None:
            tp += t(" · TP2 {tp2} ({rr2})", tp2=_f(plan.tp2, d), rr2=_rr(plan.rr(plan.tp2)))
        lines.append(tp)
    side = cancel_side(plan)
    if side is not None:
        lines.append(
            t("❌ Cancel if M5 closes {beyond} {level}",
              beyond=t("above") if side == "above" else t("below"),
              level=_f(plan.invalidation, d))
        )
    validity = _validity_line(plan)
    if validity and plan.action in ("limit", "wait", "no_trade"):
        lines.append(validity)
    return lines


def _zone_tail(plan: OpusPlan, d: int) -> str:
    if not plan.has_watch_zone:
        return ""
    return t(
        " — watch zone {lo}–{hi}; Opus is asked again when price gets there",
        lo=_f(plan.watch_low, d), hi=_f(plan.watch_high, d),
    )


def format_plan_card(
    plan: OpusPlan, instrument: Instrument, news_warning: Optional[str] = None,
) -> str:
    d = instrument.price_decimals
    lines = [
        t("🧠 <b>Opus · {pair}</b> · {as_of} Prague · price {price}",
          pair=plan.pair, as_of=escape_html(plan.as_of), price=_f(plan.price, d)),
        t("📊 Bias: {bias} · confidence {n}/5",
          bias=escape_html(plan.bias.upper()), n=plan.confidence),
    ]
    if plan.trigger != "plan":
        lines.append(
            t("🔁 Re-read after: {event}",
              event=t(_EVENT_LABELS.get(plan.trigger, plan.trigger)))
        )
    lines.append("")
    if plan.action == "limit":
        lines.append(t("📍 <b>LIMIT {side} @ {entry}</b>", side=_side(plan), entry=_f(plan.entry, d)))
        lines.extend(_levels(plan, instrument))
    elif plan.action == "market":
        lines.append(
            t("📈 <b>ENTER AT MARKET {side} @ {entry}</b>", side=_side(plan), entry=_f(plan.entry, d))
        )
        lines.extend(_levels(plan, instrument))
    else:
        head = "⏸ <b>WAIT</b>" if plan.action == "wait" else "🚫 <b>NO TRADE</b>"
        lines.append(t(head) + _zone_tail(plan, d))
        lines.extend(_levels(plan, instrument, order=False))
    if plan.downgraded_from:
        lines.append("")
        lines.append(
            t("⚠️ Opus wanted {action} {side} @ {entry} — rejected by the hard limits:",
              action=_ACTION_LABELS.get(plan.downgraded_from, plan.downgraded_from),
              side=_side(plan) if plan.direction != "none" else "",
              entry=_f(plan.entry, d))
        )
        lines.extend(f"   • {escape_html(n)}" for n in plan.notes)
    if plan.read:
        lines.append("")
        lines.append(escape_html(plan.read))
    if plan.reasons:
        lines.append("")
        lines.append(t("<b>Why:</b>"))
        lines.extend(f"• {escape_html(r)}" for r in plan.reasons)
    if plan.risks:
        lines.append("")
        lines.append(t("<b>Risks:</b>"))
        lines.extend(f"• {escape_html(r)}" for r in plan.risks)
    if news_warning:
        lines.append("")
        lines.append(news_warning)
    return "\n".join(lines)


def news_warning_line(
    events: Sequence[NewsEvent], before, after, plan: OpusPlan,
) -> Optional[str]:
    """The first red release inside the plan's validity: an order that is
    still working then has to be pulled."""
    if plan.action not in ("limit", "wait"):
        return None
    try:
        created = datetime.fromisoformat(plan.created_at)
        until = datetime.fromisoformat(plan.valid_until) if plan.valid_until else None
    except ValueError:
        return None
    for e in events:
        if e.time <= created:
            continue
        if until is not None and e.time - before > until:
            continue
        return t(
            "📰 {hhmm} {currency} {title} — no entries {start}–{end}; pull the "
            "limit before if it has not filled",
            hhmm=e.prague_hhmm(), currency=escape_html(e.currency),
            title=escape_html(e.title),
            start=to_prague(e.time - before).strftime("%H:%M"),
            end=to_prague(e.time + after).strftime("%H:%M"),
        )
    return None


# ------------------------------------------------------------------ events


def format_expired(plan: OpusPlan) -> str:
    hhmm = to_prague(datetime.fromisoformat(plan.resolved_at)).strftime("%H:%M") \
        if plan.resolved_at else "—"
    if plan.action == "limit":
        return t(
            "⏳ <b>{pair}</b>: the limit plan expired at {hhmm} Prague — pull the "
            "order if it is still in the terminal. /plan for a fresh read.",
            pair=plan.pair, hhmm=hhmm,
        )
    return t(
        "⏳ <b>{pair}</b>: the watch plan expired at {hhmm} Prague. /plan for a "
        "fresh read.",
        pair=plan.pair, hhmm=hhmm,
    )


def format_event_without_read(
    plan: OpusPlan, event: str, price: float, instrument: Instrument, reason: str,
) -> str:
    """The event happened but Opus is not asked (budget, cooldown, no key):
    say what happened and what to do, in one message."""
    d = instrument.price_decimals
    if event == "zone_reached":
        head = t(
            "👀 <b>{pair}</b>: price reached the watch zone {lo}–{hi} ({price}).",
            pair=plan.pair, lo=_f(plan.watch_low, d), hi=_f(plan.watch_high, d),
            price=_f(price, d),
        )
    elif event == "invalidated":
        head = t(
            "❌ <b>{pair}</b>: M5 closed beyond the invalidation {level} ({close}) — "
            "pull the limit if you placed one.",
            pair=plan.pair, level=_f(plan.invalidation, d), close=_f(price, d),
        )
    else:
        head = t("🔔 <b>{pair}</b>: {event}.", pair=plan.pair,
                 event=t(_EVENT_LABELS.get(event, event)))
    return head + " " + t("{reason} — press /plan to ask Opus.", reason=reason)


def budget_reason(calls: int, cap: int) -> str:
    return t("Opus event calls for today are spent ({n}/{cap})", n=calls, cap=cap)


def cooldown_reason(minutes: int) -> str:
    return t("Opus was asked {n} min ago", n=minutes)


def format_ai_failure(pair: str, reason: Optional[str]) -> str:
    return t("🧠 <b>{pair}</b>: Opus did not answer ({reason}). Try /plan again.",
             pair=pair, reason=escape_html(reason or t("no answer")))


def format_data_error(pair: str, detail: str) -> str:
    return t("⚠️ <b>{pair}</b>: data error ({detail})", pair=pair, detail=escape_html(detail))


# ------------------------------------------------------------- status/journal


def format_status(
    pairs: Sequence[str], plans: dict, trades: Sequence[Trade], instruments: dict,
) -> str:
    lines = [t("<b>Opus bot — status</b>")]
    for pair in pairs:
        plan = plans.get(pair)
        inst = instruments[pair]
        d = inst.price_decimals
        if plan is None:
            lines.append(f"{pair}: " + t("no plan yet — /plan"))
            continue
        head = (
            f"{pair}: {t(_STATUS_LABELS.get(plan.status, plan.status))} · "
            f"{_ACTION_LABELS.get(plan.action, plan.action)}"
        )
        if plan.direction != "none":
            head += f" {_side(plan)}"
        if plan.entry is not None:
            head += f" @ {_f(plan.entry, d)}"
        if plan.has_watch_zone:
            head += f" · {_f(plan.watch_low, d)}–{_f(plan.watch_high, d)}"
        head += f" · {escape_html(plan.as_of)}"
        lines.append(head)
    open_ = [tr for tr in trades if tr.status == OPEN]
    if open_:
        lines.append("")
        lines.append(t("<b>Open trades (tracked to TP1/SL):</b>"))
        for tr in open_:
            d = instruments[tr.pair].price_decimals
            lines.append(
                f"{tr.pair} {tr.direction.upper()} @ {tr.entry:.{d}f} · SL {tr.stop_loss:.{d}f} "
                f"· TP1 {tr.tp1:.{d}f}"
            )
    return "\n".join(lines)


def format_journal(trades: Sequence[Trade], history: Sequence[dict], days: int = 30) -> str:
    """Decisions and outcomes of the last `days` days."""
    now = datetime.now(tz=timezone.utc)
    cutoff = now.timestamp() - days * 86400

    def recent(iso: Optional[str]) -> bool:
        try:
            return datetime.fromisoformat(iso).timestamp() >= cutoff
        except (TypeError, ValueError):
            return False

    hist = [h for h in history if recent(h.get("created_at"))]
    trs = [tr for tr in trades if recent(tr.opened_at)]
    lines = [t("<b>Opus journal — last {days} days</b>", days=days)]
    counts = {a: sum(1 for h in hist if h.get("action") == a) for a in _ACTION_LABELS}
    lines.append(
        t("Decisions: {n} · limit {limit} · market {market} · wait {wait} · no trade {no}",
          n=len(hist), limit=counts["limit"], market=counts["market"],
          wait=counts["wait"], no=counts["no_trade"])
    )
    closed = [tr for tr in trs if tr.status in (TP, SL)]
    wins = sum(1 for tr in closed if tr.status == TP)
    total_r = sum(tr.result_r or 0.0 for tr in closed)
    lines.append(
        t("Trades: {n} opened · {closed} closed · {wins} TP / {losses} SL · {r}R",
          n=len(trs), closed=len(closed), wins=wins, losses=len(closed) - wins,
          r=f"{total_r:+.1f}")
    )
    by_pair = {}
    for tr in closed:
        by_pair.setdefault(tr.pair, []).append(tr)
    for pair, rows in sorted(by_pair.items()):
        w = sum(1 for tr in rows if tr.status == TP)
        lines.append(
            f"  {pair}: {w}/{len(rows)} · {sum(tr.result_r or 0 for tr in rows):+.1f}R"
        )
    if not hist:
        lines.append(t("nothing yet — press /plan"))
    return "\n".join(lines)
