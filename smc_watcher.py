"""SMC strategy watcher — Triple Sync + Imbalance for multiple pairs.

The only service of this project. Every 15 minutes (aligned to :00/:15/:30/:45)
it runs the strategy for each enabled pair and sends a Telegram message only
when a valid setup is found (🚨 urgent alert). Checks without a setup are
logged; set SMC_NOTIFY_NO_SETUP=true to also receive 15-min heartbeats.

Pairs are chosen at runtime via Telegram commands (/pairs) handled by a
long-polling loop in the same process. ETHUSD data comes from Binance;
forex pairs (USDJPY, EURUSD, GBPUSD, USDCAD) come from Twelve Data when
TWELVEDATA_API_KEY is set, or from OANDA v20 when OANDA_API_TOKEN is set —
a forex key is required, there is no keyless fallback.

Usage:
    python smc_watcher.py                  # run forever (scheduler + bot)
    python smc_watcher.py --once           # single check of enabled pairs
    python smc_watcher.py --test-telegram  # verify Telegram wiring
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import structlog

from app.core.config import settings
from app.core.exceptions import ConfigurationError, DataFetchError
from app.core.logging import configure_logging
from app.services.smc.data import BinanceDataFetcher
from app.services.smc.db import Database, migrate_legacy_json
from app.services.smc.engine import TripleSyncEngine, trends_disagree
from app.services.smc.instruments import INSTRUMENTS, Instrument, get_instrument
from app.services.smc.journal import SignalJournal
from app.services.smc.liquidity import find_liquidity, nearest_liquidity
from app.services.smc.news import NewsCalendar, relevant_currencies
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc import pd as pd_module
from app.services.smc.notifier import (
    TelegramNotifier,
    escape_html,
    format_no_setup,
    format_plan,
    format_plan_summary,
    format_quiet_setup,
    format_result,
    format_pd_alert,
    format_zone_alert,
    plan_summary_keyboard,
    redact_secrets,
    zone_alert_keyboard,
)
from app.services.smc.planbook import (
    PlanBook, PlanEntry, describe_plan_changes, plan_fingerprint,
    plan_snapshot,
)
from app.services.smc.oanda import OandaDataFetcher
from app.services.smc.twelvedata import TwelveDataFetcher
from app.services.smc.sessions import (
    PRAGUE, active_session, block_mute_deadline, prague_hhmm, session_block,
    to_prague,
)
from app.services.smc.state import WatcherState
from app.services.smc.telegram_bot import TelegramCommandBot
from app.services.smc.trade_journal import TradeJournal

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

# Placeholder handed to `_build_engine` for a pass that only calls the pure
# `evaluate()` (the D23 counter-trend pass). Building a real fetcher there
# would re-resolve the forex API key for nothing.
_NO_FETCH = object()


def _forex_source() -> str:
    """Resolve the configured forex source, honouring 'auto'.

    There is no keyless fallback any more (the previous keyless forex feed
    was removed — its data was bad enough to have produced a wrong strategy
    conclusion during replay validation). A forex pair with no usable key is
    a configuration error, not a silent downgrade: it must fail clearly here
    so the caller can warn the owner instead of quietly returning no data.
    """
    source = settings.smc.forex_source.strip().lower()
    if source == "auto":
        if settings.twelvedata.api_key:
            return "twelvedata"
        if settings.oanda.api_token:
            return "oanda"
        raise ConfigurationError(
            "No forex data source configured: set TWELVEDATA_API_KEY or "
            "OANDA_API_TOKEN (SMC_FOREX_SOURCE=auto has nothing to pick "
            "from — the keyless forex fallback has been removed)."
        )
    if source == "twelvedata":
        if not settings.twelvedata.api_key:
            raise ConfigurationError(
                "SMC_FOREX_SOURCE=twelvedata but TWELVEDATA_API_KEY is not set."
            )
        return "twelvedata"
    if source == "oanda":
        if not settings.oanda.api_token:
            raise ConfigurationError(
                "SMC_FOREX_SOURCE=oanda but OANDA_API_TOKEN is not set."
            )
        return "oanda"
    raise ConfigurationError(
        f"Unknown SMC_FOREX_SOURCE: {source!r} (use 'auto', 'twelvedata' or "
        "'oanda')."
    )


def _build_fetcher(instrument: Instrument):
    if instrument.source == "crypto":
        # ETHUSD stays on Binance: unlimited, deep history, funding rate.
        return BinanceDataFetcher(instrument.source_symbol)
    source = _forex_source()
    if source == "twelvedata":
        return TwelveDataFetcher(instrument.key, settings.twelvedata.api_key)
    return OandaDataFetcher(
        symbol=instrument.source_symbol,
        api_token=settings.oanda.api_token,
        environment=settings.oanda.environment,
    )


def _build_engine(
    instrument: Instrument, profile=None, fetcher=None
) -> TripleSyncEngine:
    from app.services.smc.profiles import CONSERVATIVE

    if fetcher is None:
        fetcher = _build_fetcher(instrument)
    smc = settings.smc
    return TripleSyncEngine(
        instrument=instrument,
        min_rr=smc.min_rr,
        max_entry_gap_r=smc.max_entry_gap_r,
        tp1_r=smc.tp1_r,
        runner_r=smc.runner_r,
        pd_basis=smc.pd_basis,
        require_imbalance=smc.require_imbalance,
        risk_pct=smc.risk_pct,
        deposit=smc.deposit,
        enforce_sessions=smc.enforce_sessions,
        profile=profile or CONSERVATIVE,
        fetcher=fetcher,
    )


def _setup_fingerprint(result: AnalysisResult) -> str:
    """One announcement per zone per session block.

    Detector mode (spec 2026-08-06 §3): with the RR gate gone, every fresh
    imbalance inside the same H1 zone would otherwise re-alert. A second
    imbalance in the same zone is the same trading idea — the owner has
    already placed his order or decided not to. The entry price is therefore
    no longer part of the key; the zone is.
    """
    setup = result.setup
    day = result.checked_at.strftime("%Y-%m-%d")
    zone = result.h1_zone
    # An approved result always carries its zone (Rule 2 runs before the
    # trigger). The fallback guards that one invariant only — it keeps a
    # zoneless result from collapsing the whole session onto a single key. It
    # is no protection against a missing `setup`: both branches dereference
    # it, as does the line below.
    anchor = f"{zone.bottom}-{zone.top}" if zone else f"entry{setup.entry}"
    return (
        f"{result.symbol}:{setup.direction.value}:{anchor}:"
        f"{result.session_name}:{day}"
    )


# Substrings in a DataFetchError/ConfigurationError detail that actually
# point at credentials: the provider names the key (Twelve Data's "**apikey**
# parameter is invalid", the "TWELVEDATA_API_KEY is not set" config error) or
# the HTTP layer says unauthorized. A 429, a timeout or a 5xx matches none of
# them — those are not a reason to rotate a working key.
_AUTH_FAILURE_MARKERS = (
    "apikey", "api key", "api_key", "api token", "api_token",
    "unauthorized", "forbidden", "authentication", "credential",
    " 401", "(401", "401)", " 403", "(403", "403)",
)


def _looks_like_auth_failure(detail: str) -> bool:
    """True when a data-source failure reads like a credentials problem."""
    text = detail.lower()
    return any(marker in text for marker in _AUTH_FAILURE_MARKERS)


def _card_footer(signal: Dict) -> str:
    """Status history appended to the alert message (live setup card)."""

    def hhmm(iso: Optional[str]) -> str:
        if not iso:
            return ""
        # Defense in depth (review 2026-08-11 audit): filled_at/resolved_at
        # are normally set fresh, in-process, this cycle (always aware) —
        # but a corrupted legacy-imported row could carry garbage here too,
        # and this runs on the cycle path (_track_journal ->
        # _handle_journal_events). to_prague already tolerates naive
        # datetimes; only outright unparseable values need guarding.
        try:
            local = to_prague(datetime.fromisoformat(iso))
        except (TypeError, ValueError):
            return ""
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
    elif status == "tp1_be":
        # Phase 2 hybrid lifecycle: TP1 banked, the rest closed at
        # break-even (or timed out with TP1 already banked — same result).
        r = signal.get("result_r")
        lines.append(
            f"🎯 <b>TP1 → BE</b>{when}"
            + (f" — +{r:.1f}R" if r is not None else "")
        )
    elif status == "tp1_runner":
        r = signal.get("result_r")
        lines.append(
            f"🏆 <b>RUNNER HIT</b>{when}"
            + (f" — +{r:.1f}R" if r is not None else "")
        )
    elif status == "expired":
        lines.append("🗑 Expired unfilled — order dies with its session (Rule 10)")
    elif status == "timeout":
        lines.append("⌛ Timed out — untracked after 5 days")
    elif status == "open":
        # Detector mode: a setup with no structural objective carries no
        # take-profit, and `evaluate_signal` can only ever resolve it as SL
        # or timeout. Promising to track a TP that does not exist would be a
        # lie on the live card.
        lines.append(
            "⏳ Position live — tracking TP/SL"
            if signal.get("take_profit") is not None
            else "⏳ Position live — tracking SL (no objective recorded)"
        )
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
        self.trade_journal = TradeJournal(self.db)
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
            pd_text=self.pd_text,
            on_trade_mark=self.mark_trade,
            on_plan=self.on_plan,
            on_stored_plan=self.on_stored_plan,
            on_zone_mute=self.mark_zone_mute,
            trade_journal=self.trade_journal,
        )
        self.last_results: Dict[str, AnalysisResult] = {}
        self.planbook = PlanBook()
        # Serializes run_cycle/on_plan/on_stored_plan bodies (see
        # _get_cycle_lock): the dedup fingerprint (state.last_setup) is only
        # written after a successful alert send, so two of these racing —
        # the scheduler tick and an owner /check, or a slow /plan ALL —
        # could otherwise both pass the dedup check before either had
        # written it, producing a second alert and a second journal row for
        # the same setup (review 2026-08-11).
        self._cycle_lock = asyncio.Lock()
        # apply the env default on the very first start (DB wins afterwards)
        if self.db.kv_get("pairs") is None:
            env_pairs = [p for p in settings.smc.default_pairs() if p in INSTRUMENTS]
            if env_pairs:
                self.state.pairs = env_pairs
                self.state.save()
        # Startup visibility: a forex pair enabled with no usable key would
        # otherwise fail silently cycle after cycle. This is a log line, not
        # a crash — ETHUSD (Binance, keyless) must keep working regardless.
        # The per-cycle Telegram warning below is what actually reaches the
        # owner; this just puts the same fact in front of anyone reading logs.
        if any(get_instrument(p).source == "forex" for p in self.state.pairs):
            try:
                _forex_source()
            except ConfigurationError as e:
                logger.error("Forex data source misconfigured at startup", error=str(e))

    # ------------------------------------------------------------- one cycle

    def _build_fetcher(self, instrument: Instrument):
        """Instance-level indirection over the module fetcher factory, so a
        fetch failure (or a missing key) can be simulated per-Watcher in
        tests without touching global settings."""
        return _build_fetcher(instrument)

    def _get_cycle_lock(self) -> asyncio.Lock:
        """The one lock shared by run_cycle/on_plan/on_stored_plan.

        Lazily created and cached so a Watcher built via `Watcher.__new__`
        (the stub pattern most of this test suite uses to bypass __init__'s
        network-touching setup) still gets a working lock on first use
        instead of an AttributeError — the real Watcher() constructor
        already creates it eagerly, so this branch is a test-only fallback,
        never a second lock instance in production.
        """
        lock = self.__dict__.get("_cycle_lock")
        if lock is None:
            lock = asyncio.Lock()
            self._cycle_lock = lock
        return lock

    def _news_warned_bad_seen(self) -> set:
        """Log-dedup set for `_rule_04_warnings`'s prune of poisoned
        `news_warned` timestamps. Lazily created (same pattern as
        `_get_cycle_lock`) so a Watcher built via `Watcher.__new__` in
        tests still works without calling `__init__`."""
        seen = self.__dict__.get("_news_warned_bad")
        if seen is None:
            seen = set()
            self._news_warned_bad = seen
        return seen

    def _pair_cooldown_bad_seen(self) -> set:
        """Log-dedup set for `_purge_expired_cooldowns`'s poisoned
        `pair_cooldown` entries. Same lazy-init pattern as above."""
        seen = self.__dict__.get("_pair_cooldown_bad")
        if seen is None:
            seen = set()
            self._pair_cooldown_bad = seen
        return seen

    async def check_pair(self, key: str) -> Tuple[str, Optional[AnalysisResult]]:
        """Analyze one pair. Returns (heartbeat line, result or None)."""
        instrument = get_instrument(key)
        from app.services.smc.profiles import get_profile

        profile = get_profile(
            self.state.pair_profile.get(key, settings.smc.default_profile)
        )
        try:
            fetcher = self._build_fetcher(instrument)
            engine = _build_engine(instrument, profile, fetcher=fetcher)
            result = await engine.analyze()
        except (DataFetchError, ConfigurationError) as e:
            logger.error("Pair check failed", pair=key, error=str(e))
            await self._warn_data_source_failure(key, str(e))
            # This line lands in the cycle summary, which /check sends to
            # Telegram — redact before it becomes a message (see below).
            detail = escape_html(redact_secrets(str(e)))
            return f"⚠️ {key}: data error ({detail})", None
        except Exception as e:
            logger.error("Pair check failed", pair=key, error=str(e))
            detail = escape_html(redact_secrets(str(e)))
            return f"⚠️ {key}: data error ({detail})", None
        self.last_results[key] = result
        logger.info(
            "SMC check finished",
            pair=key,
            verdict=result.verdict.value,
            price=result.price,
            reasons=result.reasons,
        )
        return format_no_setup(result), result

    def _counter_trend_result(
        self, key: str, result: Optional[AnalysisResult]
    ) -> Optional[AnalysisResult]:
        """The H1-side setup when H4 and H1 point opposite ways (D23).

        Owner decision 2026-08-31, from the ETH case: he reads a reversal on
        the lower timeframes and waits to go long while Rule 1, reading H4,
        is looking the other way — so the bot stays silent on the very trade
        he is watching. This runs a SECOND pure pass on the candles the
        primary pass already fetched (no extra API call) and hands back the
        H1-direction setup when one has fully formed.

        It adds, it never replaces: the primary H4-direction result is
        returned and alerted exactly as before, and this one carries
        `direction_source="h1_counter"`, so the header says it runs against
        H4 and `trends_disagree` (D6) denies it the ⭐. Returns None unless
        a setup actually completed — a counter-trend WATCH is noise.
        """
        if result is None or result.verdict == Verdict.OFF_SESSION:
            return None
        if result.h1_trend is None or not trends_disagree(
            result.h4_trend, result.h1_trend
        ):
            return None
        if not (result.m5_candles and result.h4_candles and result.h1_candles):
            return None
        direction = (
            Direction.LONG if result.h1_trend == Trend.UP else Direction.SHORT
        )
        if result.setup is not None and result.setup.direction == direction:
            return None  # the primary pass is already this side
        from app.services.smc.profiles import get_profile

        instrument = get_instrument(key)
        profile = get_profile(
            self.state.pair_profile.get(key, settings.smc.default_profile)
        )
        counter = AnalysisResult(
            symbol=result.symbol,
            verdict=Verdict.SKIP,
            checked_at=result.checked_at,
            price_decimals=result.price_decimals,
        )
        counter.price = result.price
        counter.session_name = result.session_name
        counter.funding_rate = result.funding_rate
        counter.m5_candles = result.m5_candles
        counter.h4_candles = result.h4_candles
        counter.h1_candles = result.h1_candles
        try:
            # `evaluate` is pure and never touches the fetcher, so the engine
            # is built with a placeholder rather than constructing a second
            # live client (which for forex would re-resolve the API key).
            engine = _build_engine(instrument, profile, fetcher=_NO_FETCH)
            counter = engine.evaluate(
                h4=result.h4_candles,
                h1=result.h1_candles,
                m5=result.m5_candles,
                result=counter,
                force_direction=direction,
            )
        except Exception as e:
            logger.warning(
                "Counter-trend pass failed", pair=key, error=str(e), exc_info=True
            )
            return None
        if counter.verdict not in APPROVED:
            return None
        logger.info(
            "Counter-trend setup found",
            pair=key, direction=direction.value,
            h4=result.h4_trend.value, h1=result.h1_trend.value,
        )
        return counter

    async def _warn_data_source_failure(self, key: str, detail: str) -> None:
        """A forex fetch failure must not look like a quiet market — the
        owner rotates his TwelveData key regularly, and an expired key
        produces no data, which is indistinguishable from "nothing to alert
        on" unless something says otherwise. Throttled to one warning per
        pair per hour (mirrors the news_warned dedup pattern) so a source
        that is down all day does not spam every cycle.
        """
        # A fetcher error detail can carry the credential that caused it — a
        # request URL with `apikey=...`, an echoed Authorization header. Logs
        # were hardened against this in bcd5728; Telegram is worse, because
        # the history is durable and syncs to every device. Scrub here, at the
        # point where the detail becomes a message, so no future fetcher can
        # reopen the hole through this path.
        detail = redact_secrets(detail)
        now = datetime.now(tz=timezone.utc)
        last = self.state.source_warned.get(key)
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(hours=1):
                    return
            except (ValueError, TypeError):
                pass
        # Binance (ETHUSD) is keyless — telling the owner to check an API
        # key that does not exist is wrong. And on a forex pair the failure
        # is just as often a rate limit or a transient HTTP error, where
        # "your key may have expired" sends him to rotate a working key for
        # nothing: the hint is only shown when the detail actually reads
        # like an auth problem.
        is_forex = get_instrument(key).source == "forex"
        hint = (
            " Check your API key (it may have expired)."
            if is_forex and _looks_like_auth_failure(detail) else ""
        )
        message_id = await self.notifier.send(
            f"⚠️ <b>{key}</b>: data source failed — {escape_html(detail)}.{hint}"
        )
        if not message_id:
            # send() swallows Telegram/network failures and returns None —
            # do not start the hour-long quiet window on a warning that
            # never actually reached the owner.
            return
        self.state.source_warned[key] = now.isoformat()
        self.state.save()

    async def run_cycle(self) -> str:
        """Run the strategy for all enabled pairs; send alerts; return summary.

        The whole body runs under `_get_cycle_lock()`: the dedup fingerprint
        (`state.last_setup`) is only written after a successful alert send,
        so a scheduler tick racing an owner /check (both awaiting this same
        coroutine, interleaving at every `await`) could otherwise both read
        the still-unset fingerprint and both send the alert — one lock per
        Watcher, shared with on_plan/on_stored_plan, closes that window.
        """
        async with self._get_cycle_lock():
            if self.state.paused:
                return "⏸ Bot is paused — /resume to continue"
            if not self.state.pairs:
                return "⚠️ No active pairs — enable at least one via /pairs"

            if self.news:
                await self.news.refresh_if_stale()
                await self._rule_04_warnings()
            await self._morning_briefing()
            await self._maybe_auto_plan()
            self._purge_expired_cooldowns()

            heartbeat_lines: List[str] = []
            approved: List[AnalysisResult] = []

            for key in list(self.state.pairs):
                blackout = self._news_blackout(key)
                if blackout:
                    heartbeat_lines.append(blackout)
                    continue
                line, result = await self.check_pair(key)
                self._recompute_plan(key, result)
                # D23 (owner decision 2026-08-31): when H4 and H1 disagree the
                # H1-side setup is announced ALONGSIDE the primary one, never
                # instead of it. Both go through the same dedup, discipline
                # and alert path below — `_setup_fingerprint` already keys on
                # direction, so the two can never collide.
                candidates = (
                    [result] if result and result.verdict in APPROVED else []
                )
                counter = self._counter_trend_result(key, result)
                if counter is not None:
                    candidates.append(counter)
                for result in candidates:
                    fingerprint = _setup_fingerprint(result)
                    if self._dedup_store(result).get(key) == fingerprint:
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
                        heartbeat_lines.append(
                            f"⛔ {key}: alert suppressed — {block}"
                        )
                        continue
                    cooldown = self._cooldown_left(key)
                    if cooldown:
                        logger.info(
                            "Alert muted (taken cooldown)", pair=key, left=cooldown
                        )
                        heartbeat_lines.append(
                            f"🔕 {key}: setup found but muted — you took a trade "
                            f"here, {cooldown} left"
                        )
                        continue
                    approved.append(result)
                    # Known ahead of the call: `_alert_send_suppressed` is a
                    # pure function of state.notify_level + the setup's tier,
                    # so whether this attempt is a deliberate mute (not a
                    # failure) is decided before `_send_alert` ever touches
                    # the network. An exception below means something broke
                    # instead — that always reports as a real failure, never
                    # as "suppressed", even if notify_level happens to be
                    # muted at the time.
                    muted_by_level = self._alert_send_suppressed(
                        result.setup.tier_star
                    )
                    try:
                        sent = await self._send_alert(key, result, fingerprint)
                    except Exception as e:
                        # Isolate this pair's failure the same way
                        # check_pair() and _track_journal()'s per-pair loop
                        # already isolate theirs: an uncaught exception here
                        # (most likely from format_result) would otherwise
                        # escape this `for key` loop and silently drop every
                        # remaining pair's check this cycle, plus skip
                        # _track_journal() below for all of them — not just
                        # the one pair that actually failed.
                        logger.error(
                            "Alert send failed", pair=key, error=str(e),
                            exc_info=True,
                        )
                        sent = False
                        muted_by_level = False
                    if sent:
                        heartbeat_lines.append(
                            f"🚨 {key}: SETUP FOUND — details above!"
                        )
                    elif muted_by_level:
                        heartbeat_lines.append(
                            f"🔇 {key}: setup found — alert suppressed by "
                            "notification level"
                        )
                    else:
                        heartbeat_lines.append(
                            f"⚠️ {key}: setup found but the alert failed to send"
                        )
                if not candidates:
                    zone_alerted = await self._maybe_plan_zone_alert(key, result)
                    if not zone_alerted:
                        await self._maybe_pd_alert(key, result)
                    heartbeat_lines.append(line)

            for warning in _correlation_warnings(approved):
                await self.notifier.send(warning)

            await self._track_journal()
            await self._maybe_edit_plan_summary()

            time_str = to_prague(datetime.now(tz=timezone.utc)).strftime("%H:%M")
            summary = (
                f"🔍 <b>Check {time_str} Prague</b>\n" + "\n".join(heartbeat_lines)
            )
            logger.info("Cycle summary", summary=" | ".join(heartbeat_lines))
            # By default only setup alerts go to Telegram; heartbeat is opt-in.
            if settings.smc.notify_no_setup and not approved:
                await self.notifier.send(summary)
            return summary

    # ---------------------------------------------------------------- alerts

    async def _send_alert(
        self, key: str, result: AnalysisResult, fingerprint: str
    ) -> bool:
        """Two-tier routing (Phase 2 sniper redesign, owner decision
        2026-08-12): `result.setup.tier_star` picks the presentation.

        ⭐ (star) — a setup that cleared room/sweep/premium-discount/
        staleness (sniper.classify) — gets today's full path unchanged:
        message with Took/Skipped buttons, pinned, chart PNG attached.
        Every other completed setup ("regular") is still announced —
        detector mode (CLAUDE.md) never suppresses a formed setup — but as
        one short plain message: no pin, no chart, no buttons.

        Both tiers share the same dedup fingerprint and the same journal
        recording (`journal.record` reads `setup.tier_star` itself, Task 3),
        so a setup that flickers between tiers within one fingerprint still
        alerts exactly once.

        Returns True once the alert actually reached Telegram, False if
        `notifier.send` failed (it swallows Telegram/network errors and
        returns None rather than raising) — the caller (`run_cycle`) uses
        this to report the pair honestly in the heartbeat instead of
        claiming "SETUP FOUND — details above!" for a message nobody
        received.

        A failure anywhere in here (most plausibly `format_result` raising)
        is caught by the caller, not here — see the `try/except` around this
        call in `run_cycle`. That isolates one pair's failure the same way
        `check_pair()` and `_track_journal()`'s own per-pair loop already
        isolate theirs, instead of letting it silently drop every remaining
        pair in this cycle's `for key in ...` loop.
        """
        if result.setup.tier_star:
            return await self._send_star_alert(key, result, fingerprint)
        return await self._send_quiet_alert(key, result, fingerprint)

    def _dedup_store(self, result: AnalysisResult) -> Dict[str, str]:
        """Which per-pair fingerprint slot this setup dedups in.

        D23: the counter-H4 track (direction_source "h1_counter") keeps its
        own slot. One slot shared by both directions would let each new
        alert clear the other's fingerprint, so a pair with a live setup on
        each side would re-announce both every cycle.
        """
        if getattr(result, "direction_source", "") == "h1_counter":
            return self.state.last_counter_setup
        return self.state.last_setup

    def _alert_send_suppressed(self, tier_star: bool) -> bool:
        """Whether `state.notify_level` (Task 4b, owner decision 2026-08-12)
        blocks the real Telegram send for this tier. "mute" blocks both
        tiers; "star" blocks only the regular (non-⭐) tier; "all" blocks
        nothing. This gates the send only — the caller still records the
        setup in the journal and advances the dedup fingerprint either way,
        so un-muting later does not replay a backlog of stale setups."""
        level = self.state.notify_level
        if level == "mute":
            return True
        if level == "star":
            return not tier_star
        return False

    async def _send_star_alert(
        self, key: str, result: AnalysisResult, fingerprint: str
    ) -> bool:
        """⭐-tier: message with Took/Skipped buttons + setup chart, pinned."""
        # Render first, record second. The caller isolates a failure in here
        # (most plausibly `format_result`) per pair — but a signal recorded
        # before that failure survives as a `pending` row with no message and
        # no stored fingerprint, so the next cycle records a second row for
        # the same setup and the journal grows one row per cycle, silently.
        # Nothing between here and `attach_message` reads the journal.
        text = format_result(result, in_plan=self._plan_provenance(key, result))
        signal = self.journal.record(result)
        if self._alert_send_suppressed(tier_star=True):
            # notify_level says "mute" — record + dedup still happen, only
            # the Telegram send (and everything downstream of it: pin,
            # chart, live card) is skipped.
            self._dedup_store(result)[key] = fingerprint
            self.state.save()
            return False
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Took it", "callback_data": f"take_{signal['id']}"},
                    {"text": "❌ Skipped", "callback_data": f"skip_{signal['id']}"},
                ]
            ]
        }
        message_id = await self.notifier.send(text, reply_markup=keyboard)
        if not message_id:
            # The signal just recorded needed its id for the keyboard above
            # before we knew the send would fail — now that it has, remove
            # it rather than leave an orphan `pending` row with no message
            # and no fingerprint (it would otherwise resolve as `expired`
            # later and pollute /stats).
            self.journal.discard(signal["id"])
            return False
        self._dedup_store(result)[key] = fingerprint
        self.state.save()
        self.journal.attach_message(signal["id"], message_id, text)
        await self.notifier.pin(message_id)
        await self._send_chart(result, message_id)
        return True

    async def _send_quiet_alert(
        self, key: str, result: AnalysisResult, fingerprint: str
    ) -> bool:
        """Non-⭐ ("regular") tier: exactly one plain `send_message`, no pin,
        no chart, no Took/Skipped buttons. Still journal-recorded — the
        signal is not linked to a message (no `attach_message`), so it never
        grows a live card: `_handle_journal_events` only edits signals that
        carry both a `message_id` and `alert_text`, and a quiet signal
        carries neither. That is deliberate, not an oversight — a live card
        would re-add the Took/Skipped keyboard on the next status change
        (`_handle_journal_events`'s `keep_buttons` defaults to True), which
        is exactly the button-free presentation this tier promises.
        """
        text = format_quiet_setup(result)
        signal = self.journal.record(result)
        if self._alert_send_suppressed(tier_star=False):
            # notify_level says "star" or "mute" — record + dedup still
            # happen, only the Telegram send is skipped.
            self._dedup_store(result)[key] = fingerprint
            self.state.save()
            return False
        message_id = await self.notifier.send(text)
        if not message_id:
            self.journal.discard(signal["id"])
            return False
        self._dedup_store(result)[key] = fingerprint
        self.state.save()
        return True

    def _plan_provenance(self, key: str, result: AnalysisResult) -> Optional[bool]:
        """Whether this zone was in the plan the owner read this morning.

        None means no /plan ran today for the pair — the alert then claims no
        provenance at all rather than calling every zone "new".
        """
        if result.h1_zone is None or not self.state.has_plan_today(key):
            return None
        return self.state.zone_was_planned(
            key,
            result.h1_zone.bottom,
            result.h1_zone.top,
            result.setup.direction.value if result.setup else None,
        )

    async def _send_chart(self, result: AnalysisResult, reply_to: int) -> None:
        """Attach the setup chart PNG (must never block the alert).

        Rendering is ~seconds of matplotlib CPU for a 2200x660 PNG — run it
        in a worker thread so the polling loop keeps serving commands while
        it runs, instead of stalling in-loop for the duration.
        """
        try:
            from app.services.smc.chart import render_setup_chart

            png = await asyncio.to_thread(render_setup_chart, result)
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

    async def mark_zone_mute(
        self, key: str, block_id: Optional[str]
    ) -> Optional[str]:
        """🔕 button: silence this pair's zone alerts until the block the
        alert was sent in ends (or tomorrow's open, if that was the day's
        last block). Setup alerts and Rule 0.4 warnings are untouched (D3).

        `block_id` is the block the ALERT belonged to (owner decision
        2026-08-16: the button does exactly what its label says, anchored
        to send time rather than press time). `None` covers a legacy
        callback payload with no block part, sent before this deploy; it
        falls back to the block the press itself falls in.

        Returns the Prague HH:MM deadline, or None when that block has
        already ended — nothing is muted, and the caller tells the owner so.
        """
        key = key.upper()
        block = block_id or session_block(datetime.now(tz=timezone.utc))
        deadline = block_mute_deadline(block) if block else None
        if deadline is None or deadline <= datetime.now(tz=timezone.utc):
            return None
        until = self.state.mute_zone_alerts(key, deadline)
        logger.info("Zone alerts muted", pair=key, until=until)
        return until

    def _cooldown_left(self, key: str) -> Optional[str]:
        """Human 'Nh Mm' remaining on a taken-trade mute, or None if expired,
        absent or poisoned.

        Read-only: no mutation, no `state.save()`. This used to delete the
        expired entry and save on every call — including from `status_text`,
        so a plain /status command performed a DB write (review 2026-08-11,
        MEDIUM). Expired/poisoned cleanup now happens once per cycle in
        `_purge_expired_cooldowns`.
        """
        expiry = self.state.pair_cooldown.get(key)
        if not expiry:
            return None
        now = datetime.now(tz=timezone.utc)
        try:
            remaining = datetime.fromisoformat(expiry) - now
        except (ValueError, TypeError):
            return None
        if remaining.total_seconds() <= 0:
            return None
        total_min = int(remaining.total_seconds() // 60)
        return f"{total_min // 60}h {total_min % 60}m"

    def _purge_expired_cooldowns(self) -> None:
        """Remove `pair_cooldown` entries that have expired, or that are
        poisoned (unparseable, or naive — a legacy JSON import raises
        TypeError on the aware-vs-naive subtraction below). The single
        writer for this dict's cleanup, called once per cycle from
        `run_cycle`; `_cooldown_left` only reads (see its docstring).
        """
        now = datetime.now(tz=timezone.utc)
        bad_seen = self._pair_cooldown_bad_seen()
        survivors: Dict[str, str] = {}
        changed = False
        for key, expiry in self.state.pair_cooldown.items():
            try:
                remaining = datetime.fromisoformat(expiry) - now
            except (ValueError, TypeError):
                changed = True
                if key not in bad_seen:
                    bad_seen.add(key)
                    logger.error(
                        "Dropping unparseable/naive pair_cooldown timestamp",
                        pair=key, value=expiry,
                    )
                continue
            if remaining.total_seconds() <= 0:
                changed = True
                continue
            survivors[key] = expiry
        if changed:
            self.state.pair_cooldown = survivors
            self.state.save()

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
                if event in (
                    "tp", "sl", "expired", "timeout", "tp1_be", "tp1_runner",
                ):
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
        """/plan command: send the Pre-Market Plan for a pair (or ALL).

        Shares `_get_cycle_lock()` with run_cycle/on_stored_plan (see
        run_cycle's docstring) — `/plan ALL` force-fetches every pair fresh
        through the rate limiter and renders a chart each, so it must not
        interleave with a cycle writing the same planbook/state.
        """
        async with self._get_cycle_lock():
            keys = list(self.state.pairs) if key == "ALL" else [key]
            for k in keys:
                if k in INSTRUMENTS:
                    await self._send_pair_plan(k)

    async def on_stored_plan(self, key: str) -> None:
        """aplan_* button: serve the stored current plan instantly.

        Shares `_get_cycle_lock()` with run_cycle/on_plan (see run_cycle's
        docstring).
        """
        async with self._get_cycle_lock():
            keys = list(self.state.pairs) if key == "ALL" else [key]
            for k in keys:
                if k in INSTRUMENTS:
                    await self._send_stored_plan(k)

    async def _send_stored_plan(self, key: str) -> None:
        entry = self.planbook.get(key)
        if entry is None:
            # fresh restart and no cycle yet — build one the normal way
            await self._send_pair_plan(key)
            return
        await self._deliver_plan(key, entry)

    async def _fetch_pair_plan(
        self, key: str, force_fresh: bool = True
    ) -> Optional[PlanEntry]:
        """Fetch candles and (re)build one pair's plan into the planbook.

        Never writes plan_zones — provenance belongs to the callers that
        represent something the owner will actually see (snapshot, /plan)."""
        from app.services.smc.plan import build_plan
        from app.services.smc.profiles import get_profile

        instrument = get_instrument(key)
        try:
            data = await self._build_fetcher(instrument).fetch_all_timeframes(
                force_fresh=force_fresh
            )
        except (DataFetchError, ConfigurationError) as e:
            # same visibility contract as the old _send_pair_plan: a dead
            # source must reach the owner (see _warn_data_source_failure)
            logger.warning("Plan fetch failed", pair=key, error=str(e))
            await self._warn_data_source_failure(key, str(e))
            return None
        except Exception as e:
            logger.warning("Plan fetch failed", pair=key, error=str(e))
            return None
        now = datetime.now(tz=timezone.utc)
        stale = (
            instrument.source == "forex"
            and now - data["m5"][-1].timestamp > timedelta(minutes=30)
        )
        profile = get_profile(
            self.state.pair_profile.get(key, settings.smc.default_profile)
        )
        plan = build_plan(
            instrument, data["h4"], data["h1"], data["m5"],
            min_rr=settings.smc.min_rr, profile=profile, market_closed=stale,
        )
        as_of = to_prague(data["m5"][-1].timestamp).strftime("%H:%M")
        entry = PlanEntry(plan=plan, data=data, as_of=as_of)
        self.planbook.update(key, entry)
        return entry

    def _autoplan_slots(self) -> List[str]:
        """Validated 'HH:MM' Prague slots from SMC_AUTO_PLAN_TIMES."""
        slots = []
        for part in settings.smc.auto_plan_times.split(","):
            part = part.strip()
            try:
                hh, mm = part.split(":")
                hh, mm = int(hh), int(mm)
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    raise ValueError(part)
            except (ValueError, TypeError):
                logger.warning("Ignoring invalid auto-plan slot", slot=part)
                continue
            slots.append(f"{hh:02d}:{mm:02d}")
        return sorted(set(slots))

    async def _maybe_auto_plan(self) -> None:
        """Fire the auto-plan snapshot once per slot per Prague day.

        Only the MOST RECENT due slot builds (a 15:00 boot sends the 13:55
        picture, not the stale morning one); older due slots are consumed
        silently. A slot is marked only after its summary actually reached
        Telegram, so a failed send retries next cycle."""
        if not settings.smc.auto_plan:
            return
        slots = self._autoplan_slots()
        if not slots:
            return
        local = to_prague(datetime.now(tz=timezone.utc))
        today = local.date().isoformat()
        due = [s for s in slots if s <= local.strftime("%H:%M")]
        due = [s for s in due if self.state.auto_plan_sent.get(s) != today]
        if not due:
            return
        slot = max(due)
        sent = await self._auto_plan_snapshot(slot)
        for s in due:
            if s == slot and not sent:
                continue
            self.state.auto_plan_sent[s] = today
        self.state.save()

    async def _auto_plan_snapshot(self, slot: str) -> bool:
        """Build every open pair's plan fresh, remember its zones
        (provenance, spec §4) and send ONE silent summary with buttons."""
        local = to_prague(datetime.now(tz=timezone.utc))
        keys = [
            k for k in self.state.pairs
            if k in INSTRUMENTS
            and (get_instrument(k).source != "forex" or local.weekday() < 5)
        ]
        if not keys:
            return True  # nothing to plan (crypto disabled on a weekend)
        built: List[Tuple[str, PlanEntry]] = []
        for k in keys:
            entry = await self._fetch_pair_plan(k, force_fresh=True)
            if entry is None:
                continue
            if not entry.plan.market_closed:
                self.state.remember_plan_zones(k, entry.plan.zones_shown())
            built.append((k, entry))
        if not built:
            return False  # every fetch failed — retry next cycle
        text = format_plan_summary(slot, [e.plan for _, e in built])
        markup = plan_summary_keyboard([k for k, _ in built])
        message_id = await self.notifier.send(
            text, reply_markup=markup, disable_notification=True
        )
        if not message_id:
            return False
        self.state.plan_summary = {
            "message_id": message_id,
            "slot": slot,
            "date": local.date().isoformat(),
            "fingerprints": {
                k: plan_fingerprint(e.plan) for k, e in built
            },
            # Same facts, diffable — see planbook.plan_snapshot. The
            # fingerprint answers "did it change", this answers "what".
            "snapshots": {k: plan_snapshot(e.plan) for k, e in built},
        }
        self.state.save()
        logger.info("Auto-plan summary sent", slot=slot, pairs=len(built))
        return True

    def _recompute_plan(self, key: str, result: Optional[AnalysisResult]) -> None:
        """Refresh the pair's current plan from the candles this cycle's
        engine pass already fetched — free by API quota, pure by build_plan.
        Never writes plan_zones: provenance stays snapshot-only (spec §4)."""
        if (
            result is None
            or result.verdict == Verdict.OFF_SESSION
            or not result.m5_candles
            or result.h4_candles is None
            or result.h1_candles is None
        ):
            return
        from app.services.smc.plan import build_plan
        from app.services.smc.profiles import get_profile

        instrument = get_instrument(key)
        profile = get_profile(
            self.state.pair_profile.get(key, settings.smc.default_profile)
        )
        plan = build_plan(
            instrument, result.h4_candles, result.h1_candles, result.m5_candles,
            min_rr=settings.smc.min_rr, profile=profile,
        )
        as_of = to_prague(result.m5_candles[-1].timestamp).strftime("%H:%M")
        self.planbook.update(key, PlanEntry(
            plan=plan,
            data={
                "h4": result.h4_candles,
                "h1": result.h1_candles,
                "m5": result.m5_candles,
            },
            as_of=as_of,
        ))

    async def _maybe_edit_plan_summary(self) -> None:
        """Silently edit today's summary when any pair's plan materially
        changed since it was rendered (spec §3). Only pairs the summary
        already shows are compared — a summary is a snapshot of its slot's
        pair set, not a live pair list.

        Degrades per pair: a pair missing from the book (mid-day restart
        racing a failing feed, or the pair got disabled) carries its stored
        fingerprint forward unchanged, so it never triggers an edit on its
        own. When another pair's change DOES trigger one, the summary is
        rendered from the available plans only, with a static placeholder
        line for each missing pair — one absent pair no longer freezes
        summary edits for the whole book."""
        info = self.state.plan_summary
        if not info or info.get("date") != WatcherState._prague_day():
            return
        stored = info.get("fingerprints") or {}
        stored_snaps = info.get("snapshots") or {}
        current: Dict[str, str] = {}
        snapshots: Dict[str, dict] = {}
        plans = []
        missing: List[str] = []
        for k in stored:
            entry = self.planbook.get(k)
            if entry is None:
                current[k] = stored[k]  # unchanged -> never triggers alone
                snapshots[k] = stored_snaps.get(k)
                missing.append(k)
                logger.info(
                    "Auto-plan summary: pair missing from book", pair=k
                )
                continue
            current[k] = plan_fingerprint(entry.plan)
            snapshots[k] = plan_snapshot(entry.plan)
            plans.append(entry.plan)
        if current == stored:
            return
        upd = to_prague(datetime.now(tz=timezone.utc)).strftime("%H:%M")
        text = format_plan_summary(info["slot"], plans, updated_hhmm=upd)
        for k in missing:
            text += f"\n{escape_html(k)} ⚠️ no fresh plan (data unavailable)"
        ok = await self.notifier.edit_message(
            info["message_id"], text,
            reply_markup=plan_summary_keyboard(list(stored)),
        )
        if ok:
            info["fingerprints"] = current
            info["snapshots"] = snapshots
            self.state.plan_summary = info
            self.state.save()
            # The edit above is silent by design (it must not re-notify on
            # every recompute). The owner asked for corrections to actually
            # REACH him (2026-08-31), so the pairs that really moved get one
            # throttled message of their own.
            await self._notify_plan_changes(stored, current, stored_snaps, snapshots)

    PLAN_CHANGE_COOLDOWN = timedelta(hours=1)

    async def _notify_plan_changes(
        self,
        stored: Dict[str, str],
        current: Dict[str, str],
        stored_snaps: Dict[str, dict],
        snapshots: Dict[str, dict],
    ) -> None:
        """One "plan updated" message per pair that materially moved.

        Owner request 2026-08-31: the silent summary edit kept the picture
        current but never told him it had changed, so a correction was only
        visible to someone who scrolled back to the morning message and
        re-read it. This says what moved, in the plan's own vocabulary.

        Throttled to one message per pair per hour: the plan is recomputed
        every five minutes and a zone that keeps shifting by a pip would
        otherwise turn the whole point of quiet mode inside out. The silent
        edit is NOT throttled — the summary always shows the truth; only the
        announcement waits. A pair with no stored snapshot (the first cycle
        after this shipped, or a pair that was missing from the book) is
        recorded and stays silent: there is nothing honest to diff against.
        """
        now = datetime.now(tz=timezone.utc)
        for key, fingerprint in current.items():
            if stored.get(key) == fingerprint:
                continue  # this pair did not move
            before, after = stored_snaps.get(key), snapshots.get(key)
            if not before or not after:
                continue
            last = self.state.plan_change_notified.get(key)
            if last:
                try:
                    if now - datetime.fromisoformat(last) < self.PLAN_CHANGE_COOLDOWN:
                        logger.info("Plan change muted by cooldown", pair=key)
                        continue
                except (ValueError, TypeError):
                    pass  # poisoned timestamp -> treat as never notified
            decimals = get_instrument(key).price_decimals
            changes = describe_plan_changes(before, after, decimals)
            if not changes:
                continue
            body = "\n".join(f"• {escape_html(line)}" for line in changes)
            sent = await self.notifier.send(
                f"🔁 <b>Plan updated — {escape_html(key)}</b>\n{body}\n"
                f"Full plan: press {escape_html(key)} on today's summary."
            )
            if sent:
                self.state.plan_change_notified[key] = now.isoformat()
                self.state.save()
                logger.info("Plan change announced", pair=key, changes=changes)

    def _seconds_until_next_autoplan(self) -> Optional[float]:
        """Seconds until the nearest configured slot (today or tomorrow),
        so the scheduler can wake AT 07:55/13:55 instead of on the next
        cadence grid tick five minutes later.

        Each candidate is localized fresh from a naive datetime via
        `PRAGUE.localize` rather than built with `.replace()` on an
        already-localized `now_local` — `.replace()` keeps `now_local`'s
        UTC offset, which is wrong for a candidate on the other side of a
        DST transition."""
        if not settings.smc.auto_plan:
            return None
        slots = self._autoplan_slots()
        if not slots:
            return None
        now_local = to_prague(datetime.now(tz=timezone.utc))
        best = None
        for s in slots:
            hh, mm = (int(x) for x in s.split(":"))
            candidate = PRAGUE.localize(
                datetime.combine(now_local.date(), time(hh, mm))
            )
            if candidate <= now_local:
                candidate = PRAGUE.localize(
                    datetime.combine(
                        now_local.date() + timedelta(days=1), time(hh, mm)
                    )
                )
            delta = (candidate - now_local).total_seconds()
            best = delta if best is None else min(best, delta)
        return best

    async def _send_pair_plan(self, key: str) -> None:
        """Build and send one pair's Pre-Market Plan (text + H1 chart)."""
        entry = await self._fetch_pair_plan(key, force_fresh=True)
        if entry is None:
            return
        if not entry.plan.market_closed:
            self.state.remember_plan_zones(key, entry.plan.zones_shown())
        await self._deliver_plan(key, entry)

    async def _deliver_plan(self, key: str, entry: PlanEntry) -> None:
        """Send a plan message + chart from an already-built PlanEntry."""
        from app.services.smc.chart import render_plan_chart

        instrument = get_instrument(key)
        now = datetime.now(tz=timezone.utc)
        live_line = (
            None if entry.plan.market_closed
            else self._live_status(instrument, entry.data, now)
        )
        await self.notifier.send(
            format_plan(entry.plan, live_line=live_line, as_of=entry.as_of)
        )
        try:
            png = await asyncio.to_thread(
                render_plan_chart, entry.plan, entry.data["h1"]
            )
            if png:
                await self.notifier.send_photo(png)
        except Exception as e:
            logger.warning("Plan chart failed", pair=key, error=str(e))

    def _live_status(self, instrument: Instrument, data: dict, now) -> str:
        """One-line live checklist status of a pair, for the /plan message."""
        res = AnalysisResult(
            symbol=instrument.key,
            verdict=Verdict.SKIP,
            checked_at=now,
            price_decimals=instrument.price_decimals,
        )
        res.session_name = active_session(
            now, require_weekday=instrument.source == "forex"
        )
        res.price = data["m5"][-1].close
        from app.services.smc.profiles import get_profile

        profile = get_profile(
            self.state.pair_profile.get(
                instrument.key, settings.smc.default_profile
            )
        )
        res = _build_engine(instrument, profile).evaluate(
            h4=data["h4"], h1=data["h1"], m5=data["m5"], result=res
        )
        d = instrument.price_decimals
        if res.verdict in APPROVED:
            s = res.setup
            # take_profit is optional (detector mode): no unswept liquidity
            # ahead means there is no objective and no RR to quote.
            tail = (
                f", TP {s.take_profit:.{d}f} (RR 1:{s.rr:.1f})"
                if s.take_profit is not None
                else ", no TP (no structural objective)"
            )
            return (
                f"🚨 LIVE SETUP NOW — {s.direction.value} entry {s.entry:.{d}f}, "
                f"SL {s.stop_loss:.{d}f}{tail}"
            )
        prefix = "" if res.session_name else "(off session) "
        icon = "👀" if res.verdict == Verdict.WATCH else "⛔"
        reason = res.reasons[0] if res.reasons else "no direction"
        return f"{icon} {prefix}{escape_html(reason)}"

    async def _maybe_plan_zone_alert(
        self, key: str, result: Optional[AnalysisResult]
    ) -> bool:
        """Price entered a zone the CURRENT plan names: one alert per zone
        per session block (owner decision 2026-08-16, spec §1.3), carrying
        the plan's projected bracket.

        Returns whether a message went out, so the PD radar can stand down
        for this cycle: "price reached your zone" is the more actionable of
        the two, and both in one cycle is two messages about one moment.

        The 2026-08-11 "episode" rule is gone. It kept the alert armed as
        soon as the last closed M5 candle stopped overlapping the zone, so
        price oscillating on a zone edge re-alerted every few cycles —
        USDCAD sent the identical message four times on 2026-08-13. Silence
        now lasts the whole block and survives exits, re-entries and the
        five-minute plan recompute; the owner can end it early only by the
        block ending, or extend it with the 🔕 button.
        """
        if not settings.smc.zone_ping or result is None:
            return False
        if not result.session_name or not result.m5_candles:
            return False  # Rule 0.1: get-ready alerts belong to the session
        block = session_block(result.checked_at)
        if block is None:
            return False
        if self.state.zone_muted_until(key, result.checked_at):
            return False  # the owner silenced this pair's zone alerts (D3)
        if result.verdict in APPROVED:
            return False  # the full 🚨 alert covers this touch
        last = result.m5_candles[-1]
        scenario = self.planbook.scenario_for_touch(key, last.low, last.high)
        if scenario is None:
            return False
        if self._cooldown_left(key):
            return False  # already managing a position here
        if self.state.zone_already_pinged(
            key, scenario.zone_bottom, scenario.zone_top,
            scenario.direction.value, block,
        ):
            return False
        # The label promises exactly the deadline a press would produce
        # (owner decision 2026-08-16): both sides call block_mute_deadline
        # on the same block. block_mute_deadline cannot return None for a
        # block session_block itself just produced, but if it somehow did,
        # an alert must never be lost to a button problem — send it with no
        # keyboard rather than skip it.
        deadline = block_mute_deadline(block)
        reply_markup = (
            zone_alert_keyboard(key, prague_hhmm(deadline), block)
            if deadline is not None else None
        )
        marks = None
        try:
            from app.services.smc.profiles import effective_min_fvg, get_profile
            from app.services.smc.structure import m5_marks

            instrument = get_instrument(key)
            profile = get_profile(
                self.state.pair_profile.get(key, settings.smc.default_profile)
            )
            marks = m5_marks(
                result.m5_candles,
                scenario.direction,
                scenario.zone_bottom,
                scenario.zone_top,
                effective_min_fvg(instrument.min_fvg, profile),
            )
        except Exception as e:  # marking must never cost the owner an alert
            logger.warning("M5 marks failed", pair=key, error=str(e))
        sent = await self.notifier.send(
            format_zone_alert(key, scenario, result.price_decimals, marks=marks),
            reply_markup=reply_markup,
        )
        if sent:
            # mark AFTER the send: a failed delivery must retry next cycle
            self.state.remember_zone_ping(
                key, scenario.zone_bottom, scenario.zone_top,
                scenario.direction.value, block,
            )
            logger.info("Plan-zone alert sent", pair=key, block=block)
        return bool(sent)

    @staticmethod
    def _bias_direction(result: AnalysisResult) -> Optional[Direction]:
        """The direction the pair is biased to, by Rule 1's own precedence.

        H4 first; H1 when H4 reads FLAT (owner decision 2026-08-06); None
        when both are flat — that is the range state (D11), where there is no
        trend to be early or late to and the PD radar has nothing to say.
        None as well before Rule 1 ran at all (`h1_trend` unset), which is
        every off-session and market-closed return.
        """
        if result.h1_trend is None:
            return None
        for trend in (result.h4_trend, result.h1_trend):
            if trend == Trend.UP:
                return Direction.LONG
            if trend == Trend.DOWN:
                return Direction.SHORT
        return None

    def _pd_target(self, result: AnalysisResult, direction: Direction):
        """The nearest unswept H1/H4 pool ahead — the level the bias is
        reaching for. Best-effort: a level nobody can compute must never cost
        the owner the message it was going to decorate."""
        try:
            tolerance = get_instrument(result.symbol).min_fvg
            levels = (
                find_liquidity(result.h1_candles, "H1", tolerance)
                + find_liquidity(result.h4_candles, "H4", tolerance)
            )
            return nearest_liquidity(levels, direction, result.price)
        except Exception as e:
            logger.warning("PD target lookup failed", pair=result.symbol, error=str(e))
            return None

    async def _maybe_pd_alert(
        self, key: str, result: Optional[AnalysisResult]
    ) -> None:
        """PD radar: price reached the half of its dealing range the bias
        wants — discount under a long bias, premium under a short one.

        Owner request 2026-08-26. This is a get-ready message, not a setup:
        it says where price is inside the range it is retracing and what to
        watch for, and the M5 CHoCH + FVG trigger stays the engine's job.

        It fires ONLY with the bias (owner choice): a discount under a
        downtrend is not an opportunity, it is the trend working, and a bot
        that points at it is arguing for counter-trend entries the strategy
        does not take. One message per pair per side per session block, and
        the 🔕 button silences the pair the same way it does a zone alert —
        both belong to the same get-ready family, so one mute covers both.
        """
        if not settings.smc.pd_alert or result is None:
            return
        if not result.session_name or not result.h4_candles:
            return  # Rule 0.1: get-ready alerts belong to the session
        if not result.h1_candles or not result.price:
            return
        block = session_block(result.checked_at)
        if block is None:
            return
        if self.state.zone_muted_until(key, result.checked_at):
            return
        if result.verdict in APPROVED:
            return  # the 🚨 alert already carries its own PD line
        if self._cooldown_left(key):
            return  # already managing a position here
        direction = self._bias_direction(result)
        if direction is None:
            return
        read = pd_module.read(
            result.h4_candles, result.h1_candles, result.price, direction,
            basis=settings.smc.pd_basis,
        )
        if read is None or not read.favourable:
            return
        if self.state.pd_already_pinged(key, direction.value, block):
            return
        deadline = block_mute_deadline(block)
        reply_markup = (
            zone_alert_keyboard(key, prague_hhmm(deadline), block)
            if deadline is not None else None
        )
        sent = await self.notifier.send(
            format_pd_alert(
                key, read, result.price_decimals,
                zone=result.h1_zone,
                target=self._pd_target(result, direction),
            ),
            reply_markup=reply_markup,
        )
        if sent:
            # mark AFTER the send, like every other alert here: a delivery
            # that never landed must be retried on the next cycle.
            self.state.remember_pd_ping(key, direction.value, block)
            logger.info(
                "PD alert sent", pair=key, block=block,
                side=read.label, pct=read.pct,
            )

    async def _rule_04_warnings(self) -> None:
        """Rule 0.4: active signal + red news soon -> SL to BU / pull the order.

        `open_runner` (Phase 2 hybrid exit, journal.py: TP1 already closed
        half the position, the runner leg is still open) is included here
        alongside `open` — a runner-leg position is still live and exposed
        to the news release exactly like a plain `open` one (reviewer
        finding, carried into Task 4's scope). Before this fix the filter
        only checked `("pending", "open")` and a runner-leg signal got no
        pre-news warning at all.
        """
        now = datetime.now(tz=timezone.utc)
        horizon = timedelta(minutes=30)
        changed = False
        for signal in self.journal.signals:
            if signal["status"] not in ("pending", "open", "open_runner"):
                continue
            instrument = get_instrument(signal["pair"])
            for event in self.news.upcoming(relevant_currencies(instrument), horizon):
                warn_key = f"{signal['id']}:{event.time.isoformat()}"
                if warn_key in self.state.news_warned:
                    continue
                minutes_left = int((event.time - now).total_seconds() // 60)
                is_open_position = signal["status"] in ("open", "open_runner")
                action = (
                    "move the SL to breakeven"
                    if is_open_position
                    else "cancel the pending order"
                )
                await self.notifier.send(
                    f"⚠️ <b>RULE 0.4:</b> {signal['pair']} — 🔴 {escape_html(event.title)} "
                    f"({event.currency}) in {minutes_left} min "
                    f"({event.prague_hhmm()} Prague). You have "
                    f"{'an open position' if is_open_position else 'an active limit order'} "
                    f"— {action}!"
                )
                self.state.news_warned[warn_key] = now.isoformat()
                changed = True
        # Prune dedup keys older than 2 days. A value that fails to parse, or
        # is naive (a legacy JSON import, or older code before this fix), is
        # garbage with no dedup value of its own — drop it, instead of
        # letting fromisoformat's ValueError/TypeError (naive-vs-aware
        # comparison raises TypeError) kill this line, and every future
        # cycle, forever (review 2026-08-11, MEDIUM: this ran before the
        # per-pair loop, so it took the whole cycle down with it).
        cutoff = now - timedelta(days=2)
        bad_seen = self._news_warned_bad_seen()
        kept: Dict[str, str] = {}
        for k, v in self.state.news_warned.items():
            try:
                parsed = datetime.fromisoformat(v)
                if parsed.tzinfo is None:
                    raise ValueError("naive news_warned timestamp")
            except (TypeError, ValueError):
                if k not in bad_seen:
                    bad_seen.add(k)
                    logger.error(
                        "Dropping unparseable/naive news_warned timestamp",
                        key=k, value=v,
                    )
                continue
            if parsed > cutoff:
                kept[k] = v
        if kept != self.state.news_warned:
            changed = True
        self.state.news_warned = kept
        if changed:
            self.state.save()

    def pd_text(self) -> str:
        """`/pd`: premium/discount for every watched pair, from the last
        completed cycle.

        Reads `last_results` rather than fetching: the answer must be the
        same one the radar and the alerts are working from, and a second
        fetch would both cost Twelve Data credits and risk telling the owner
        a different story than the message he is holding.
        """
        lines = ["📐 <b>Premium / discount</b>"]
        for key in self.state.pairs:
            result = self.last_results.get(key)
            if result is None or not result.h4_candles or not result.h1_candles:
                lines.append(f"• {escape_html(key)}: no data yet")
                continue
            direction = self._bias_direction(result)
            if direction is None:
                lines.append(
                    f"• {escape_html(key)}: H4 and H1 both flat — no bias to "
                    "measure against"
                )
                continue
            read = pd_module.read(
                result.h4_candles, result.h1_candles, result.price, direction,
                basis=settings.smc.pd_basis,
            )
            side = "LONG" if direction == Direction.LONG else "SHORT"
            if read is None:
                lines.append(
                    f"• {escape_html(key)} (bias {side}): price is outside "
                    "every dealing range — expansion, not a retracement"
                )
                continue
            d = result.price_decimals
            mark = " ⭐ in OTE" if read.in_ote else ""
            verdict = "✅" if read.favourable else "⚠️"
            lines.append(
                f"• {escape_html(key)} (bias {side}): {verdict} "
                f"{read.pct}% — {escape_html(read.label)}{mark}\n"
                f"   {escape_html(read.range.timeframe)} "
                f"{read.range.low:.{d}f}–{read.range.high:.{d}f} · "
                f"OTE {read.ote_low:.{d}f}–{read.ote_high:.{d}f}"
            )
        return "\n".join(lines)

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
        ]
        if self.state.paused:
            lines.append("⏸ <b>PAUSED</b> — no alerts, /resume to continue")
        lines.append(f"Pairs: {', '.join(self.state.pairs) or 'none'}")
        lines.append(f"Notify: {self.state.notify_level}")
        try:
            forex_source = _forex_source()
        except ConfigurationError:
            forex_source = "NOT CONFIGURED"
        lines.extend([
            f"Forex data: {forex_source} | crypto: Binance",
            f"Session now: {session or 'off session'}",
            f"Cadence: {settings.smc.session_interval_minutes} min in session / "
            f"{settings.smc.interval_minutes} min off",
            "Deposit for sizing: "
            + (f"${settings.smc.deposit:.0f}" if settings.smc.deposit else "not set"),
            "PD radar: "
            + (
                f"on ({settings.smc.pd_basis.upper()} range)"
                if settings.smc.pd_alert
                else f"off ({settings.smc.pd_basis.upper()} range for ⭐)"
            ),
        ])
        muted = [
            f"{k} ({left})"
            for k in self.state.pairs
            if (left := self._cooldown_left(k))
        ]
        if muted:
            lines.append(f"🔕 Muted (taken): {', '.join(muted)}")
        zone_muted = [
            f"{k} (till {until})"
            for k in self.state.pairs
            if (until := self.state.zone_muted_until(k))
        ]
        if zone_muted:
            lines.append(
                f"🔕 Zone + PD alerts muted: {', '.join(zone_muted)}"
            )
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
            sleep_s = _seconds_until_next_slot(interval) + 10
            # wake AT the auto-plan slot (07:55 sits off the 15-min grid)
            next_plan = self._seconds_until_next_autoplan()
            if next_plan is not None:
                sleep_s = min(sleep_s, next_plan + 5)
            await asyncio.sleep(sleep_s)

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
    watcher = Watcher()
    samples = [
        "🧪 <b>SMC watcher TEST</b> — Telegram wiring works.",
        "🚨 <b>TEST: SETUP READY — this is how a detector-mode setup alert "
        "opens</b> (NOT a real signal).",
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
