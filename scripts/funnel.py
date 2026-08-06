"""Offline funnel calibration for the SMC strategy (NOT used by the worker).

Replays TripleSyncEngine.evaluate over historical candles per strategy
profile and prints where setups die in the rule funnel. Point-in-time H4/H1
slicing means each replayed M5 close only sees higher-timeframe candles that
had actually closed by then (no lookahead), and distinct-setup (rising-edge)
counting turns "how many M5 closes read approved" into "how many alert
episodes the bot would have sent" — the number that matters for calibrating
StrategyProfile.fvg_size_factor.

Usage:
    python -m scripts.funnel --pair ETHUSD --days 45
    python -m scripts.funnel --all --days 45
    python -m scripts.funnel --pair ETHUSD --days 45 --factors 0.4,0.6,0.8,1.0
    python -m scripts.funnel --pair ETHUSD --days 45 --legacy   # old dual-profile report
"""

import argparse
import asyncio
import dataclasses
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import httpx

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import INSTRUMENTS, Instrument, get_instrument
from app.services.smc.models import AnalysisResult, Candle, Trend, Verdict
from app.services.smc.profiles import AGGRESSIVE, CONSERVATIVE, PROFILES, StrategyProfile
from app.services.smc.sessions import active_session, to_prague

# Minimum point-in-time H4/H1 candles required before evaluate() is allowed to
# run (protects detect_trend()/find_pivots() from reading a near-empty
# series). The brief's suggested default was 20; tuned down to 15 here — see
# funnel-v2-report.md for the fixture math that requires <=17 to keep the
# original replay_funnel test green under real point-in-time slicing.
PIT_FLOOR = 15

BINANCE_BASE = "https://api.binance.com"


def classify_result(result: AnalysisResult) -> str:
    """Map an evaluate() outcome to a primary funnel stage label.

    Detector mode (2026-08-06 spec sec 1/5) demoted rr_low, entry_stale and
    no_liquidity from SKIP reasons to warnings on an APPROVED result — the
    engine no longer writes their old reason strings, so an approved result
    always classifies as "approved" here, whichever warnings it carries. Use
    `classify_warnings` to count those warnings separately.
    """
    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
        return "approved"
    if result.verdict == Verdict.OFF_SESSION:
        return "off_session"
    reason = (result.reasons[0] if result.reasons else "").lower()
    if result.h4_trend == Trend.FLAT and "no direction" in reason:
        return "h4_flat"
    if "no valid untested" in reason:
        return "no_zone"
    if "has not reached" in reason or "pullback phase" in reason:
        return "no_touch"
    if "has not printed a choch" in reason:
        return "no_choch"
    if "no valid fvg" in reason:
        return "fvg_small"
    return "other"


def classify_warnings(result: AnalysisResult) -> List[str]:
    """Warning labels carried by an APPROVED result.

    rr_low, entry_stale and no_liquidity used to be terminal funnel stages;
    detector mode turned them into `result.warnings` entries on a setup that
    is announced anyway (spec sec 1/5). The labels are kept, not deleted —
    moved here so the funnel can still answer "how many announced setups
    carried a warning" without the death-stage dict implying a rejection
    that no longer happens. Meaningful only for an approved result.
    """
    labels = []
    for warning in result.warnings:
        w = warning.lower()
        if w.startswith("rr to the nearest liquidity"):
            labels.append("rr_low")
        elif "past the imbalance" in w:
            labels.append("entry_stale")
        elif "no unswept liquidity ahead" in w or "inside the stop buffer" in w:
            labels.append("no_liquidity")
    return labels


def in_session_count(counts: Dict[str, int]) -> int:
    """Total M5 closes classified while in-session, for the `--legacy`
    report's "in-session checks" line.

    Every replayed close increments exactly one of `classify_result`'s
    stage labels (or "warmup") — except `off_session`, which by definition
    is not in session, so it is excluded. The `warn_*` counters are NOT one
    of those per-close stages: `replay_funnel` increments them *alongside*
    "approved" on the same close (spec sec 1/5, a warning does not replace
    the approval), so summing them in here double-counts every warned close
    and inflates the total — on a real replay, by as much as the approved
    count itself. Excluded for the same reason `off_session` is.
    """
    return sum(
        v for k, v in counts.items()
        if k != "off_session" and not k.startswith("warn_")
    )


