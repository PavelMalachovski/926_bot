"""The engine trades the range between its boundaries (spec §3.2, D9, D11).

Every fixture here is a real `TripleSyncEngine.evaluate()` run on synthetic
candles — nothing in the pipeline is mocked.

The H1 series oscillates twice between ~3200 and ~3179, which gives
`detect_range` two clusters of two confirmed pivots each: the top at 3200.8
(highs 3201.0 and 3200.6) and the bottom at 3179.2 (lows 3179.0 and 3179.4).
The box is 21.6 wide against an ETHUSD tolerance of 2.0 (the raw
`instrument.min_fvg`), so it clears the 3x floor. The last two highs descend
and the last two lows ascend, so `detect_trend` reads FLAT on both H4 and H1
— the D11 state where the range is in play.

The M5 series is built against the top boundary band [3198.8, 3200.8] and
mirrored around K = 3200.8 + 3179.2 = 6380.0 for the bottom: reflecting
every OHLC value around K maps the top band onto the bottom band exactly, so
the LONG case is the structural mirror of the SHORT one (the same technique
helpers.py documents for H4_DOWNTREND_CLOSES).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.range import detect_range
from app.services.smc.structure import detect_trend
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    SESSION_BASE,
    candle,
    m5_long_trigger,
    make_candles,
)

TOLERANCE = 2.0  # ETHUSD min_fvg, the raw per-instrument value
RANGE_TOP = 3200.8
RANGE_BOTTOM = 3179.2
MIRROR = RANGE_TOP + RANGE_BOTTOM  # reflect the SHORT fixture onto the LONG one

RANGING_CLOSES = [
    3180.0, 3184.0, 3190.0, 3196.0, 3200.0, 3196.0, 3190.0, 3184.0, 3180.0,
    3184.0, 3190.0, 3196.0, 3199.6, 3196.0, 3190.0, 3184.0, 3180.4,
    3184.0, 3190.0, 3194.0,
]

# The M5 window opens after the whole H1 series, the way live data always
# does: the newest closed H1 candle is older than the newest closed M5 one.
M5_START = SESSION_BASE + timedelta(hours=20)

# M5 into the top boundary and back out:
#   index 2  — confirmed swing low at 3183.0 (the CHoCH reference)
#   index 7-9 — the excursion into the band [3198.8, 3200.8]
#   index 11 — bearish FVG 3194.0-3197.0 (size 3.0, unfilled)
#   index 12 — body closes at 3182.5, below the 3183.0 low: the CHoCH
_M5_AT_TOP_SPEC = [
    (3186.0, 3187.5, 3185.0, 3187.0),
    (3187.0, 3188.0, 3185.5, 3186.0),
    (3186.0, 3186.5, 3183.0, 3184.0),
    (3184.0, 3187.0, 3183.5, 3186.5),
    (3186.5, 3190.0, 3186.0, 3189.5),
    (3189.5, 3193.0, 3189.0, 3192.5),
    (3192.5, 3196.0, 3192.0, 3195.5),
    (3195.5, 3199.5, 3195.0, 3199.0),
    (3199.0, 3200.5, 3198.6, 3199.2),
    (3199.2, 3200.2, 3197.0, 3197.5),
    (3197.5, 3198.0, 3193.0, 3193.5),
    (3193.5, 3194.0, 3188.0, 3188.5),
    (3188.5, 3189.0, 3182.0, 3182.5),
]

# Price drifting in the middle of the box, never reaching either band.
_M5_MID_RANGE_SPEC = [
    (3189.0, 3190.5, 3188.0, 3190.0),
    (3190.0, 3191.0, 3188.5, 3189.0),
    (3189.0, 3190.0, 3187.5, 3188.0),
    (3188.0, 3189.5, 3187.0, 3189.0),
] * 3


def _candles(spec, start=M5_START, step_minutes=5):
    return [
        candle(*row, index=i, start=start, step_minutes=step_minutes)
        for i, row in enumerate(spec)
    ]


def _mirrored(spec):
    """Reflect an OHLC spec around MIRROR: open'=K-open, high'=K-low,
    low'=K-high, close'=K-close."""
    return [
        (MIRROR - o, MIRROR - low, MIRROR - high, MIRROR - c)
        for (o, high, low, c) in spec
    ]


def m5_at_top_pierced(wick_high):
    """m5_at_top with the excursion's highest candle wicking through the top
    and closing back inside — D9's liquidity sweep."""
    spec = list(_M5_AT_TOP_SPEC)
    open_, _, low, close = spec[8]
    spec[8] = (open_, wick_high, low, close)
    return _candles(spec)


