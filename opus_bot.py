"""Opus analyst bot — the alternative to the rule engine (owner request
2026-09-13, «альтернатива этому боту»).

A second Telegram bot in a second process. On /plan it fetches H4/H1/M5,
draws two context charts, writes a brief (candles as rows, today's red
news, the rule engine's reading as a hint) and asks Claude Opus for the
order: limit, market, wait or no trade, with every level. The code holds
the model to three hard limits (session window, red-news blackout,
minimum RR) and the geometry of a valid order, then sends the card and
the M5 chart. Afterwards it watches the price every five minutes in
session and asks Opus again — a bounded number of times a day — when
price reaches the zone it named, when a candle closes beyond its
invalidation, or when a new session block opens. A filled limit becomes
a trade tracked silently to TP1/SL for /journal; the position itself is
the owner's.

Usage:
    python opus_bot.py                 # run forever (monitor + bot)
    python opus_bot.py --plan USDJPY   # one decision, printed, no Telegram
    python opus_bot.py --test-telegram # verify the wiring
"""

import argparse
import asyncio
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional

import structlog

from app.core.config import settings
from app.core.exceptions import ConfigurationError, DataFetchError
from app.core.logging import configure_logging
from app.services.opus import plan as planmod
from app.services.opus.analyst import OpusAnalyst
from app.services.opus.brief import build_brief
from app.services.opus.chart import render_context_chart, render_plan_chart
from app.services.opus.decision import Limits
from app.services.opus.messages import (
    budget_reason, cooldown_reason, format_ai_failure, format_data_error,
    format_event_without_read, format_expired, format_journal, format_plan_card,
    format_status, news_warning_line,
)
from app.services.opus.plan import (
    EV_EXPIRED, EV_INVALIDATED, EV_SESSION_OPENED, EV_ZONE_REACHED, OpusPlan,
    advance_plan, advance_trade, plan_from_decision, trade_from_plan,
)
from app.services.opus.store import PlanStore
from app.services.opus.telegram import OpusCommandBot
from app.services.smc.db import Database
from app.services.smc.i18n import set_language, t
from app.services.smc.instruments import INSTRUMENTS, Instrument, get_instrument
from app.services.smc.news import NewsCalendar, relevant_currencies
from app.services.smc.notifier import TelegramNotifier, escape_html, redact_secrets
from app.services.smc.sessions import (
    PRAGUE, active_session, session_block, session_end_utc, to_prague,
)
from app.services.smc.sources import build_fetcher

configure_logging()
logger = structlog.get_logger("opus_bot")

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAY_END = dtime(18, 30)  # the trading day's end, Prague (sessions.WINDOWS)
OFF_SESSION_INTERVAL_MIN = 30  # open trades are still tracked, slowly


def _day_end_utc(now: datetime) -> datetime:
    local = to_prague(now)
    end = PRAGUE.localize(datetime.combine(local.date(), DAY_END), is_dst=None)
    if end <= local:
        end = PRAGUE.localize(
            datetime.combine(local.date() + timedelta(days=1), DAY_END), is_dst=None
        )
    return end.astimezone(timezone.utc)


def _next_block_end_utc(now: datetime) -> datetime:
    """The end of the current block, or of the next one when off session."""
    end = session_end_utc(now)
    if end is not None:
        return end
    return _day_end_utc(now)


def valid_until(valid_for: str, now: datetime) -> datetime:
    return _next_block_end_utc(now) if valid_for == "session" else _day_end_utc(now)


def _seconds_until_next_slot(interval_minutes: int, offset_s: int) -> float:
    now = datetime.now(tz=timezone.utc)
    seconds_into_hour = now.minute * 60 + now.second + now.microsecond / 1e6
    slot = interval_minutes * 60
    wait = slot - (seconds_into_hour % slot) + offset_s
    return wait


