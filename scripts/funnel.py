"""Offline funnel calibration for the SMC strategy (NOT used by the worker).

Replays TripleSyncEngine.evaluate over historical candles for both profiles
and prints where setups die in the rule funnel and the size distribution of
near-miss FVGs. Use the numbers to choose StrategyProfile.fvg_size_factor.

Usage:
    python -m scripts.funnel --pair ETHUSD --days 45
    python -m scripts.funnel --all --days 45
"""

import argparse
import asyncio
from collections import Counter
from typing import Dict, List

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import INSTRUMENTS, Instrument, get_instrument
from app.services.smc.models import AnalysisResult, Candle, Trend, Verdict
from app.services.smc.profiles import PROFILES, StrategyProfile
from app.services.smc.sessions import active_session


def classify_result(result: AnalysisResult) -> str:
    """Map an evaluate() outcome to a funnel stage label."""
    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
        return "approved"
    if result.verdict == Verdict.OFF_SESSION:
        return "off_session"
    reason = (result.reasons[0] if result.reasons else "").lower()
    if result.h4_trend == Trend.FLAT and "no direction" in reason:
        return "h4_flat"
    if "no valid untested" in reason or "no untested opposite" in reason:
        return "no_zone"
    if "has not reached" in reason or "pullback phase" in reason:
        return "no_touch"
    if "has not printed a choch" in reason:
        return "no_choch"
    if "no valid fvg" in reason:
        return "fvg_small"
    if "rr 1:" in reason:
        return "rr_low"
    return "other"


def replay_funnel(
    instrument: Instrument,
    h4: List[Candle],
    h1: List[Candle],
    m5: List[Candle],
    profile: StrategyProfile,
    min_rr: float,
    warmup: int = 60,
) -> Dict[str, int]:
    """Replay evaluate() at each M5 close (after `warmup` candles). Off-session
    candles are skipped. Returns a Counter of funnel-stage labels."""
    engine = TripleSyncEngine(
        instrument=instrument, min_rr=min_rr, enforce_sessions=True,
        profile=profile, fetcher=None,
    )
    counts: Counter = Counter()
    start = max(warmup, 2)
    # If warmup exceeds available data, use what we have
    start = min(start, len(m5) - 1)
    for i in range(start, len(m5) + 1):
        window = m5[:i]
        now = window[-1].timestamp
        if active_session(now, require_weekday=instrument.source == "forex") is None:
            counts["off_session"] += 1
            continue
        result = AnalysisResult(
            symbol=instrument.key, verdict=Verdict.SKIP, checked_at=now,
            price_decimals=instrument.price_decimals,
        )
        result.session_name = active_session(now)
        result.price = window[-1].close
        result = engine.evaluate(h4=h4, h1=h1, m5=window, result=result)
        counts[classify_result(result)] += 1
    return dict(counts)


async def _run(pairs: List[str], days: int) -> None:
    from smc_watcher import _build_fetcher  # reuse the worker's source resolution

    for key in pairs:
        instrument = get_instrument(key)
        fetcher = _build_fetcher(instrument)
        data = await fetcher.fetch_all_timeframes()
        print(f"\n=== {key} — last {len(data['m5'])} M5 candles ===")
        for profile in PROFILES.values():
            counts = replay_funnel(
                instrument, data["h4"], data["h1"], data["m5"], profile, min_rr=2.0
            )
            in_session = sum(v for k, v in counts.items() if k != "off_session")
            print(f"  {profile.label}: {counts}")
            print(f"    in-session checks: {in_session}, approved: {counts.get('approved', 0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC funnel calibration")
    parser.add_argument("--pair", default="ETHUSD")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--days", type=int, default=45)
    args = parser.parse_args()
    pairs = list(INSTRUMENTS) if args.all else [args.pair]
    asyncio.run(_run(pairs, args.days))


if __name__ == "__main__":
    main()
