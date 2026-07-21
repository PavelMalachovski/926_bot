"""SMC strategy watcher — Triple Sync + Imbalance for multiple pairs.

The only service of this project. Every 15 minutes (aligned to :00/:15/:30/:45)
it runs the strategy for each enabled pair and sends a Telegram message only
when a valid setup is found (🚨 urgent alert). Checks without a setup are
logged; set SMC_NOTIFY_NO_SETUP=true to also receive 15-min heartbeats.

Pairs are chosen at runtime via Telegram commands (/pairs) handled by a
long-polling loop in the same process. ETHUSD data comes from Binance;
forex pairs (USDJPY, EURUSD, GBPUSD, USDCAD) come from the free Yahoo
Finance feed by default, or from OANDA v20 when OANDA_API_TOKEN is set.

Usage:
    python smc_watcher.py                  # run forever (scheduler + bot)
    python smc_watcher.py --once           # single check of enabled pairs
    python smc_watcher.py --test-telegram  # verify Telegram wiring
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import structlog

from app.core.config import settings
from app.core.logging import configure_logging
from app.services.smc.data import BinanceDataFetcher
from app.services.smc.db import Database, migrate_legacy_json
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import INSTRUMENTS, Instrument, get_instrument
from app.services.smc.journal import SignalJournal
from app.services.smc.news import NewsCalendar, relevant_currencies
from app.services.smc.models import AnalysisResult, Direction, Verdict
from app.services.smc.notifier import (
    TelegramNotifier,
    escape_html,
    format_no_setup,
    format_plan,
    format_result,
)
from app.services.smc.oanda import OandaDataFetcher
from app.services.smc.twelvedata import TwelveDataFetcher
from app.services.smc.yahoo import YahooDataFetcher
from app.services.smc.sessions import active_session, to_prague
from app.services.smc.state import WatcherState
from app.services.smc.telegram_bot import TelegramCommandBot

configure_logging()
logger = structlog.get_logger("smc_watcher")

# Windows consoles often default to a legacy codepage that cannot print emoji.
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_FILE = os.getenv("SMC_DB_FILE", ".smc_watcher.db")
# legacy JSON files, imported into SQLite once if present
STATE_FILE = os.getenv("SMC_STATE_FILE", ".smc_watcher_state.json")
JOURNAL_FILE = os.getenv("SMC_JOURNAL_FILE", ".smc_journal.json")

APPROVED = (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET)


def _forex_source() -> str:
    """Resolve the configured forex source, honouring 'auto'."""
    source = settings.smc.forex_source.strip().lower()
    if source != "auto":
        return source
    if settings.twelvedata.api_key:
        return "twelvedata"
    if settings.oanda.api_token:
        return "oanda"
    return "yahoo"


def _build_fetcher(instrument: Instrument):
    if instrument.source == "crypto":
        # ETHUSD stays on Binance: unlimited, deep history, funding rate.
        return BinanceDataFetcher(instrument.source_symbol)
    source = _forex_source()
    if source == "twelvedata" and settings.twelvedata.api_key:
        return TwelveDataFetcher(instrument.key, settings.twelvedata.api_key)
    if source == "oanda" and settings.oanda.api_token:
        return OandaDataFetcher(
            symbol=instrument.source_symbol,
            api_token=settings.oanda.api_token,
            environment=settings.oanda.environment,
        )
    # Default / fallback: free keyless Yahoo Finance feed.
    return YahooDataFetcher(symbol=f"{instrument.key}=X")


def _build_engine(instrument: Instrument) -> TripleSyncEngine:
    fetcher = _build_fetcher(instrument)
    smc = settings.smc
    return TripleSyncEngine(
        instrument=instrument,
        min_rr=smc.min_rr,
        risk_pct=smc.risk_pct,
        deposit=smc.deposit,
        enforce_sessions=smc.enforce_sessions,
        fetcher=fetcher,
    )


def _setup_fingerprint(result: AnalysisResult) -> str:
    setup = result.setup
    day = result.checked_at.strftime("%Y-%m-%d")
    return (
        f"{result.symbol}:{setup.direction.value}:{setup.entry}:"
        f"{result.session_name}:{day}"
    )


def _card_footer(signal: Dict) -> str:
    """Status history appended to the alert message (live setup card)."""

    def hhmm(iso: Optional[str]) -> str:
        if not iso:
            return ""
        local = to_prague(datetime.fromisoformat(iso))
        return f" ({local:%H:%M} Prague)"

    lines = ["", "──────────────"]
    if signal.get("filled_at"):
        lines.append(f"📈 Filled @ {signal['entry']}{hhmm(signal['filled_at'])}")
    status = signal["status"]
    when = hhmm(signal.get("resolved_at"))
    if status == "tp":
        lines.append(f"🎯 <b>TP HIT</b>{when} — planned +{signal['rr']:.1f}R")
    elif status == "sl":
        lines.append(f"🛑 <b>SL HIT</b>{when} — −1R")
    elif status == "expired":
        lines.append("🗑 Expired unfilled — order dies with its session (Rule 10)")
    elif status == "timeout":
        lines.append("⌛ Timed out — untracked after 5 days")
    elif status == "open":
        lines.append("⏳ Position live — tracking TP/SL")
    return "\n".join(lines)


def _correlation_warnings(approved: List[AnalysisResult]) -> List[str]:
    """Rule 9.2: warn about forbidden simultaneous USD combinations."""
    warnings = []
    by_pair: Dict[str, Direction] = {
        r.symbol: r.setup.direction for r in approved if r.setup
    }
    eur, gbp, jpy = (
        by_pair.get("EURUSD"),
        by_pair.get("GBPUSD"),
        by_pair.get("USDJPY"),
    )
    if eur and gbp and eur == gbp:
        warnings.append(
            "❌ RULE 9: EURUSD and GBPUSD in the same direction — forbidden "
            "combination (correlation ~0.90). Pick ONE of the pairs."
        )
    for sym, d in (("EURUSD", eur), ("GBPUSD", gbp)):
        if d and jpy and d != jpy:
            warnings.append(
                f"❌ RULE 9: {sym} {d.value} + USDJPY {jpy.value} — a triple bet "
                "on one side of USD. Forbidden."
            )
    return warnings


class Watcher:
    """Owns the state, the 15-minute scheduler and result reporting."""

    def __init__(self):
        self.db = Database(DB_FILE)
        migrate_legacy_json(self.db, STATE_FILE, JOURNAL_FILE)
        self.state = WatcherState(self.db)
        chat_id = settings.smc.chat_id or settings.telegram.chat_id
        token = settings.telegram.bot_token
        if not token or token.startswith("your-"):
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        if not chat_id:
            raise RuntimeError("Set TELEGRAM_CHAT_ID (or SMC_CHAT_ID)")
        self.notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)
        self.journal = SignalJournal(self.db)
        self.news = (
            NewsCalendar(
                before_minutes=settings.smc.news_blackout_before_min,
                after_minutes=settings.smc.news_blackout_after_min,
            )
            if settings.smc.news_enabled
            else None
        )
        self.bot = TelegramCommandBot(
            bot_token=token,
            owner_chat_id=chat_id,
            state=self.state,
            run_cycle=self.run_cycle,
            status_text=self.status_text,
            stats_text=self.journal.stats_text,
            news_text=self.news_text,
            on_trade_mark=self.mark_trade,
            on_plan=self.on_plan,
        )
        self.last_results: Dict[str, AnalysisResult] = {}
        # apply the env default on the very first start (DB wins afterwards)
        if self.db.kv_get("pairs") is None:
            env_pairs = [p for p in settings.smc.default_pairs() if p in INSTRUMENTS]
            if env_pairs:
                self.state.pairs = env_pairs
                self.state.save()

    # ------------------------------------------------------------- one cycle

    async def check_pair(self, key: str) -> Tuple[str, Optional[AnalysisResult]]:
        """Analyze one pair. Returns (heartbeat line, result or None)."""
        instrument = get_instrument(key)
        engine = _build_engine(instrument)
        try:
            result = await engine.analyze()
        except Exception as e:
            logger.error("Pair check failed", pair=key, error=str(e))
            return f"⚠️ {key}: data error ({e})", None
        self.last_results[key] = result
        logger.info(
            "SMC check finished",
            pair=key,
            verdict=result.verdict.value,
            price=result.price,
            reasons=result.reasons,
        )
        return format_no_setup(result), result

    async def run_cycle(self) -> str:
        """Run the strategy for all enabled pairs; send alerts; return summary."""
        if not self.state.pairs:
            return "⚠️ No active pairs — enable at least one via /pairs"

        if self.news:
            await self.news.refresh_if_stale()
            await self._rule_04_warnings()
        await self._morning_briefing()

        heartbeat_lines: List[str] = []
        approved: List[AnalysisResult] = []

        for key in list(self.state.pairs):
            blackout = self._news_blackout(key)
            if blackout:
                heartbeat_lines.append(blackout)
                continue
            line, result = await self.check_pair(key)
            if result and result.verdict in APPROVED:
                fingerprint = _setup_fingerprint(result)
                if self.state.last_setup.get(key) == fingerprint:
                    heartbeat_lines.append(
                        f"⏳ {key}: previously reported setup is still active"
                    )
                    continue
                block = self.journal.discipline_block(
                    key,
                    result.setup.direction.value,
                    result.session_name,
                    result.checked_at,
                )
                if block:
                    logger.info("Alert suppressed", pair=key, rule=block)
                    heartbeat_lines.append(f"⛔ {key}: alert suppressed — {block}")
                    continue
                cooldown = self._cooldown_left(key)
                if cooldown:
                    logger.info("Alert muted (taken cooldown)", pair=key, left=cooldown)
                    heartbeat_lines.append(
                        f"🔕 {key}: setup found but muted — you took a trade "
                        f"here, {cooldown} left"
                    )
                    continue
                approved.append(result)
                await self._send_alert(key, result, fingerprint)
                heartbeat_lines.append(f"🚨 {key}: SETUP FOUND — details above!")
            else:
                heartbeat_lines.append(line)

        for warning in _correlation_warnings(approved):
            await self.notifier.send(warning)

        await self._track_journal()

        time_str = to_prague(datetime.now(tz=timezone.utc)).strftime("%H:%M")
        summary = f"🔍 <b>Check {time_str} Prague</b>\n" + "\n".join(heartbeat_lines)
        logger.info("Cycle summary", summary=" | ".join(heartbeat_lines))
        # By default only setup alerts go to Telegram; the heartbeat is opt-in.
        if settings.smc.notify_no_setup and not approved:
            await self.notifier.send(summary)
        return summary

    # ---------------------------------------------------------------- alerts

    async def _send_alert(
        self, key: str, result: AnalysisResult, fingerprint: str
    ) -> None:
        """Urgent alert: message with Took/Skipped buttons + setup chart."""
        signal = self.journal.record(result)
        text = format_result(result)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Took it", "callback_data": f"take_{signal['id']}"},
                    {"text": "❌ Skipped", "callback_data": f"skip_{signal['id']}"},
                ]
            ]
        }
        message_id = await self.notifier.send(text, reply_markup=keyboard)
        if message_id:
            self.state.last_setup[key] = fingerprint
            self.state.save()
            self.journal.attach_message(signal["id"], message_id, text)
            await self.notifier.pin(message_id)
            await self._send_chart(result, message_id)

    async def _send_chart(self, result: AnalysisResult, reply_to: int) -> None:
        """Attach the setup chart PNG (must never block the alert)."""
        try:
            from app.services.smc.chart import render_setup_chart

            png = render_setup_chart(result)
            if png:
                await self.notifier.send_photo(png, reply_to=reply_to)
        except Exception as e:
            logger.warning("Chart rendering failed", pair=result.symbol, error=str(e))

    async def mark_trade(self, signal_id: str, taken: bool) -> str:
        """Callback for the Took/Skipped buttons on alerts."""
        signal = self.journal.mark_taken(signal_id, taken)
        if not signal:
            return "Signal not found (journal may have been reset)"
        if not taken:
            return f"{signal['pair']} marked as skipped"
        # You are now managing this position: mute new alerts for the pair.
        hours = settings.smc.taken_cooldown_hours
        expiry = datetime.now(tz=timezone.utc) + timedelta(hours=hours)
        self.state.pair_cooldown[signal["pair"]] = expiry.isoformat()
        self.state.save()
        return (
            f"{signal['pair']} marked as taken — tracking your stats; "
            f"muted for {hours:.0f}h"
        )

    def _cooldown_left(self, key: str) -> Optional[str]:
        """Human 'Nh Mm' remaining on a taken-trade mute, or None if expired."""
        expiry = self.state.pair_cooldown.get(key)
        if not expiry:
            return None
        now = datetime.now(tz=timezone.utc)
        try:
            remaining = datetime.fromisoformat(expiry) - now
        except ValueError:
            return None
        if remaining.total_seconds() <= 0:
            del self.state.pair_cooldown[key]
            self.state.save()
            return None
        total_min = int(remaining.total_seconds() // 60)
        return f"{total_min // 60}h {total_min % 60}m"

    async def _handle_journal_events(self, events) -> None:
        """Live-update alert cards and enforce the daily stop notification."""
        now = datetime.now(tz=timezone.utc)
        for signal, event in events:
            if signal.get("message_id") and signal.get("alert_text"):
                footer = _card_footer(signal)
                keep_buttons = signal.get("taken") is None
                keyboard = (
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "✅ Took it",
                                    "callback_data": f"take_{signal['id']}",
                                },
                                {
                                    "text": "❌ Skipped",
                                    "callback_data": f"skip_{signal['id']}",
                                },
                            ]
                        ]
                    }
                    if keep_buttons
                    else None
                )
                await self.notifier.edit_message(
                    signal["message_id"],
                    signal["alert_text"] + footer,
                    reply_markup=keyboard,
                )
                if event in ("tp", "sl", "expired", "timeout"):
                    await self.notifier.unpin(signal["message_id"])
            # Rule 0.2 proxy: the second taken stop of the day closes trading
            if (
                event == "sl"
                and signal.get("taken") == 1
                and self.journal.taken_sl_count_today(now) == 2
            ):
                today = to_prague(now).date().isoformat()
                if self.state.day_stop_notified != today:
                    self.state.day_stop_notified = today
                    self.state.save()
                    await self.notifier.send(
                        "🛑 <b>RULE 0.2:</b> two taken stop-losses today — "
                        "the trading day is CLOSED. No more alerts until "
                        "tomorrow. A skipped bad day is a win."
                    )

    # ------------------------------------------------------------------ news

    def _news_blackout(self, key: str) -> Optional[str]:
        """Heartbeat line if the pair is inside a red-news blackout window."""
        if not self.news:
            return None
        instrument = get_instrument(key)
        event = self.news.blackout(relevant_currencies(instrument))
        if not event:
            return None
        logger.info(
            "News blackout", pair=key, event=event.title, currency=event.currency
        )
        return (
            f"⛔ {key}: blackout — 🔴 {escape_html(event.title)} ({event.currency}) "
            f"at {event.prague_hhmm()} Prague, entries blocked"
        )

    async def _morning_briefing(self) -> None:
        """Once a day Mon-Fri at 07:45 Prague: today's red-news digest
        (strategy Rule -1). The per-pair plan is on demand via /plan."""
        if not settings.smc.news_digest or not self.news or self.news.fetched_at is None:
            return
        local = to_prague(datetime.now(tz=timezone.utc))
        if local.weekday() >= 5:
            return  # Forex Factory has no weekend releases
        today = local.date().isoformat()
        try:
            hh, mm = settings.smc.news_digest_time.split(":")
            after = local.replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0
            )
        except ValueError:
            after = local.replace(hour=7, minute=45, second=0, microsecond=0)
        if self.state.last_digest_date == today or local < after:
            return
        await self.notifier.send(self.news.digest_text(self.state.pairs))
        self.state.last_digest_date = today
        self.state.save()

    async def on_plan(self, key: str) -> None:
        """/plan command: send the Pre-Market Plan for a pair (or ALL)."""
        keys = list(self.state.pairs) if key == "ALL" else [key]
        for k in keys:
            if k in INSTRUMENTS:
                await self._send_pair_plan(k)

    async def _send_pair_plan(self, key: str) -> None:
        """Build and send one pair's Pre-Market Plan (text + H1 chart)."""
        from app.services.smc.chart import render_plan_chart
        from app.services.smc.plan import build_plan

        instrument = get_instrument(key)
        try:
            data = await _build_fetcher(instrument).fetch_all_timeframes()
        except Exception as e:
            logger.warning("Plan fetch failed", pair=key, error=str(e))
            return
        now = datetime.now(tz=timezone.utc)
        stale = (
            instrument.source == "forex"
            and now - data["m5"][-1].timestamp > timedelta(minutes=30)
        )
        plan = build_plan(
            instrument, data["h4"], data["h1"], data["m5"], market_closed=stale
        )
        await self.notifier.send(format_plan(plan, min_rr=settings.smc.min_rr))
        try:
            png = render_plan_chart(plan, data["h1"])
            if png:
                await self.notifier.send_photo(png)
        except Exception as e:
            logger.warning("Plan chart failed", pair=key, error=str(e))

    async def _rule_04_warnings(self) -> None:
        """Rule 0.4: active signal + red news soon -> SL to BU / pull the order."""
        now = datetime.now(tz=timezone.utc)
        horizon = timedelta(minutes=30)
        for signal in self.journal.signals:
            if signal["status"] not in ("pending", "open"):
                continue
            instrument = get_instrument(signal["pair"])
            for event in self.news.upcoming(relevant_currencies(instrument), horizon):
                warn_key = f"{signal['id']}:{event.time.isoformat()}"
                if warn_key in self.state.news_warned:
                    continue
                minutes_left = int((event.time - now).total_seconds() // 60)
                action = (
                    "move the SL to breakeven"
                    if signal["status"] == "open"
                    else "cancel the pending order"
                )
                await self.notifier.send(
                    f"⚠️ <b>RULE 0.4:</b> {signal['pair']} — 🔴 {escape_html(event.title)} "
                    f"({event.currency}) in {minutes_left} min "
                    f"({event.prague_hhmm()} Prague). You have "
                    f"{'an open position' if signal['status'] == 'open' else 'an active limit order'} "
                    f"— {action}!"
                )
                self.state.news_warned[warn_key] = now.isoformat()
        # prune dedup keys older than 2 days
        cutoff = now - timedelta(days=2)
        self.state.news_warned = {
            k: v
            for k, v in self.state.news_warned.items()
            if datetime.fromisoformat(v) > cutoff
        }
        self.state.save()

    def news_text(self) -> str:
        """/news command."""
        if not self.news:
            return "News filter is disabled (SMC_NEWS_ENABLED=false)."
        return self.news.digest_text(self.state.pairs)

    async def _track_journal(self) -> None:
        """Advance unresolved journal signals using fresh M5 candles."""
        for pair in self.journal.unresolved_pairs():
            try:
                fetcher = _build_fetcher(get_instrument(pair))
                candles = await fetcher.fetch_candles("5m", limit=400)
            except Exception as e:
                logger.warning("Journal update failed", pair=pair, error=str(e))
                continue
            events = self.journal.update_pair(pair, candles)
            if events:
                await self._handle_journal_events(events)

    # ---------------------------------------------------------------- status

    def status_text(self) -> str:
        session = active_session(datetime.now(tz=timezone.utc))
        lines = [
            "<b>SMC Watcher — status</b>",
            f"Pairs: {', '.join(self.state.pairs) or 'none'}",
            f"Forex data: {_forex_source()} | crypto: Binance",
            f"Session now: {session or 'off session'}",
            f"Cadence: {settings.smc.session_interval_minutes} min in session / "
            f"{settings.smc.interval_minutes} min off",
            "Deposit for sizing: "
            + (f"${settings.smc.deposit:.0f}" if settings.smc.deposit else "not set"),
        ]
        muted = [
            f"{k} ({left})"
            for k in self.state.pairs
            if (left := self._cooldown_left(k))
        ]
        if muted:
            lines.append(f"🔕 Muted (taken): {', '.join(muted)}")
        if self.last_results:
            lines.append("")
            lines.append("<b>Last check:</b>")
            for key, r in self.last_results.items():
                local = to_prague(r.checked_at)
                lines.append(f"• {key}: {r.verdict.value} ({local:%H:%M} Prague)")
        return "\n".join(lines)

    # ------------------------------------------------------------- scheduler

    async def scheduler_loop(self) -> None:
        session_interval = settings.smc.session_interval_minutes
        off_interval = settings.smc.interval_minutes
        logger.info(
            "SMC watcher started",
            pairs=self.state.pairs,
            session_interval_minutes=session_interval,
            off_session_interval_minutes=off_interval,
        )
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error("SMC cycle failed", error=str(e), exc_info=True)
            # M5 cadence inside sessions, relaxed outside. Session windows
            # start on the hour, so the coarse grid never misses an open.
            now = datetime.now(tz=timezone.utc)
            interval = session_interval if active_session(now) else off_interval
            # +10s so the just-closed M5 candle is already served by the APIs
            await asyncio.sleep(_seconds_until_next_slot(interval) + 10)

    async def run_forever(self) -> None:
        await asyncio.gather(self.scheduler_loop(), self.bot.run())


def _seconds_until_next_slot(interval_minutes: int) -> float:
    """Seconds until the next aligned slot (e.g. :00/:15/:30/:45 for 15m)."""
    now = datetime.now(tz=timezone.utc)
    seconds_into_hour = now.minute * 60 + now.second + now.microsecond / 1e6
    slot = interval_minutes * 60
    return slot - (seconds_into_hour % slot)


async def run_once() -> None:
    watcher = Watcher()
    summary = await watcher.run_cycle()
    print(summary.replace("<b>", "").replace("</b>", ""))


async def run_telegram_test() -> None:
    """Send sample messages to verify the Telegram wiring end-to-end."""
    from app.services.smc.notifier import URGENT_HEADER

    watcher = Watcher()
    samples = [
        "🧪 <b>SMC watcher TEST</b> — Telegram wiring works.",
        f"{URGENT_HEADER}\n\n🧪 TEST: this is how an urgent setup alert looks "
        "(NOT a real signal).",
        "🔍 TEST: commands available: /pairs /status /check /stats /news",
    ]
    for text in samples:
        ok = await watcher.notifier.send(text)
        print(f"Telegram: {'sent' if ok else 'FAILED'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Triple Sync + Imbalance watcher")
    parser.add_argument(
        "--once", action="store_true", help="run a single check and exit"
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="send test messages to verify Telegram wiring and exit",
    )
    args = parser.parse_args()
    try:
        if args.test_telegram:
            asyncio.run(run_telegram_test())
        elif args.once:
            asyncio.run(run_once())
        else:
            asyncio.run(Watcher().run_forever())
    except KeyboardInterrupt:
        sys.exit(0)