def h1_ranging():
    return make_candles(RANGING_CLOSES, step_minutes=60)


def h1_ranging_swept(wick_high):
    """The ranging H1 plus one closed candle whose wick pierced the top and
    whose body closed back inside (D9).

    It is the newest candle, so `find_pivots` cannot confirm it (a pivot
    needs two closed candles after it): the clusters, the boundaries and the
    FLAT trend are all unchanged, only `swept_top` flips.
    """
    return h1_ranging() + [
        candle(3194.0, wick_high, 3193.0, 3196.0, index=20,
               start=SESSION_BASE, step_minutes=60)
    ]


def h1_ranging_broken():
    """The same box, then three candles closing above the top."""
    return make_candles(
        RANGING_CLOSES + [3204.0, 3208.0, 3212.0], step_minutes=60
    )


def h1_uptrend_over_a_range():
    """H1 trending UP while an unbroken range is still detectable (D11).

    The three extra candles print a confirmed higher high (wick 3201.5)
    above the top cluster while the last two lows still ascend, so
    `detect_trend` reads UP; the two-pivot clusters are still the largest,
    so `detect_range` still returns a live box.
    """
    extra = [
        (3194.0, 3201.5, 3193.0, 3196.0),
        (3196.0, 3197.0, 3192.0, 3193.0),
        (3193.0, 3194.0, 3190.0, 3191.0),
    ]
    return h1_ranging() + [
        candle(*row, index=20 + i, start=SESSION_BASE, step_minutes=60)
        for i, row in enumerate(extra)
    ]


def h4_flat():
    return make_candles(RANGING_CLOSES, step_minutes=240)


def m5_at_top():
    return _candles(_M5_AT_TOP_SPEC)


def m5_at_bottom():
    return _candles(_mirrored(_M5_AT_TOP_SPEC))


def m5_mid_range():
    return _candles(_M5_MID_RANGE_SPEC)


def _fresh_result() -> AnalysisResult:
    return AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.SKIP,
        checked_at=M5_START + timedelta(minutes=60),
    )


def _engine(**kwargs) -> TripleSyncEngine:
    defaults = dict(
        min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=99.0
    )
    defaults.update(kwargs)
    return TripleSyncEngine(**defaults)


def _run(h1=None, m5=None, h4=None) -> AnalysisResult:
    return _engine().evaluate(
        h4=h4 or h4_flat(),
        h1=h1 or h1_ranging(),
        m5=m5 or m5_at_top(),
        result=_fresh_result(),
    )


class TestFixtures:
    """The preconditions every other test in this file leans on."""

    def test_both_timeframes_read_flat(self):
        assert detect_trend(h4_flat()) is Trend.FLAT
        assert detect_trend(h1_ranging()) is Trend.FLAT

    def test_the_box_is_where_the_tests_say_it_is(self):
        rng = detect_range(h1_ranging(), TOLERANCE)
        assert rng is not None and not rng.broken
        assert rng.top == pytest.approx(RANGE_TOP)
        assert rng.bottom == pytest.approx(RANGE_BOTTOM)


class TestRangeDirection:
    def test_price_at_the_top_shorts_toward_the_bottom(self):
        result = _run()
        assert result.direction_source == "range"
        assert result.setup is not None
        assert result.setup.direction == Direction.SHORT
        assert result.verdict == Verdict.APPROVED_LIMIT

    def test_price_at_the_bottom_goes_long(self):
        result = _run(m5=m5_at_bottom())
        assert result.direction_source == "range"
        assert result.setup.direction == Direction.LONG
        assert result.verdict == Verdict.APPROVED_LIMIT

    def test_the_boundary_is_the_zone_the_setup_formed_at(self):
        result = _run()
        zone = result.h1_zone
        assert zone.kind == "RANGE"
        assert zone.is_demand is False
        assert zone.top == pytest.approx(RANGE_TOP)
        assert zone.bottom == pytest.approx(RANGE_TOP - TOLERANCE)

    def test_at_the_boundary_without_a_trigger_is_a_watch(self):
        # The excursion into the band, cut before the CHoCH candle.
        result = _run(m5=m5_at_top()[:10])
        assert result.verdict == Verdict.WATCH
        assert result.direction_source == "range"
        assert result.setup is None

    def test_mid_range_is_a_watch_with_no_direction(self):
        result = _run(m5=m5_mid_range())
        assert result.verdict == Verdict.WATCH
        assert result.setup is None
        assert result.direction_source != "range"
        reason = " ".join(result.reasons)
        assert "3179.20" in reason and "3200.80" in reason

    def test_a_broken_range_is_ignored(self):
        result = _run(h1=h1_ranging_broken())
        assert result.verdict == Verdict.SKIP
        assert "no direction" in " ".join(result.reasons).lower()
        assert result.market_range is None

    def test_a_trending_h1_still_wins(self):
        """D11: the range is in play only when BOTH timeframes read FLAT."""
        h1 = h1_uptrend_over_a_range()
        # The precondition: a live range really is there to be stolen.
        rng = detect_range(h1, TOLERANCE)
        assert rng is not None and not rng.broken
        assert detect_trend(h1) is Trend.UP

        result = _run(h1=h1, m5=m5_at_bottom())
        assert result.direction_source == "h1"
        assert result.market_range is None


