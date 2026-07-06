"""Standalone SMC strategy watcher.

Runs the Triple Sync + Imbalance strategy on a fixed interval (default: every
15 minutes, aligned to :00/:15/:30/:45) and sends a Telegram message when a
valid setup is found. No database or Redis required.

Usage:
    python smc_watcher.py            # run forever
    python smc_watcher.py --once     # single check (prints result, exits)
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import structlog

from app.core.config import settings
from app.core.logging import configure_logging
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.models import AnalysisResult, Verdict
from app.services.smc.notifier import TelegramNotifier, format_result

configure_logging()
logger = structlog.get_logger("smc_watcher")

# Windows consoles often default to a legacy codepage that cannot print emoji.
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STATE_FILE = os.getenv("SMC_STATE_FILE", ".smc_watcher_state.json")


def _setup_fingerprint(result: AnalysisResult) -> str:
    """Identity of a setup so the same one is not re-sent every 15 minutes."""
    setup = result.setup
    session_day = result.checked_at.strftime("%Y-%m-%d")
    return (
        f"{result.symbol}:{setup.direction.value}:{setup.entry:.2f}:"
        f"{result.session_name}:{session_day}"
    )


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        logger.warning("Failed to persist watcher state", error=str(e))


def _build_engine() -> TripleSyncEngine:
    smc = settings.smc
    return TripleSyncEngine(
        symbol=smc.symbol,
        display_symbol=smc.display_symbol,
        min_fvg_size=smc.min_fvg_usd,
        sl_buffer=smc.sl_buffer_usd,
        min_rr=smc.min_rr,
        risk_pct=smc.risk_pct,
        deposit=smc.deposit,
        enforce_sessions=smc.enforce_sessions,
    )


def _build_notifier() -> TelegramNotifier:
    chat_id = settings.smc.chat_id or settings.telegram.chat_id
    token = settings.telegram.bot_token
    if not token or token.startswith("your-"):
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    if not chat_id:
        raise RuntimeError("Set SMC_CHAT_ID or TELEGRAM_CHAT_ID for notifications")
    return TelegramNotifier(bot_token=token, chat_id=chat_id)


async def run_check(
    engine: TripleSyncEngine, notifier: TelegramNotifier, state: dict
) -> AnalysisResult:
    """One strategy pass: analyze, notify if a new setup was found."""
    result = await engine.analyze()
    logger.info(
        "SMC check finished",
        verdict=result.verdict.value,
        trend=result.h4_trend.value,
        price=result.price,
        reasons=result.reasons,
    )

    approved = result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET)
    if approved:
        fingerprint = _setup_fingerprint(result)
        if state.get("last_setup") == fingerprint:
            logger.info("Setup already reported this session, skipping resend")
            return result
        if await notifier.send(format_result(result)):
            state["last_setup"] = fingerprint
            _save_state(state)
    elif settings.smc.notify_no_setup and result.verdict != Verdict.OFF_SESSION:
        await notifier.send(format_result(result))

    return result


def _seconds_until_next_slot(interval_minutes: int) -> float:
    """Seconds until the next aligned slot (e.g. :00/:15/:30/:45 for 15m)."""
    now = datetime.now(tz=timezone.utc)
    seconds_into_hour = now.minute * 60 + now.second + now.microsecond / 1e6
    slot = interval_minutes * 60
    return slot - (seconds_into_hour % slot)


async def main_loop() -> None:
    engine = _build_engine()
    notifier = _build_notifier()
    state = _load_state()
    interval = settings.smc.interval_minutes
    logger.info(
        "SMC watcher started",
        symbol=settings.smc.symbol,
        interval_minutes=interval,
    )
    while True:
        try:
            await run_check(engine, notifier, state)
        except Exception as e:
            logger.error("SMC check failed", error=str(e), exc_info=True)
        await asyncio.sleep(_seconds_until_next_slot(interval))


async def run_once() -> None:
    engine = _build_engine()
    result = await engine.analyze()
    print(format_result(result).replace("<b>", "").replace("</b>", ""))
    approved = result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET)
    if approved:
        notifier = _build_notifier()
        sent = await notifier.send(format_result(result))
        print(f"\nTelegram: {'sent' if sent else 'FAILED'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Triple Sync + Imbalance watcher")
    parser.add_argument(
        "--once", action="store_true", help="run a single check and exit"
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_once() if args.once else main_loop())
    except KeyboardInterrupt:
        sys.exit(0)
