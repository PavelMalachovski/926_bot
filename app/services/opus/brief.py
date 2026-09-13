"""The brief: everything the model is allowed to reason from, as text.

Pair and clock, session state, today's red news and any blackout in
force, the orders already issued today (the 1-2 trades goal), the rule
engine's reading as a labelled hint, the unswept liquidity the engine
sees, and the candles themselves — H4, H1, M5 — as rows. For a follow-up
the previous plan and the event that triggered the re-read are appended.
Pure: the caller fetches, this module only formats.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from app.services.opus.candles import candle_block
from app.services.opus.plan import OpusPlan
from app.services.smc.ai_read import describe_for_ai
from app.services.smc.instruments import Instrument
from app.services.smc.liquidity import LiquidityLevel, find_liquidity
from app.services.smc.models import AnalysisResult, Verdict
from app.services.smc.news import NewsEvent
from app.services.smc.sessions import active_session, session_end_utc, to_prague

EVENT_TEXT = {
    "zone_reached": "price has reached the watch zone you named",
    "invalidated": "an M5 candle closed beyond the invalidation level you named",
    "session_opened": "a new session block has opened (the plan was made in the previous one)",
}


def engine_hint(instrument: Instrument, data: dict, now: datetime) -> str:
    """The owner's rule engine on the same candles, as a labelled hint the
    model may disagree with. Never raises: an engine error is one line."""
    try:
        from app.services.smc.engine import TripleSyncEngine
        from app.services.smc.profiles import CONSERVATIVE

        res = AnalysisResult(
            symbol=instrument.key, verdict=Verdict.SKIP, checked_at=now,
            price_decimals=instrument.price_decimals,
        )
        res.session_name = active_session(
            now, require_weekday=instrument.source == "forex"
        )
        res.price = data["m5"][-1].close
        res.m5_candles, res.h4_candles, res.h1_candles = data["m5"], data["h4"], data["h1"]
        engine = TripleSyncEngine(
            instrument=instrument, profile=CONSERVATIVE, max_entry_gap_r=99.0,
        )
        res = engine.evaluate(h4=data["h4"], h1=data["h1"], m5=data["m5"], result=res)
        return describe_for_ai(res, instrument)
    except Exception as e:  # the hint is optional; the brief is not
        return f"(engine reference unavailable: {e})"


def _pools(levels: Sequence[LiquidityLevel], price: float, d: int, limit: int = 4) -> str:
    highs = sorted((lv for lv in levels if lv.is_high and lv.price > price), key=lambda lv: lv.price)
    lows = sorted((lv for lv in levels if not lv.is_high and lv.price < price), key=lambda lv: -lv.price)

    def fmt(lv: LiquidityLevel) -> str:
        return f"{lv.price:.{d}f}" + (f" (EQ x{lv.equal_count})" if lv.equal_count > 1 else "")

    return (
        "above: " + (", ".join(fmt(lv) for lv in highs[:limit]) or "none")
        + "; below: " + (", ".join(fmt(lv) for lv in lows[:limit]) or "none")
    )


def liquidity_hint(instrument: Instrument, data: dict) -> str:
    d = instrument.price_decimals
    price = data["m5"][-1].close
    try:
        h4 = find_liquidity(data["h4"], "H4", instrument.min_fvg)
        h1 = find_liquidity(data["h1"], "H1", instrument.min_fvg)
    except Exception:
        return ""
    return (
        f"Unswept liquidity (engine): H4 {_pools(h4, price, d)} | "
        f"H1 {_pools(h1, price, d)}"
    )


def news_lines(
    events: Sequence[NewsEvent], before: timedelta, after: timedelta,
) -> List[str]:
    out = []
    for e in events:
        start = to_prague(e.time - before).strftime("%H:%M")
        end = to_prague(e.time + after).strftime("%H:%M")
        out.append(f"{e.prague_hhmm()} {e.currency} {e.title} (no entries {start}-{end})")
    return out


def _plan_lines(plan: OpusPlan, d: int) -> List[str]:
    def f(v: Optional[float]) -> str:
        return "n/a" if v is None else f"{v:.{d}f}"

    lines = [
        f"PREVIOUS PLAN ({plan.as_of} Prague, price then {f(plan.price)}): "
        f"{plan.action.upper()} {plan.direction.upper() if plan.direction != 'none' else ''}"
        f" bias {plan.bias}, confidence {plan.confidence}/5, status {plan.status}",
    ]
    if plan.entry is not None:
        lines.append(
            f"  entry {f(plan.entry)}, stop {f(plan.stop_loss)}, tp1 {f(plan.tp1)}, "
            f"tp2 {f(plan.tp2)}, invalidation {f(plan.invalidation)}"
        )
    if plan.has_watch_zone:
        lines.append(f"  watch zone {f(plan.watch_low)}-{f(plan.watch_high)}")
    if plan.read:
        lines.append(f"  your read then: {plan.read}")
    if plan.risks:
        lines.append("  your risks then: " + "; ".join(plan.risks))
    return lines


def build_brief(
    instrument: Instrument,
    data: dict,
    now: datetime,
    *,
    todays_news: Sequence[NewsEvent] = (),
    blackout: Optional[NewsEvent] = None,
    news_before: timedelta = timedelta(minutes=60),
    news_after: timedelta = timedelta(minutes=15),
    orders_today: int = 0,
    min_rr: float = 2.0,
    counts: tuple = (80, 150, 200),
    previous: Optional[OpusPlan] = None,
    event: Optional[str] = None,
    engine_text: Optional[str] = None,
) -> str:
    d = instrument.price_decimals
    price = data["m5"][-1].close
    local = to_prague(now)
    session = active_session(now, require_weekday=instrument.source == "forex")
    lines = [
        f"Pair: {instrument.key} (pip {instrument.pip:g}, prices to {d} decimals)",
        f"Now: {local.strftime('%Y-%m-%d %H:%M %A')} Prague",
    ]
    if session:
        end = session_end_utc(now)
        left = max(int((end - now).total_seconds() // 60), 0) if end else 0
        lines.append(
            f"Session: {session}, block ends {to_prague(end).strftime('%H:%M')} "
            f"({left} min left); the day ends 18:30"
        )
    else:
        lines.append(
            "Session: CLOSED — no market entry now; you may plan a limit or a "
            "watch zone for the next block (08:00 or 14:00 Prague)"
        )
    lines.append(f"Price: {price:.{d}f} (last closed M5)")
    lines.append(
        f"Last closed M5 candle: {to_prague(data['m5'][-1].timestamp).strftime('%H:%M')} Prague"
    )
    if blackout is not None:
        until = to_prague(blackout.time + news_after).strftime("%H:%M")
        lines.append(
            f"RED-NEWS BLACKOUT NOW: {blackout.currency} {blackout.title} at "
            f"{blackout.prague_hhmm()} — no entries until {until} Prague"
        )
    news = news_lines(todays_news, news_before, news_after)
    lines.append(
        "Red news today for this pair (Prague time): "
        + ("; ".join(news) if news else "none")
    )
    lines.append(
        f"Orders already issued today for this pair: {orders_today} "
        "(goal: one or two quality trades per day, never more)"
    )
    lines.append(f"Minimum RR to tp1 (hard limit): 1:{min_rr:.1f}")
    if event:
        lines.append("")
        lines.append(f"EVENT: {EVENT_TEXT.get(event, event)} — re-read the pair now.")
    if previous is not None:
        lines.append("")
        lines.extend(_plan_lines(previous, d))
    lines.append("")
    lines.append(
        "Rule-engine reference (the owner's Triple Sync + Imbalance engine "
        "on these candles; a hint you may disagree with, never a verdict):"
    )
    lines.append(engine_text if engine_text is not None else engine_hint(instrument, data, now))
    liq = liquidity_hint(instrument, data)
    if liq:
        lines.append(liq)
    lines.append("")
    lines.append(
        "Candles, Prague time, oldest first; columns: time open high low "
        "close; the last row is the most recent CLOSED candle."
    )
    h4_n, h1_n, m5_n = counts
    lines.append(candle_block("H4", data["h4"], d, h4_n))
    lines.append(candle_block("H1", data["h1"], d, h1_n))
    lines.append(candle_block("M5", data["m5"], d, m5_n))
    return "\n".join(lines)