class TestMarketRangeOnTheResult:
    def test_recorded_while_the_range_is_in_play(self):
        result = _run()
        assert result.market_range is not None
        assert result.market_range.top == pytest.approx(RANGE_TOP)
        assert result.market_range.bottom == pytest.approx(RANGE_BOTTOM)

    def test_recorded_even_when_price_sits_mid_range(self):
        result = _run(m5=m5_mid_range())
        assert result.market_range is not None

    def test_none_for_an_ordinary_trend_setup(self):
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=AnalysisResult(
                symbol="ETHUSD",
                verdict=Verdict.SKIP,
                checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
            ),
        )
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.market_range is None


class TestRangeGeometry:
    """SL beyond the boundary, TP at the opposite one, both by one buffer."""

    def test_short_stop_and_target_come_from_the_boundaries(self):
        setup = _run().setup
        # Entry: the bearish FVG's proximal edge (3194.0-3197.0).
        assert setup.entry == 3194.0
        # The excursion's highest wick (3200.5) sits inside the top, so the
        # boundary is the reference: 3200.8 + 2.0 buffer.
        assert setup.stop_loss == 3202.8
        # The objective is the opposite boundary, one buffer short of it.
        assert setup.take_profit == 3181.2

    def test_long_stop_and_target_come_from_the_boundaries(self):
        setup = _run(m5=m5_at_bottom()).setup
        assert setup.entry == 3186.0
        assert setup.stop_loss == 3177.2  # 3179.2 - 2.0
        assert setup.take_profit == 3198.8  # 3200.8 - 2.0

    def test_the_stop_uses_the_wick_when_it_ran_past_the_boundary(self):
        """Whichever is further out — the stop is never inside the sweep."""
        result = _run(
            h1=h1_ranging_swept(3204.0), m5=m5_at_top_pierced(3204.0)
        )
        assert result.setup.stop_loss == 3206.0  # 3204.0 wick + 2.0

    def test_risk_is_positive_and_rr_follows_from_the_two_levels(self):
        setup = _run().setup
        risk = setup.stop_loss - setup.entry
        assert risk > 0
        assert setup.rr == round((setup.entry - setup.take_profit) / risk, 2)
        assert setup.rr == 1.45  # 12.8 reward over 8.8 risk

    def test_the_hybrid_exit_is_still_computed_from_risk(self):
        setup = _run().setup
        risk = setup.stop_loss - setup.entry
        assert setup.tp1 == round(setup.entry - 2.0 * risk, 2)
        assert setup.runner_tp == round(setup.entry - 3.0 * risk, 2)

    def test_rule_7_does_not_claim_the_setup_has_no_objective(self):
        result = _run()
        assert "no unswept liquidity ahead" not in result.warnings
        assert result.setup.target is None  # the boundary is not a pool


class TestRangeStarTier:
    """D9: a boundary pierced by a wick and reclaimed is liquidity taken."""

    def test_an_untouched_boundary_misses_the_sweep(self):
        setup = _run().setup
        assert setup.tier_star is False
        assert setup.tier_missed == ["sweep"]

    def test_a_shallow_sweep_is_named_by_the_liquidity_pool(self):
        # Pierced by 0.7 — less than the tolerance, so the boundary's EQH
        # pool survives `_is_swept` and `sniper.sweep_label` names it.
        result = _run(
            h1=h1_ranging_swept(3201.5), m5=m5_at_top_pierced(3201.5)
        )
        assert result.setup.tier_star is True

    def test_a_deep_sweep_still_earns_the_star(self):
        # Pierced by 3.2 — more than the tolerance, so `find_liquidity` drops
        # the pool and `sweep_label` returns None. The range's own D9 flag is
        # what earns the star here.
        result = _run(
            h1=h1_ranging_swept(3204.0), m5=m5_at_top_pierced(3204.0)
        )
        assert result.setup.tier_star is True
        assert result.setup.tier_missed == []
