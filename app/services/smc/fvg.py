"""FVG detection and validation (Rule 4)."""

from typing import List, Optional, Tuple

from app.services.smc.models import Candle, Direction, FVG
from app.services.smc.sessions import same_session, same_trading_day


def find_fvgs(
    candles: List[Candle], direction: Direction, from_index: int
) -> List[FVG]:
    """Find FVGs in the trade direction formed at or after `from_index`.

    Convention follows the strategy spec (candle 1 = newest of the triple):
    bullish FVG when low(newest) > high(oldest); the gap is the imbalance.
    """
    fvgs: List[FVG] = []
    for i in range(max(from_index, 2), len(candles)):
        newest, oldest = candles[i], candles[i - 2]
        if direction == Direction.LONG and newest.low > oldest.high:
            fvgs.append(
                FVG(
                    index=i,
                    bottom=oldest.high,
                    top=newest.low,
                    is_bullish=True,
                    timestamp=newest.timestamp,
                )
            )
        elif direction == Direction.SHORT and newest.high < oldest.low:
            fvgs.append(
                FVG(
                    index=i,
                    bottom=newest.high,
                    top=oldest.low,
                    is_bullish=False,
                    timestamp=newest.timestamp,
                )
            )
    return fvgs


def measure_fill(candles: List[Candle], fvg: FVG) -> FVG:
    """Compute fill percentage and structural invalidation for an FVG."""
    later = candles[fvg.index + 1 :]
    if not later:
        return fvg

    if fvg.is_bullish:
        deepest = min(c.low for c in later)
        penetration = max(0.0, fvg.top - deepest)
        # Body close below the far (bottom) edge = closed through.
        fvg.closed_through = any(
            c.close < fvg.bottom and c.body_low < fvg.bottom for c in later
        )
    else:
        highest = max(c.high for c in later)
        penetration = max(0.0, highest - fvg.bottom)
        fvg.closed_through = any(
            c.close > fvg.top and c.body_high > fvg.top for c in later
        )
    fvg.fill_pct = min(1.0, penetration / fvg.size) if fvg.size > 0 else 1.0
    return fvg


def select_valid_fvg(
    candles: List[Candle],
    direction: Direction,
    from_index: int,
    min_size: float,
    max_fill: float = 0.5,
    same_day_scope: bool = False,
) -> Optional[FVG]:
    """Return the first FVG of the impulse passing all Rule 4 validations.

    The earliest gap of the impulse leg is the trigger (best entry price).
    Checks: minimum size, fill < max_fill, not closed through, session scope.

    Session scope: forex FVGs must be formed in the same session as the
    latest candle (no London -> NY carry-over); for 24/7 crypto pass
    same_day_scope=True — the FVG stays valid for the whole Prague day.
    """
    now = candles[-1].timestamp
    in_scope = same_trading_day if same_day_scope else same_session
    for fvg in find_fvgs(candles, direction, from_index):
        if fvg.size < min_size:
            continue
        fvg = measure_fill(candles, fvg)
        if fvg.closed_through or fvg.fill_pct >= max_fill:
            continue
        if not in_scope(fvg.timestamp, now):
            continue
        return fvg
    return None


def best_rejected_fvg(
    candles: List[Candle],
    direction: Direction,
    from_index: int,
    min_size: float,
    max_fill: float = 0.5,
    same_day_scope: bool = False,
) -> Optional[Tuple[FVG, List[str]]]:
    """Diagnostics: the candidate that came closest to passing Rule 4.

    Returns (fvg, problems) where problems is a subset of
    {"size", "fill", "closed", "session"}, or None when no gap formed at all.
    Used to explain WATCH verdicts ("best candidate was 3.2 pips of 5").
    """
    now = candles[-1].timestamp
    in_scope = same_trading_day if same_day_scope else same_session
    best: Optional[Tuple[FVG, List[str]]] = None
    best_key = None
    for fvg in find_fvgs(candles, direction, from_index):
        fvg = measure_fill(candles, fvg)
        problems = []
        if fvg.size < min_size:
            problems.append("size")
        if fvg.closed_through:
            problems.append("closed")
        elif fvg.fill_pct >= max_fill:
            problems.append("fill")
        if not in_scope(fvg.timestamp, now):
            problems.append("session")
        key = (len(problems), -fvg.size)  # fewest problems, then largest gap
        if best_key is None or key < best_key:
            best, best_key = (fvg, problems), key
    return best
