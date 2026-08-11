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

# SHORT-side mirrors of H4_UPTREND_CLOSES / H1_PULLBACK_CLOSES, built by
# reflecting every close around K=6270 (close' = K - close). find_pivots,
# detect_trend, build_zone etc. are pure relational comparisons on price
# differences, so this reflection is an exact structural mirror: every
# bullish candle becomes bearish (and vice versa) at the same indices, every
# HH+HL uptrend becomes a LL+LH downtrend, every high pivot becomes a low
# pivot at the mirrored price. Verified by running TripleSyncEngine against
# both the reflected-closes version and this hand-transcribed literal list
# and confirming identical output (see task-3-report.md finding 1).
#
# H4 downtrend: LL (3170 -> 3070 -> 2970) + LH (3220 -> 3150)
H4_DOWNTREND_CLOSES = [
    3270, 3250, 3230, 3210, 3190, 3170,  # down leg
    3180, 3195, 3210, 3220,              # pullback (LH at 3220)
    3200, 3170, 3130, 3100, 3070,        # down leg (LL)
    3085, 3110, 3130, 3150,              # pullback (LH)
    3120, 3070, 3020, 2970,              # down leg (LL)
    2980, 2990,                          # confirmation candles
]

# H1: decline, pullback forming a supply pivot at 3138 (zone ~3132-3139),
# decline to 3050 (untested demand ~3049-3070), drift up.
H1_RALLY_INTO_SUPPLY_CLOSES = [
    3170, 3160, 3150, 3135, 3120,
    3125, 3132, 3138,
    3125, 3110, 3090, 3070, 3050,
    3060, 3075, 3090, 3105,
]


