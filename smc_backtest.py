"""Backtest CLI — replay the Triple Sync strategy over cached history.

Usage:
    python smc_backtest.py --pair USDJPY --days 365
    python smc_backtest.py --pair ETHUSD USDJPY --days 180
    python smc_backtest.py --all-pairs --days 180 --report-dir data/backtest/reports

Forex pairs need TWELVEDATA_API_KEY (the live bot's own key: the first run
downloads history into data/backtest/ — roughly 15 requests per pair-year
of M5 — and every later run reuses the cache). ETHUSD needs no key.

A pair whose history cannot be fetched is reported and skipped, so an
--all-pairs run always produces every report it can. The report format and
its NOT-simulated disclaimers come from app/services/smc/backtest.py; the
strategy itself is the production engine, untouched (spec 2026-08-30,
D20/D21).
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
from app.core.exceptions import DataFetchError
from app.services.smc.backtest import (
    build_backtest_engine,
    render_combined,
    render_report,
    run_backtest,
    synthetic_history,
)
from app.services.smc.db import Database
from app.services.smc.history import DEFAULT_CACHE_DIR, load_history
from app.services.smc.instruments import DEFAULT_PAIRS, INSTRUMENTS, get_instrument
from app.services.smc.journal import SignalJournal
from app.services.smc.profiles import get_profile

# Calendar lead per timeframe so every window is full at --start: 300 H4 is
# ~50 trading days (~70 calendar with weekends), 400 H1 ~17 trading days,
# 400 M5 fits in two days plus a weekend.
_LEAD = {"h4": timedelta(days=75), "h1": timedelta(days=30), "m5": timedelta(days=4)}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pair",
        nargs="+",
        choices=sorted(INSTRUMENTS),
        default=None,
        help=f"pairs to replay (default: the live set {DEFAULT_PAIRS})",
    )
    parser.add_argument(
        "--all-pairs",
        action="store_true",
        help="replay every registered instrument",
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument(
        "--end", default=None, help="last day to replay (YYYY-MM-DD, default: now)"
    )
    parser.add_argument("--profile", default="conservative")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument(
        "--report-dir",
        default=None,
        help="also write each report to <dir>/<end>-<pair>.txt",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="offline pipeline check on deterministic synthetic candles — "
        "no network, no keys; exits 0 on OK",
    )
    return parser.parse_args()


def _selftest() -> int:
    """Replay a fixed synthetic tape through the full pipeline and assert
    its invariants. Synthetic candles prove the machinery, never the
    strategy — the report is suppressed on purpose."""
    print("selftest: generating deterministic synthetic history…")
    candles = synthetic_history()
    end = candles["m5"][-1].timestamp
    start = end - timedelta(days=6)

    fd, db_path = tempfile.mkstemp(prefix="smc_selftest_", suffix=".db")
    os.close(fd)
    try:
        journal = SignalJournal(Database(db_path))
        run = run_backtest(
            "ETHUSD",
            candles["h4"],
            candles["h1"],
            candles["m5"],
            start,
            end,
            journal=journal,
            engine=build_backtest_engine("ETHUSD"),
        )
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    checks = [
        ("cycles ran", run.cycles > 0),
        ("off-session candles were gated", run.off_session > 0),
        ("every cycle got a verdict", sum(run.verdicts.values()) == run.cycles),
        ("signals were recorded", len(run.records) > 0),
        (
            "every signal survived the journal round-trip",
            all(journal.get(r.signal["id"]) is not None for r in run.records),
        ),
        ("the report renders with its disclaimers",
         "NOT simulated" in render_report(run)),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print(
        f"selftest: {run.cycles} cycles, {len(run.records)} signals, "
        f"verdicts {run.verdicts}"
    )
    if failed:
        print("SELFTEST FAILED")
        return 1
    print("SELFTEST OK — the pipeline works; numbers above are synthetic, "
          "not a strategy verdict")
    return 0


async def _run_pair(pair: str, args, start, end, api_key):
    candles = {}
    for tf in ("h4", "h1", "m5"):
        candles[tf] = await load_history(
            pair,
            tf,
            start - _LEAD[tf],
            end,
            cache_dir=Path(args.cache_dir),
            api_key=api_key,
            refresh=args.refresh_cache,
        )
        print(f"{pair} {tf}: {len(candles[tf])} candles", file=sys.stderr)

    # A throwaway journal DB per pair: production semantics, disposable rows.
    fd, db_path = tempfile.mkstemp(prefix=f"smc_backtest_{pair}_", suffix=".db")
    os.close(fd)
    try:
        journal = SignalJournal(Database(db_path))
        return run_backtest(
            pair,
            candles["h4"],
            candles["h1"],
            candles["m5"],
            start,
            end,
            journal=journal,
            engine=build_backtest_engine(pair, get_profile(args.profile)),
        )
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


async def _main() -> int:
    # The journal logs one INFO line per recorded signal — thousands of
    # them would bury the report, so the CLI runs quiet. The named-logger
    # level survives the basicConfig() that importing smc_watcher (inside
    # run_backtest) performs; a bare root setLevel would not.
    logging.getLogger("app.services.smc").setLevel(logging.WARNING)
    args = _parse_args()
    if args.selftest:
        return _selftest()
    pairs = (
        sorted(INSTRUMENTS) if args.all_pairs
        else (args.pair or list(DEFAULT_PAIRS))
    )
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
        if args.end
        else datetime.now(tz=timezone.utc)
    )
    start = end - timedelta(days=args.days)
    api_key = settings.twelvedata.api_key
    forex = [p for p in pairs if get_instrument(p).source == "forex"]
    if forex and not api_key:
        print(
            f"TWELVEDATA_API_KEY is not set — skipping forex pairs {forex}.",
            file=sys.stderr,
        )
        pairs = [p for p in pairs if p not in forex]
    if not pairs:
        print("Nothing to replay.", file=sys.stderr)
        return 1

    runs = []
    for pair in pairs:
        try:
            run = await _run_pair(pair, args, start, end, api_key)
        except DataFetchError as e:
            print(f"{pair}: history unavailable — {e}", file=sys.stderr)
            continue
        runs.append(run)
        report = render_report(run)
        print(report)
        print()
        if args.report_dir:
            out = Path(args.report_dir) / f"{end:%Y-%m-%d}-{pair}.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report + "\n")
            print(f"report written to {out}", file=sys.stderr)

    if not runs:
        print("No pair produced a report.", file=sys.stderr)
        return 1
    if len(runs) > 1:
        print(render_combined(runs))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