def session_day_span(m5: List[Candle]) -> int:
    """Count distinct Prague calendar days among the in-session M5 candles."""
    days = {
        to_prague(c.timestamp).date()
        for c in m5
        if active_session(c.timestamp) is not None
    }
    return len(days)


def replay_funnel(
    instrument: Instrument,
    h4: List[Candle],
    h1: List[Candle],
    m5: List[Candle],
    profile: StrategyProfile,
    min_rr: float = 1.0,
    max_entry_gap_r: float = 0.75,
    warmup: int = 60,
) -> Dict[str, int]:
    """Replay evaluate() at each M5 close (after `warmup` candles).

    Off-session candles are skipped. At each step h4/h1 are sliced to only
    the candles that had CLOSED by the current M5 close time (open + timeframe
    duration, point-in-time — replaying an old M5 close must not see the
    still-in-progress H4/H1 bar, whose OHLC contains future prices).
    Steps with too little point-in-time history are counted as "warmup".

    Returns a Counter of funnel-stage labels, plus "distinct_setups" (a
    rising-edge count of transitions into "approved" — how many alert
    episodes the bot would have sent, as opposed to how many M5 closes a
    persisting setup was re-counted on), "session_days" (the number of
    distinct Prague trading days spanned by the replayed M5 candles, for
    turning distinct_setups into a setups-per-week rate) and "warn_rr_low" /
    "warn_entry_stale" / "warn_no_liquidity" (how many approved results
    carried that warning — not death stages, so they are counted alongside
    "approved" rather than instead of it; see `classify_warnings`).

    `max_entry_gap_r` defaults to 0.75, matching `SMCSettings.max_entry_gap_r`
    — this tool measures what the bot would actually alert on, so its
    default must track production, not disable the gate. Pass a different
    value (e.g. a large one) to measure a threshold the bot isn't shipping.
    """
    engine = TripleSyncEngine(
        instrument=instrument, min_rr=min_rr,
        max_entry_gap_r=max_entry_gap_r, enforce_sessions=True,
        profile=profile, fetcher=None,
    )
    counts: Counter = Counter()
    start = max(warmup, 2)
    # If warmup exceeds available data, use what we have
    start = min(start, len(m5) - 1)
    prev_approved = False
    for i in range(start, len(m5) + 1):
        window = m5[:i]
        now = window[-1].timestamp
        if active_session(now, require_weekday=instrument.source == "forex") is None:
            counts["off_session"] += 1
            continue
        # A bar is visible only once fully closed: open + timeframe <= cutoff.
        cutoff = window[-1].timestamp
        h4_pit = [c for c in h4 if c.timestamp + timedelta(hours=4) <= cutoff]
        h1_pit = [c for c in h1 if c.timestamp + timedelta(hours=1) <= cutoff]
        if len(h4_pit) < PIT_FLOOR or len(h1_pit) < PIT_FLOOR:
            counts["warmup"] += 1
            continue
        result = AnalysisResult(
            symbol=instrument.key, verdict=Verdict.SKIP, checked_at=now,
            price_decimals=instrument.price_decimals,
        )
        result.session_name = active_session(now)
        result.price = window[-1].close
        result = engine.evaluate(h4=h4_pit, h1=h1_pit, m5=window, result=result)
        stage = classify_result(result)
        counts[stage] += 1
        if stage == "approved":
            for label in classify_warnings(result):
                counts[f"warn_{label}"] += 1
        approved_now = stage == "approved"
        if approved_now and not prev_approved:
            counts["distinct_setups"] += 1
        prev_approved = approved_now
    counts["session_days"] = session_day_span(m5[max(start - 1, 0):])
    return dict(counts)