# M5: decline into the H1 demand zone, bullish CHoCH + FVG.
# - lower-high pivot at index 6 (3148)
# - protective pivot low at index 10 (3130), inside the zone
# - bullish FVG between high[13]=3136 and low[15]=3139.5 (size 3.5)
# - CHoCH at index 16 (close 3149 > 3148)
_M5_LONG_TRIGGER_SPEC = [
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


def m5_long_trigger() -> List[Candle]:
    """M5: decline into the H1 demand zone, bullish CHoCH + FVG.

    See _M5_LONG_TRIGGER_SPEC for the shape.
    """
    return [candle(*row, index=i) for i, row in enumerate(_M5_LONG_TRIGGER_SPEC)]


def m5_long_trigger_reentry(
    invalidated: bool = True, start: datetime = SESSION_BASE
) -> List[Candle]:
    """m5_long_trigger preceded by an EARLIER excursion into the demand zone
    (3131.0-3138.0 from H1_PULLBACK_CLOSES).

    invalidated=True is the stop-hunt shape: the early excursion body-closes
    at 3128 below the zone bottom (invalidation), price exits above, then the
    normal re-entry trigger plays out. The zone is dead from that close
    onward, whatever price does next — the engine must SKIP.

    invalidated=False is the control: the early excursion holds the zone
    (close 3132), so the later trigger is a legitimate second touch and must
    still approve.

    Pass a `start` after the H1 zone pivot's timestamp (the pivot forms 7
    hours into H1_PULLBACK_CLOSES) so the M5 candles read as post-formation
    price action, the way live data always does.
    """
    if invalidated:
        prelude = [
            (3160.0, 3161.0, 3155.0, 3156.0),
            (3156.0, 3157.0, 3144.0, 3145.0),  # above the zone (low > 3138)
            (3145.0, 3146.0, 3136.0, 3137.0),  # enters the zone
            (3137.0, 3138.5, 3127.0, 3128.0),  # body close 3128 < 3131 — dead
            (3128.0, 3130.5, 3126.0, 3130.0),  # fully below (high < 3131)
            (3130.0, 3150.0, 3129.0, 3149.0),  # rallies back through the zone
            (3149.0, 3151.0, 3143.0, 3144.0),  # above again (low > 3138)
        ]
    else:
        prelude = [
            (3160.0, 3161.0, 3155.0, 3156.0),
            (3156.0, 3157.0, 3144.0, 3145.0),
            (3145.0, 3146.0, 3136.0, 3137.0),  # enters the zone
            (3137.0, 3138.5, 3131.5, 3132.0),  # holds inside (close >= 3131)
            (3132.0, 3150.0, 3131.5, 3149.0),  # exits upward through the top
            (3149.0, 3151.0, 3143.0, 3144.0),  # above the zone (low > 3138)
            (3144.0, 3146.0, 3141.0, 3145.0),  # still above
        ]
    spec = prelude + _M5_LONG_TRIGGER_SPEC
    return [candle(*row, index=i, start=start) for i, row in enumerate(spec)]


def m5_long_trigger_deep_sweep() -> List[Candle]:
    """M5 where the sweep low and the last fractal pivot DISAGREE.

    Against the H1 demand zone 3131.0-3138.0 (built from H1_PULLBACK_CLOSES):
    - lower-high pivot at index 6 (3148) is the CHoCH reference
    - the excursion into the zone starts at index 8 and runs to index 14
    - index 9 spikes down to 3126 — the real swept low
    - a shallower fractal low forms later at index 12 (3132.5)
    - bullish FVG between high[13]=3138.0 and low[15]=3140.5 (size 2.5)
    - CHoCH at index 16 (close 3149 > 3148)

    `last_protective_pivot` returned the LAST pivot at/before the CHoCH —
    3132.5, above the sweep. `sweep_extreme` returns 3126.0.
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
        (3142.0, 3143.0, 3134.0, 3136.0),
        (3136.0, 3137.0, 3126.0, 3135.0),
        (3135.0, 3136.0, 3133.5, 3134.5),
        (3134.5, 3135.5, 3133.0, 3135.0),
        (3135.0, 3136.0, 3132.5, 3135.5),
        (3135.5, 3138.0, 3135.0, 3137.5),
        (3137.5, 3141.0, 3137.0, 3140.8),
        (3140.8, 3144.0, 3140.5, 3143.5),
        (3143.5, 3149.5, 3143.0, 3149.0),
        (3149.0, 3151.0, 3147.0, 3150.0),
        (3150.0, 3152.0, 3148.0, 3151.0),
        (3151.0, 3152.0, 3149.0, 3150.0),
    ]
    return [candle(*row, index=i) for i, row in enumerate(spec)]


def m5_short_trigger_deep_sweep() -> List[Candle]:
    """SHORT mirror of m5_long_trigger_deep_sweep (each OHLC reflected
    around K=6270: open'=K-open, high'=K-low, low'=K-high, close'=K-close —
    see the comment above H4_DOWNTREND_CLOSES).

    Against the H1 supply zone 3132.0-3139.0 (built from
    H1_RALLY_INTO_SUPPLY_CLOSES):
    - higher-low pivot at index 6 (3122) is the CHoCH reference
    - the excursion into the zone starts at index 8 and runs to index 14
    - index 9 spikes up to 3144 — the real swept high
    - a shallower fractal high forms later at index 12 (3137.5)
    - bearish FVG between low[13]=3132.0 and high[15]=3129.5 (size 2.5)
    - CHoCH at index 16 (close below the 3122 higher-low)

    `sweep_extreme` returns 3144.0, above the shallower pivot at 3137.5 —
    the SHORT-side analogue of the LONG fixture's sweep/pivot disagreement.
    Independently confirmed by running the engine against this fixture:
    entry 3129.5, stop_loss 3146.0, target H1 swing low 3049.0,
    take_profit 3051.0, rr 4.76 (see task-3-report.md finding 1).
    """
    spec = [
        (3110.0, 3115.0, 3109.0, 3114.0),
        (3114.0, 3119.0, 3113.0, 3118.0),
        (3118.0, 3123.0, 3117.0, 3122.0),
        (3122.0, 3127.0, 3121.0, 3126.0),
        (3126.0, 3130.0, 3125.0, 3128.0),
        (3128.0, 3129.0, 3124.0, 3125.0),
        (3125.0, 3126.0, 3122.0, 3123.0),
        (3123.0, 3129.0, 3122.5, 3128.0),
        (3128.0, 3136.0, 3127.0, 3134.0),
        (3134.0, 3144.0, 3133.0, 3135.0),
        (3135.0, 3136.5, 3134.0, 3135.5),
        (3135.5, 3137.0, 3134.5, 3135.0),
        (3135.0, 3137.5, 3134.0, 3134.5),
        (3134.5, 3135.0, 3132.0, 3132.5),
        (3132.5, 3133.0, 3129.0, 3129.2),
        (3129.2, 3129.5, 3126.0, 3126.5),
        (3126.5, 3127.0, 3120.5, 3121.0),
        (3121.0, 3123.0, 3119.0, 3120.0),
        (3120.0, 3122.0, 3118.0, 3119.0),
        (3119.0, 3121.0, 3118.0, 3120.0),
    ]
    return [candle(*row, index=i) for i, row in enumerate(spec)]


def m5_choch_still_in_zone() -> List[Candle]:
    """M5 where the bullish CHoCH forms and price STAYS inside the demand zone
    to the end of the series — the exact case the touch-span fix targets.

    Against a demand zone of 3130-3140:
    - a high pivot at index 2 (high 3149) sits ABOVE the zone (indices 0-4
      never enter it), so it is a valid CHoCH reference level
    - price declines into the zone at index 5; the contiguous excursion runs
      indices 5-10 with no exit before the series ends
    - the CHoCH candle at index 8 dips into the zone (low 3137.6) while its
      body closes at 3151, breaking the 3149 pivot
    - price then hovers in the zone at indices 9-10

    The old `zone_touch_index` returned the LAST in-zone candle (index 10,
    after the CHoCH), collapsing `find_choch`'s window to nothing → no trigger.
    `zone_touch_span` returns the excursion START (index 5), so the CHoCH at
    index 8 is inside the search window.
    """
    return make_candles(
        [3143, 3146, 3148, 3145, 3143, 3140, 3138, 3138, 3151, 3140, 3138]
    )
