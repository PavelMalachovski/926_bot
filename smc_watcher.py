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
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import INSTRUMENTS, Instrument, get_instrument
from app.services.smc.journal import SignalJournal
from app.services.smc.news import NewsCalendar, relevant_currencies
from app.services.smc.models import AnalysisResult, Direction, Verdict
from app.services.smc.notifier import (
    TelegramNotifier,
    format_no_setup,
    format_result,
)
from app.services.smc.oanda import OandaDataFetcher
from app.services.smc.yahoo import YahooDataFetcher
from app.services.smc.sessions import active_session, to_prague
from app.services.smc.state import WatcherState
from app.services.smc.telegram_bot import TelegramCommandBot

configure_logging()
logger = structlog.get_logger("smc_watcher")

# Windows consoles often default to a legacy codepage that cannot print emoji.
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STATE_FILE = os.getenv("SMC_STATE_FILE", ".smc_watcher_state.json")
JOURNAL_FILE = os.getenv("SMC_JOURNAL_FILE", ".smc_journal.json")

APPROVED = (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET)


def _build_fetcher(instrument: Instrument):
    if instrument.source == "crypto":
        return BinanceDataFetcher(instrument.source_symbol)
    # Forex: OANDA when a token is configured (better data), otherwise the
    # free keyless Yahoo Finance feed.
    if settings.oanda.api_token:
        return OandaDataFetcher(
            symbol=instrument.source_symbol,
            api_token=settings.oanda.api_token,
            environment=settings.oanda.environment,
        )
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
            "❌ ПРАВИЛО 9: EURUSD и GBPUSD в одном направлении — запрещённая "
            "комбинация (корреляция ~0.90). Выбери ОДНУ из пар."
        )
    for sym, d in (("EURUSD", eur), ("GBPUSD", gbp)):
        if d and jpy and d != jpy:
            warnings.append(
                f"❌ ПРАВИЛО 9: {sym} {d.value} + USDJPY {jpy.value} — тройная "
                "ставка на одну сторону USD. Запрещено."
            )
    return warnings


class Watcher:
    """Owns the state, the 15-minute scheduler and result reporting."""

    def __init__(self):
        self.state = WatcherState(STATE_FILE)
        chat_id = settings.smc.chat_id or settings.telegram.chat_id
        token = settings.telegram.bot_token
        if not token or token.startswith("your-"):
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        if not chat_id:
            raise RuntimeError("Set TELEGRAM_CHAT_ID (or SMC_CHAT_ID)")
        self.notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)
        self.journal = SignalJournal(JOURNAL_FILE)
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
        )
        self.last_results: Dict[str, AnalysisResult] = {}
        # apply env default on first ever start (state file wins afterwards)
        if not os.path.exists(STATE_FILE):
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
            return f"⚠️ {key}: ошибка данных ({e})", None
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
            return "⚠️ Нет активных пар — включи хотя бы одну через /pairs"

        if self.news:
            await self.news.refresh_if_stale()
            await self._send_morning_digest()
            await self._rule_04_warnings()

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
                        f"⏳ {key}: сетап, о котором писал ранее, всё ещё активен"
                    )
                else:
                    approved.append(result)
                    if await self.notifier.send(format_result(result)):
                        self.state.last_setup[key] = fingerprint
                        self.state.save()
                    self.journal.record(result)
                    heartbeat_lines.append(f"🚨 {key}: СЕТАП НАЙДЕН — детали выше!")
            else:
                heartbeat_lines.append(line)

        for warning in _correlation_warnings(approved):
            await self.notifier.send(warning)

        await self._track_journal()

        time_str = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")
        summary = f"🔍 <b>Проверка {time_str}</b>\n" + "\n".join(heartbeat_lines)
        logger.info("Cycle summary", summary=" | ".join(heartbeat_lines))
        # By default only setup alerts go to Telegram; the heartbeat is opt-in.
        if settings.smc.notify_no_setup and not approved:
            await self.notifier.send(summary)
        return summary

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
            f"⛔ {key}: блэкаут — 🔴 {event.title} ({event.currency}) "
            f"в {event.prague_hhmm()} Праги, входы запрещены"
        )

    async def _send_morning_digest(self) -> None:
        """Once a day before the session: today's red news (Правило -1)."""
        if not settings.smc.news_digest or self.news.fetched_at is None:
            return
        local = to_prague(datetime.now(tz=timezone.utc))
        today = local.date().isoformat()
        if self.state.last_digest_date == today or local.hour < 7:
            return
        currencies = set()
        for key in self.state.pairs:
            currencies |= relevant_currencies(get_instrument(key))
        await self.notifier.send(self.news.digest_text(currencies))
        self.state.last_digest_date = today
        self.state.save()

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
                    "переведи SL в безубыток"
                    if signal["status"] == "open"
                    else "удали отложенный ордер"
                )
                await self.notifier.send(
                    f"⚠️ <b>ПРАВИЛО 0.4:</b> {signal['pair']} — 🔴 {event.title} "
                    f"({event.currency}) через {minutes_left} мин "
                    f"({event.prague_hhmm()} Праги). У тебя "
                    f"{'открытая позиция' if signal['status'] == 'open' else 'активная лимитка'} "
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
            return "Новостной фильтр выключен (SMC_NEWS_ENABLED=false)."
        currencies = set()
        for key in self.state.pairs:
            currencies |= relevant_currencies(get_instrument(key))
        return self.news.digest_text(currencies)

    async def _track_journal(self) -> None:
        """Advance unresolved journal signals using fresh M5 candles."""
        for pair in self.journal.unresolved_pairs():
            try:
                fetcher = _build_fetcher(get_instrument(pair))
                candles = await fetcher.fetch_candles("5m", limit=400)
            except Exception as e:
                logger.warning("Journal update failed", pair=pair, error=str(e))
                continue
            self.journal.update_pair(pair, candles)

    # ---------------------------------------------------------------- status

    def status_text(self) -> str:
        session = active_session(datetime.now(tz=timezone.utc))
        lines = [
            "<b>SMC Watcher — статус</b>",
            f"Пары: {', '.join(self.state.pairs) or 'нет'}",
            f"Сессия сейчас: {session or 'вне сессии'}",
            f"Интервал: каждые {settings.smc.interval_minutes} мин",
            f"Депозит для лота: "
            + (f"${settings.smc.deposit:.0f}" if settings.smc.deposit else "не задан"),
        ]
        if self.last_results:
            lines.append("")
            lines.append("<b>Последняя проверка:</b>")
            for key, r in self.last_results.items():
                lines.append(f"• {key}: {r.verdict.value} ({r.checked_at:%H:%M UTC})")
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
        "🧪 <b>ТЕСТ SMC-вотчера</b> — связь с Telegram работает.",
        f"{URGENT_HEADER}\n\n🧪 ТЕСТ: так будет выглядеть срочное сообщение "
        "о найденном сетапе (это НЕ реальный сигнал).",
        "🔍 ТЕСТ: так выглядит 15-минутный отчёт. Команды: /pairs /status /check",
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
