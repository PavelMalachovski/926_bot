"""Backtest CLI — replay the Triple Sync strategy over cached history.

Usage:
    python smc_backtest.py --pair USDJPY --days 365
    python smc_backtest.py --pair ETHUSD --days 90 --report-out report.txt

Forex pairs need TWELVEDATA_API_KEY (the live bot's own key: the first run
downloads history into data/backtest/ — roughly 15 requests per pair-year
of M5 — and every later run reuses the cache). ETHUSD needs no key.

The report and its NOT-simulated disclaimers come from
app/services/smc/backtest.py; the strategy itself is the production engine,
untouched (spec 2026-08-30, D20/D21).
"""

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings
from app.services.smc.backtest import build_backtest_engine, render_report, run_backtest
from app.services.smc.db import Database
from app.services.smc.history import DEFAULT_CACHE_DIR, load_history
from app.services.smc.instruments import INSTRUMENTS, get_instrument
from app.services.smc.journal import SignalJournal
from app.services.smc.profiles import get_profile

# Calendar lead per timeframe so every window is full at --start: 300 H4 is
# ~50 trading days (~70 calendar with weekends), 400 H1 ~17 trading days,
# 400 M5 fits in two days plus a weekend.
_LEAD = {"h4": timedelta(days=75), "h1": timedelta(days=30), "m5": timedelta(days=4)}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pair", required=True, choices=sorted(INSTRUMENTS))
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument(
        "--end", default=None, help="last day to replay (YYYY-MM-DD, default: now)"
    )
    parser.add_argument("--profile", default="conservative")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--report-out", default=None)
    return parser.parse_args()


async def _main() -> int:
    # The journal logs one INFO line per recorded signal — thousands of
    # them would bury the report, so the CLI runs quiet. The named-logger
    # level survives the basicConfig() that importing smc_watcher (inside
    # run_backtest) performs; a bare root setLevel would not.
    logging.getLogger("app.services.smc").setLevel(logging.WARNING)
    args = _parse_args()
    instrument = get_instrument(args.pair)
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
        if args.end
        else datetime.now(tz=timezone.utc)
    )
    start = end - timedelta(days=args.days)
    api_key = settings.twelvedata.api_key
    if instrument.source == "forex" and not api_key:
        print("TWELVEDATA_API_KEY is not set — forex history needs it.")
        return 1

    candles = {}
    for tf in ("h4", "h1", "m5"):
        candles[tf] = await load_history(
            args.pair,
            tf,
            start - _LEAD[tf],
            end,
            cache_dir=Path(args.cache_dir),
            api_key=api_key,
            refresh=args.refresh_cache,
        )
        print(f"{tf}: {len(candles[tf])} candles", file=sys.stderr)

    # A throwaway journal DB: production semantics, disposable rows.
    fd, db_path = tempfile.mkstemp(prefix="smc_backtest_", suffix=".db")
    os.close(fd)
    try:
        journal = SignalJournal(Database(db_path))
        run = run_backtest(
            args.pair,
            candles["h4"],
            candles["h1"],
            candles["m5"],
            start,
            end,
            journal=journal,
            engine=build_backtest_engine(args.pair, get_profile(args.profile)),
        )
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    report = render_report(run)
    print(report)
    if args.report_out:
        Path(args.report_out).write_text(report + "\n")
        print(f"\nreport written to {args.report_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