async def fetch_binance_history(symbol: str, interval: str, days: int) -> List[Candle]:
    """Page Binance klines from `days` ago up to now (funnel-local — do not
    use app.services.smc.data.BinanceDataFetcher.fetch_candles() here, it
    only returns the last `limit` candles: ~1.5 days of M5, far too shallow
    to calibrate fvg_size_factor against distinct setups per week).

    Builds Candle objects the same way data.py does, and drops the
    in-progress last candle by close-time > now.
    """
    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(days=days)).timestamp() * 1000)
    now_ms = int(now.timestamp() * 1000)
    rows: List[list] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        while start_ms < now_ms:
            params = {
                "symbol": symbol, "interval": interval,
                "startTime": start_ms, "limit": 1000,
            }
            response = await client.get(f"{BINANCE_BASE}/api/v3/klines", params=params)
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            start_ms = batch[-1][6] + 1  # next page starts after this close time
            if len(batch) < 1000:
                break  # caught up to "now"

    candles = [
        Candle(
            timestamp=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in rows
    ]
    if rows and rows[-1][6] > now_ms:
        candles = candles[:-1]
    return candles


def build_factor_profile(factor: float) -> StrategyProfile:
    """Derive an AGGRESSIVE variant with a different fvg_size_factor."""
    return dataclasses.replace(AGGRESSIVE, fvg_size_factor=factor, key=f"aggr{factor}")


def parse_factors(raw: str) -> List[float]:
    """Parse a comma list like '0.4,0.6' into [0.4, 0.6]."""
    return [float(x) for x in raw.split(",") if x.strip()]


async def _run(pairs: List[str], days: int, factors: List[float], legacy: bool) -> None:
    from smc_watcher import _build_fetcher  # reuse the worker's source resolution

    for key in pairs:
        instrument = get_instrument(key)
        if instrument.source == "crypto":
            symbol = instrument.source_symbol
            h4 = await fetch_binance_history(symbol, "4h", days)
            h1 = await fetch_binance_history(symbol, "1h", days)
            m5 = await fetch_binance_history(symbol, "5m", days)
        else:  # forex
            from smc_watcher import _forex_source
            fetcher = _build_fetcher(instrument)
            src = _forex_source()
            if src == "twelvedata":
                # TwelveData: outputsize up to 5000 in one request — deep M5.
                m5 = await fetcher.fetch_candles("5m", limit=5000)
                h1 = await fetcher.fetch_candles("1h", limit=min(5000, days * 24 + 100))
                h4 = await fetcher.fetch_candles("4h", limit=min(5000, days * 6 + 50))
                print(f"  note: {key} via TwelveData — deep M5 (outputsize up to 5000)")
            else:
                data = await fetcher.fetch_all_timeframes()
                h4, h1, m5 = data["h4"], data["h1"], data["m5"]
                print(f"  note: {key} via {src} — 5m history is feed-limited (~1.5d)")

        print(f"\n=== {key} — {len(m5)} M5 candles ===")

        if legacy:
            for profile in PROFILES.values():
                counts = replay_funnel(instrument, h4, h1, m5, profile)
                in_session = in_session_count(counts)
                print(f"  {profile.label}: {counts}")
                print(
                    f"    in-session checks: {in_session}, "
                    f"approved: {counts.get('approved', 0)}, "
                    f"distinct setups: {counts.get('distinct_setups', 0)}"
                )
            continue

        rows = [("baseline", CONSERVATIVE)] + [
            (f"{f:g}", build_factor_profile(f)) for f in factors
        ]
        print(f"  {'factor':<10}{'distinct':>10}{'setups/wk':>12}{'approved':>10}  died")
        for label, profile in rows:
            counts = replay_funnel(instrument, h4, h1, m5, profile)
            session_days = max(counts.get("session_days", 0), 1)
            setups_per_week = counts.get("distinct_setups", 0) / session_days * 5
            died = {
                k: v for k, v in counts.items()
                if k not in (
                    "approved", "distinct_setups", "session_days",
                    "off_session", "warmup",
                ) and not k.startswith("warn_")
            }
            print(
                f"  {label:<10}{counts.get('distinct_setups', 0):>10}"
                f"{setups_per_week:>12.2f}{counts.get('approved', 0):>10}  {died}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC funnel calibration")
    parser.add_argument("--pair", default="ETHUSD")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument(
        "--factors", default="0.4,0.6,0.8,1.0",
        help="comma list of fvg_size_factor values to sweep (aggressive profile)",
    )
    parser.add_argument(
        "--legacy", action="store_true",
        help="run the old dual-profile report instead of the fvg_size_factor sweep",
    )
    args = parser.parse_args()
    pairs = list(INSTRUMENTS) if args.all else [args.pair]
    factors = parse_factors(args.factors)
    asyncio.run(_run(pairs, args.days, factors, args.legacy))


if __name__ == "__main__":
    main()
