"""Synthetic candle builders for SMC tests."""

from datetime import datetime, timedelta, timezone
from typing import List, Sequence

from app.services.smc.models import Candle

# Monday 2026-07-06 14:00 UTC = 16:00 Prague (CEST) — inside the NY session.
SESSION_BASE = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)


def make_candles(
    closes: Sequence[float],
    start: datetime = SESSION_BASE,
    step_minutes: int = 5,
) -> List[Candle]:
    """Build a candle series from closes.

    Wicks are asymmetric (bullish candles wick more above, bearish more below)
    so turning points become strict fractal pivots.
    """
    candles = []
    prev = closes[0]
    for i, close in enumerate(closes):
        open_ = prev
        bullish = close >= open_
        high = max(open_, close) + (1.0 if bullish else 0.4)
        low = min(open_, close) - (0.4 if bullish else 1.0)
        candles.append(
            Candle(
                timestamp=start + timedelta(minutes=step_minutes * i),
                open=open_,
                high=high,
                low=low,
                close=close,
            )
        )
        prev = close
    return candles


def candle(
    open_: float,
    high: float,
    low: float,
    close: float,
    index: int = 0,
    start: datetime = SESSION_BASE,
    step_minutes: int = 5,
) -> Candle:
    return Candle(
        timestamp=start + timedelta(minutes=step_minutes * index),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


# H4 uptrend: HH (3101 -> 3201 -> 3301) + HL (3049 -> 3119)
H4_UPTREND_CLOSES = [
    3000, 3020, 3040, 3060, 3080, 3100,  # up leg
    3090, 3075, 3060, 3050,              # pullback (HL at 3050)
    3070, 3100, 3140, 3170, 3200,        # up leg (HH)
    3185, 3160, 3140, 3120,              # pullback (HL)
    3150, 3200, 3250, 3300,              # up leg (HH)
    3290, 3280,                          # confirmation candles
]

# H1: rally, pullback forming a demand pivot at 3132 (zone ~3131-3138),
# rally to 3220 (untested supply ~3200-3221), drift down.
H1_PULLBACK_CLOSES = [
    3100, 3110, 3120, 3135, 3150,
    3145, 3138, 3132,
    3145, 3160, 3180, 3200, 3220,
    3210, 3195, 3180, 3165,
]


def m5_long_trigger() -> List[Candle]:
    """M5: decline into the H1 demand zone, bullish CHoCH + FVG.

    - lower-high pivot at index 6 (3148)
    - protective pivot low at index 10 (3130), inside the zone
    - bullish FVG between high[13]=3136 and low[15]=3139.5 (size 3.5)
    - CHoCH at index 16 (close 3149 > 3148)
    """
    spec = [
        (3160.0, 3161.0, 3155.0, 3156.0),
        (3156.0, 3157.0, 3151.0, 3152.0),
        (3152.0, 3153.0, 3147.0, 3148.0),
        (3148.0, 3149.0, 3143.0, 3144.0),
        (3144.0, 3145.0, 3140.0, 3142.0),
        (3142.0, 3146.0, 3141.0, 3145.0),
        (3145.0, 3148.0, 3144.0, 3147.0),
        (3147.0, 3147.5, 3141.0, 3142.0),
        (3142.0, 3143.0, 3137.0, 3138.0),
        (3138.0, 3139.0, 3133.0, 3134.0),
        (3134.0, 3135.0, 3130.0, 3131.0),
        (3131.0, 3133.0, 3130.5, 3132.0),
        (3132.0, 3134.0, 3131.0, 3133.0),
        (3133.0, 3136.0, 3132.0, 3135.5),
        (3135.5, 3141.0, 3135.0, 3140.5),
        (3140.5, 3144.0, 3139.5, 3143.0),
        (3143.0, 3149.5, 3142.0, 3149.0),
        (3149.0, 3151.0, 3147.0, 3150.0),
        (3150.0, 3152.0, 3148.0, 3151.0),
        (3151.0, 3152.0, 3149.0, 3150.0),
    ]
    return [candle(*row, index=i) for i, row in enumerate(spec)]