class OpusWatcher:
    def __init__(self):
        set_language(settings.smc.language)
        token = settings.opus.bot_token
        chat_id = settings.opus.chat_id or settings.telegram.chat_id
        if not token:
            raise RuntimeError("OPUS_BOT_TOKEN is not configured")
        if not chat_id:
            raise RuntimeError("Set OPUS_CHAT_ID (or TELEGRAM_CHAT_ID)")
        self.db = Database(settings.opus.db_file)
        self.store = PlanStore(self.db)
        self.pairs: List[str] = [
            p for p in settings.opus.default_pairs() if p in INSTRUMENTS
        ]
        self.notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)
        self.news = (
            NewsCalendar(
                before_minutes=settings.smc.news_blackout_before_min,
                after_minutes=settings.smc.news_blackout_after_min,
            )
            if settings.smc.news_enabled else None
        )
        self.analyst = OpusAnalyst(
            api_key=settings.anthropic.api_key,
            model=settings.opus.model,
            effort=settings.opus.effort,
            timeout=settings.opus.timeout_s,
            fallbacks=settings.opus.fallbacks,
        )
        self.bot = OpusCommandBot(
            bot_token=token,
            owner_chat_id=chat_id,
            pairs=self.pairs,
            on_plan=self.on_plan,
            status_text=self.status_text,
            journal_text=self.journal_text,
            news_text=self.news_text,
        )
        self.last_data: Dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- data

    def _build_fetcher(self, instrument: Instrument):
        return build_fetcher(instrument)

    async def _fetch_all(self, instrument: Instrument) -> Optional[dict]:
        try:
            data = await self._build_fetcher(instrument).fetch_all_timeframes(
                force_fresh=True
            )
        except (DataFetchError, ConfigurationError) as e:
            logger.warning("Fetch failed", pair=instrument.key, error=str(e))
            await self.notifier.send(
                format_data_error(instrument.key, redact_secrets(str(e))[:200])
            )
            return None
        except Exception as e:
            logger.warning("Fetch failed", pair=instrument.key, error=str(e))
            return None
        if not data.get("m5") or not data.get("h1") or not data.get("h4"):
            return None
        self.last_data[instrument.key] = data
        return data

    async def _fetch_m5(self, instrument: Instrument):
        try:
            return await self._build_fetcher(instrument).fetch_candles("5m", 400)
        except Exception as e:
            logger.warning("M5 fetch failed", pair=instrument.key, error=str(e))
            return None

    # ---------------------------------------------------------------- news

    async def _refresh_news(self) -> None:
        if self.news is not None:
            try:
                await self.news.refresh_if_stale()
            except Exception as e:
                logger.warning("News refresh failed", error=str(e))

    def _news_for(self, instrument: Instrument, now: datetime):
        if self.news is None:
            return [], None
        currencies = relevant_currencies(instrument)
        return (
            self.news.todays_events(currencies, now),
            self.news.blackout(currencies, now),
        )

    # ------------------------------------------------------------ decision

    async def _decide(
        self, key: str, trigger: str = "plan", previous: Optional[OpusPlan] = None,
    ) -> Optional[OpusPlan]:
        """Fetch, brief, ask, check, store, send. Returns the new plan or None."""
        instrument = get_instrument(key)
        await self._refresh_news()
        data = await self._fetch_all(instrument)
        if data is None:
            return None
        now = datetime.now(tz=timezone.utc)
        todays, blackout = self._news_for(instrument, now)
        session = active_session(now, require_weekday=instrument.source == "forex")
        price = data["m5"][-1].close
        before = self.news.before if self.news else timedelta(minutes=60)
        after = self.news.after if self.news else timedelta(minutes=15)
        brief = build_brief(
            instrument, data, now,
            todays_news=todays, blackout=blackout, news_before=before, news_after=after,
            orders_today=self.store.orders_today(key, now),
            min_rr=settings.opus.min_rr,
            counts=(settings.opus.h4_candles, settings.opus.h1_candles, settings.opus.m5_candles),
            previous=previous, event=None if trigger == "plan" else trigger,
        )
        images = []
        for tf, candles, back in (("H1", data["h1"], 150), ("M5", data["m5"], 200)):
            try:
                png = render_context_chart(candles, key, tf, instrument, back)
                if png:
                    images.append(png)
            except Exception as e:  # a chart must never block the read
                logger.warning("Context chart failed", pair=key, tf=tf, error=str(e))
        limits = Limits(
            price=price,
            min_rr=settings.opus.min_rr,
            session_open=session is not None,
            blackout_until=(
                to_prague(blackout.time + after).strftime("%H:%M") if blackout else None
            ),
            decimals=instrument.price_decimals,
        )
        outcome = await self.analyst.decide(brief, images, limits)
        self.store.note_call(key, now)
        if outcome.decision is None:
            await self.notifier.send(format_ai_failure(key, outcome.error))
            self.store.save()
            return None
        decision = outcome.decision
        as_of = to_prague(data["m5"][-1].timestamp).strftime("%d.%m %H:%M")
        plan = plan_from_decision(
            key, decision, now, price, valid_until(decision.valid_for, now), as_of,
            trigger=trigger,
        )
        self.store.put_plan(plan)
        if plan.action == "market":
            self.store.add_trade(trade_from_plan(plan, now, "market"))
        warning = news_warning_line(todays, before, after, plan) if todays else None
        text = format_plan_card(plan, instrument, warning)
        plan.message_id = await self.notifier.send(text)
        self.store.save()
        try:
            png = render_plan_chart(plan, data["m5"], instrument)
            if png:
                await self.notifier.send_photo(png, reply_to=plan.message_id)
        except Exception as e:
            logger.warning("Plan chart failed", pair=key, error=str(e))
        logger.info(
            "Opus plan sent", pair=key, action=plan.action, direction=plan.direction,
            entry=plan.entry, attempts=outcome.attempts, trigger=trigger,
            downgraded_from=plan.downgraded_from,
        )
        return plan

    async def on_plan(self, key: str) -> None:
        async with self._lock:
            try:
                await self._decide(key)
            except Exception as e:
                logger.error("Plan failed", pair=key, error=str(e), exc_info=True)
                await self.notifier.send(format_ai_failure(key, str(e)[:120]))

    # ------------------------------------------------------------- monitor

    def _may_call(self, key: str, now: datetime) -> Optional[str]:
        """None when an event may trigger a read; else the reason it may not."""
        if not self.analyst.enabled:
            return t("no ANTHROPIC_API_KEY")
        cap = settings.opus.max_event_calls_per_day
        calls = self.store.event_calls_today(key, now)
        if cap and calls >= cap:
            return budget_reason(calls, cap)
        since = self.store.minutes_since_call(key, now)
        if since is not None and since < settings.opus.event_cooldown_min:
            return cooldown_reason(int(since))
        return None

    async def _handle_event(
        self, plan: OpusPlan, event: str, price: float, now: datetime,
    ) -> None:
        instrument = get_instrument(plan.pair)
        if event == EV_EXPIRED:
            await self.notifier.send(format_expired(plan))
            return
        if event == planmod.EV_FILLED:
            # tracked from the fill candle, not from this tick — TP/SL in the
            # fill candle itself count (SL wins there, as everywhere)
            filled_at = datetime.fromisoformat(plan.resolved_at) if plan.resolved_at else now
            self.store.add_trade(trade_from_plan(plan, filled_at, "limit"))
            logger.info("Limit filled", pair=plan.pair, entry=plan.entry)
            return
        reason = self._may_call(plan.pair, now)
        if reason is not None:
            await self.notifier.send(
                format_event_without_read(plan, event, price, instrument, reason)
            )
            return
        self.store.bump_event_calls(plan.pair, now)
        await self._decide(plan.pair, trigger=event, previous=plan)

    async def monitor_tick(self) -> None:
        async with self._lock:
            now = datetime.now(tz=timezone.utc)
            block = session_block(now)
            new_block = block is not None and block != self.store.last_block
            if block is not None:
                self.store.last_block = block
            for key in self.pairs:
                instrument = get_instrument(key)
                plan = self.store.plans.get(key)
                trades = self.store.open_trades(key)
                in_session = active_session(
                    now, require_weekday=instrument.source == "forex"
                ) is not None
                live = plan is not None and plan.live
                if not live and not trades:
                    continue
                if not in_session and not trades:
                    # nothing can fill off session; the validity clock still runs
                    if plan is not None:
                        for ev in advance_plan(plan, [], now):
                            await self._handle_event(plan, ev.kind, ev.price, now)
                    continue
                m5 = await self._fetch_m5(instrument)
                if not m5:
                    continue
                for trade in trades:
                    status = advance_trade(trade, m5, now)
                    if status:
                        logger.info("Trade resolved", pair=key, status=status,
                                    result_r=trade.result_r)
                if live:
                    events = advance_plan(plan, m5, now)
                    for ev in events:
                        await self._handle_event(plan, ev.kind, ev.price, now)
                    plan = self.store.plans.get(key)
                    if (
                        new_block and in_session and plan is not None and plan.live
                        and not events
                        and session_block(datetime.fromisoformat(plan.created_at)) != block
                        and EV_SESSION_OPENED not in plan.events
                    ):
                        plan.events.append(EV_SESSION_OPENED)
                        await self._handle_event(plan, EV_SESSION_OPENED, m5[-1].close, now)
            self.store.save()

    async def scheduler_loop(self) -> None:
        logger.info("Opus bot started", pairs=self.pairs, model=settings.opus.model)
        while True:
            try:
                await self.monitor_tick()
            except Exception as e:
                logger.error("Monitor tick failed", error=str(e), exc_info=True)
            now = datetime.now(tz=timezone.utc)
            interval = (
                settings.opus.monitor_interval_minutes
                if active_session(now) else OFF_SESSION_INTERVAL_MIN
            )
            await asyncio.sleep(
                _seconds_until_next_slot(interval, settings.opus.tick_offset_s)
            )

    async def run_forever(self) -> None:
        await asyncio.gather(self.scheduler_loop(), self.bot.run())

    # ------------------------------------------------------------- commands

    def status_text(self) -> str:
        return format_status(
            self.pairs, self.store.plans, self.store.trades,
            {p: get_instrument(p) for p in self.pairs},
        )

    def journal_text(self) -> str:
        return format_journal(self.store.trades, self.store.history)

    def news_text(self) -> str:
        if self.news is None:
            return t("News filter is off (SMC_NEWS_ENABLED=false)")
        return self.news.digest_text(self.pairs)


async def run_plan_once(pair: str) -> None:
    watcher = OpusWatcher()
    plan = await watcher._decide(pair.upper())
    print(plan.to_dict() if plan else f"no plan: {watcher.analyst.last_error}")


async def run_telegram_test() -> None:
    watcher = OpusWatcher()
    ok = await watcher.notifier.send("🧪 <b>Opus bot TEST</b> — Telegram wiring works.")
    print(f"Telegram: {'sent' if ok else 'FAILED'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Opus analyst bot")
    parser.add_argument("--plan", metavar="PAIR", help="one decision for PAIR and exit")
    parser.add_argument("--test-telegram", action="store_true")
    args = parser.parse_args()
    try:
        if args.test_telegram:
            asyncio.run(run_telegram_test())
        elif args.plan:
            asyncio.run(run_plan_once(args.plan))
        else:
            asyncio.run(OpusWatcher().run_forever())
    except KeyboardInterrupt:
        sys.exit(0)
