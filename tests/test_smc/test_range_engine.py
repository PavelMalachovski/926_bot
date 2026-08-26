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

from app.services.smc import sniper
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.liquidity import find_liquidity
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.range import (
    boundary_excursion_start,
    boundary_swept,
    boundary_zone,
    detect_range,
)
from app.services.smc.structure import detect_trend, find_choch, zone_touch_span
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

# Two visits, each starting and ending mid-box so they chain either way
# round: one dips into the bottom band, the other reaches the top band.
_M5_BOTTOM_VISIT_SPEC = [
    (3190.0, 3191.0, 3188.0, 3188.5),
    (3188.5, 3189.0, 3184.0, 3184.5),
    (3184.5, 3185.0, 3180.5, 3181.0),
    (3181.0, 3183.0, 3180.0, 3182.5),
    (3182.5, 3186.0, 3182.0, 3185.5),
    (3185.5, 3190.0, 3185.0, 3189.5),
]
_M5_TOP_VISIT_SPEC = [
    (3190.0, 3193.0, 3189.5, 3192.5),
    (3192.5, 3196.0, 3192.0, 3195.5),
    (3195.5, 3199.5, 3195.0, 3199.0),
    (3199.0, 3200.4, 3198.5, 3199.2),
    (3199.2, 3199.5, 3196.0, 3196.5),
    (3196.5, 3197.0, 3189.5, 3190.0),
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


def m5_bottom_then_top():
    return _candles(_M5_BOTTOM_VISIT_SPEC + _M5_TOP_VISIT_SPEC)


def m5_top_then_bottom():
    return _candles(_M5_TOP_VISIT_SPEC + _M5_BOTTOM_VISIT_SPEC)


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


def _top_excursion(h1, m5):
    """(band, excursion_start, choch) for the top boundary — the very window
    the engine measures Rules 3-6 and the ⭐ sweep on."""
    band = boundary_zone(detect_range(h1, TOLERANCE), Direction.SHORT, TOLERANCE)
    touch = zone_touch_span(m5, band)[0]
    choch = find_choch(m5, Direction.SHORT, touch)
    return band, boundary_excursion_start(m5, band, touch), choch


def _sweep_label_at_top(h1, m5):
    """`sniper.sweep_label` on the very excursion the engine measures — the
    top boundary's touch→CHoCH window, with the engine's own arguments."""
    band = boundary_zone(detect_range(h1, TOLERANCE), Direction.SHORT, TOLERANCE)
    touch = zone_touch_span(m5, band)[0]
    choch = find_choch(m5, Direction.SHORT, touch)
    return sniper.sweep_label(
        m5, h1, Direction.SHORT, touch, choch, TOLERANCE,
        _fresh_result().checked_at,
    )


def _boundary_swept_at_top(h1, m5):
    """D9/D13/D16's own predicate on the same excursion — the thing that
    stars a range setup when no named pool is left to credit."""
    _, start, choch = _top_excursion(h1, m5)
    return boundary_swept(m5[start:choch + 1], RANGE_TOP, above=True)


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

    def test_the_boundary_worked_most_recently_wins(self):
        """Both edges visited: the later excursion is the live one. The two
        runs are the same two blocks in either order, so nothing but their
        recency can decide it."""
        after_bottom = _run(m5=m5_bottom_then_top())
        assert after_bottom.direction_source == "range"
        assert after_bottom.h1_zone.is_demand is False  # the top boundary

        after_top = _run(m5=m5_top_then_bottom())
        assert after_top.direction_source == "range"
        assert after_top.h1_zone.is_demand is True  # the bottom boundary

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


def m5_top_invalidated():
    """The top-boundary excursion, then a body close above the top — Rule
    3's invalidation, before any CHoCH has a chance to form."""
    spec = list(_M5_AT_TOP_SPEC[:9]) + [(3199.2, 3203.0, 3199.0, 3202.5)]
    return _candles(spec)


def m5_bottom_invalidated():
    return _candles(_mirrored(list(_M5_AT_TOP_SPEC[:9]) + [
        (3199.2, 3203.0, 3199.0, 3202.5)
    ]))


class TestRangeZoneInvalidationWording:
    """Task 3 wording fix: Rule 3's invalidation sentence used to call every
    zone an 'H1 Demand/Supply zone', which is wrong for a RANGE boundary."""

    def test_top_boundary_invalidation_names_the_range_not_an_h1_zone(self):
        result = _run(m5=m5_top_invalidated())
        assert result.verdict == Verdict.SKIP
        reason = " ".join(result.reasons)
        assert "range HIGH boundary" in reason
        assert "H1 Supply zone" not in reason

    def test_bottom_boundary_invalidation_names_the_range_not_an_h1_zone(self):
        result = _run(m5=m5_bottom_invalidated())
        assert result.verdict == Verdict.SKIP
        reason = " ".join(result.reasons)
        assert "range LOW boundary" in reason
        assert "H1 Demand zone" not in reason

    def test_an_ordinary_h1_zone_invalidation_is_unchanged(self):
        """The fix must not touch the wording for a real H1 Demand/Supply
        zone — only a RANGE boundary gets the new sentence. Same fixture as
        test_engine.py's TestZoneInvalidationMemory."""
        from tests.test_smc.helpers import (
            H1_PULLBACK_CLOSES,
            H4_UPTREND_CLOSES,
            m5_long_trigger_reentry,
        )

        m5_start = SESSION_BASE + timedelta(days=1)
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_reentry(invalidated=True, start=m5_start),
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.SKIP
        assert any("H1 Demand zone" in r for r in result.reasons)


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

    def test_rule_7_does_not_claim_the_setup_has_no_objective(self):
        result = _run()
        assert "no unswept liquidity ahead" not in result.warnings
        assert result.setup.target is None  # the boundary is not a pool


class TestNoHybridExitInRangeMode:
    """D14 (owner decision 2026-08-18): one target, the opposite boundary,
    full size. Risk is anchored beyond the boundary plus a buffer, so RR
    across the box is routinely under 2 — a 2R TP1 would land outside the
    very box the setup is aiming at (here: TP1 3176.40 and runner 3167.60
    against a range low of 3179.20)."""

    def test_a_range_setup_carries_no_hybrid_levels(self):
        setup = _run().setup
        assert setup.tp1 is None
        assert setup.runner_tp is None

    def test_the_one_target_is_still_there(self):
        setup = _run().setup
        assert setup.take_profit == 3181.20  # the opposite boundary − buffer
        assert setup.rr == 1.45

    def test_the_long_side_too(self):
        setup = _run(m5=m5_at_bottom()).setup
        assert (setup.tp1, setup.runner_tp) == (None, None)
        assert setup.take_profit == 3198.80

    def test_a_trend_setup_keeps_the_hybrid_exit_untouched(self):
        """The other half of D14: only RANGE loses the hybrid. A plain
        trend setup must still carry TP1 at 2R and the runner at 3R."""
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
        setup = result.setup
        assert result.direction_source != "range"
        risk = setup.entry - setup.stop_loss
        assert setup.tp1 == round(setup.entry + 2.0 * risk, 2)
        assert setup.runner_tp == round(setup.entry + 3.0 * risk, 2)


def m5_top_pierced_and_reclaimed():
    """The reviewer's stop-hunt (D15): price runs the stops above the range
    high over THREE candles — two of them closing beyond it — and then
    snaps back inside, after which the setup forms as usual."""
    spec = (
        list(_M5_AT_TOP_SPEC[:8])
        + [
            (3199.0, 3202.5, 3198.8, 3202.0),  # body closes above 3200.8
            (3202.0, 3203.5, 3201.0, 3203.0),  # still beyond, second candle
            (3203.0, 3203.2, 3199.0, 3199.5),  # reclaimed — back inside
        ]
        + list(_M5_AT_TOP_SPEC[9:])
    )
    return _candles(spec)


def m5_top_pierced_and_held():
    """The same pierce with no reclaim: the break is still in force on the
    last candle, so the boundary really is gone."""
    spec = list(_M5_AT_TOP_SPEC[:8]) + [
        (3199.0, 3202.5, 3198.8, 3202.0),
        (3202.0, 3203.5, 3201.0, 3203.0),
        (3203.0, 3206.0, 3202.5, 3205.5),
    ]
    return _candles(spec)


class TestBoundaryPierceAndReclaim:
    """D15 (owner decision 2026-08-18): a close beyond a range boundary
    invalidates it only while the break STILL HOLDS at the end of the
    excursion. Before this, the pierce D9 blesses (usually an M5 body
    close) returned `SKIP … invalidated` while the plan message was
    simultaneously advertising the boundary as already swept."""

    def test_the_reclaimed_pierce_is_tradeable_rather_than_a_skip(self):
        result = _run(m5=m5_top_pierced_and_reclaimed())
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.direction_source == "range"
        assert "invalidated" not in " ".join(result.reasons)
        # Rule 6 anchors the stop one buffer beyond the RAID's own high
        # (3203.5 at index 9), not beyond the leg price returned on (3203.2)
        # — see TestTheStopHidesBehindTheWholeRaid for the property itself.
        assert result.setup.stop_loss == 3205.50
        assert result.setup.take_profit == 3181.20

    def test_a_break_that_still_holds_invalidates(self):
        result = _run(m5=m5_top_pierced_and_held())
        assert result.verdict == Verdict.SKIP
        assert "invalidated" in " ".join(result.reasons)
        assert "range HIGH boundary" in " ".join(result.reasons)

    def test_an_ordinary_zone_is_not_made_reclaim_aware(self):
        """The one-way memory on an OB/FVG zone is load-bearing (review
        2026-08-11: a stop-hunt through a demand zone alerted on re-entry)
        and D15 must not reach it. Same fixture as test_engine.py's
        TestZoneInvalidationMemory: price closes below the demand zone and
        then comes back — still a SKIP."""
        from tests.test_smc.helpers import m5_long_trigger_reentry

        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_reentry(
                invalidated=True, start=SESSION_BASE + timedelta(days=1)
            ),
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.SKIP
        assert "invalidated" in " ".join(result.reasons)


def m5_top_raid_split_by_an_outside_candle():
    """The reviewer's staged raid (2026-08-18): three candles run the stops
    above the range high, and the MIDDLE one trades wholly above the band
    (low 3204.0 against a band top of 3200.8). `zone_touch_span` therefore
    sees two runs and returns only the second — the leg price had already
    come back on — so Rule 6's stop reference misses the raid's own peak
    (3215.0) entirely."""
    spec = (
        list(_M5_AT_TOP_SPEC[:8])
        + [
            (3199.0, 3202.5, 3198.8, 3202.0),  # pierce, still touching the band
            (3202.0, 3215.0, 3204.0, 3205.0),  # WHOLLY above the band
            (3205.0, 3205.5, 3199.0, 3199.5),  # reclaimed — back inside
        ]
        + list(_M5_AT_TOP_SPEC[9:])
    )
    return _candles(spec)


def m5_top_raid_uninterrupted():
    """The same shape with every raid candle still dipping into the band:
    the excursion is never split, so the widened anchor must agree with the
    one `zone_touch_span` alone would have given."""
    spec = (
        list(_M5_AT_TOP_SPEC[:8])
        + [
            (3199.0, 3203.0, 3198.7, 3202.5),
            (3202.5, 3206.0, 3199.5, 3200.0),
            (3200.0, 3200.5, 3197.0, 3197.5),
        ]
        + list(_M5_AT_TOP_SPEC[9:])
    )
    return _candles(spec)


def _assert_stop_hides_behind(result, candles, raid, long=False):
    """The property: the stop sits beyond EVERY candle of the raid — one
    buffer past the raid's own extreme, not past the leg price returned on.

    `raid` is the slice of candles the fixture's docstring calls the raid,
    named there rather than recomputed here: recomputing it with the
    production helper would only assert that the code agrees with itself.
    """
    setup = result.setup
    assert setup is not None
    if long:
        deepest = min(c.low for c in candles[raid])
        assert setup.stop_loss < deepest
        assert setup.stop_loss == pytest.approx(deepest - 2.0)  # sl_buffer
    else:
        highest = max(c.high for c in candles[raid])
        assert setup.stop_loss > highest
        assert setup.stop_loss == pytest.approx(highest + 2.0)


class TestTheStopHidesBehindTheWholeRaid:
    """Rule 6 anchors the stop beyond the swept extreme so it cannot be
    taken with the liquidity the sweep took. On a range boundary the band
    is one tolerance thick (`boundary_zone`), so a raid that prints a
    candle wholly outside it splits the excursion in two and the last-run
    anchor lands INSIDE the raid — reachable only since D15 made the
    body-close pierce tradeable (spec §3.3, reviewer 2026-08-18).

    Every test here asserts the same property across a different raid
    shape; none of them pins a number the fixture happens to produce.
    """

    def test_the_staged_raid_really_does_split_the_excursion(self):
        """The precondition: without the split there is nothing to heal."""
        m5 = m5_top_raid_split_by_an_outside_candle()
        band = boundary_zone(
            detect_range(h1_ranging(), TOLERANCE), Direction.SHORT, TOLERANCE
        )
        assert zone_touch_span(m5, band)[0] == 10  # the re-entry leg alone
        assert m5[9].low > band.top  # the raid candle trades wholly outside

    def test_a_raid_interrupted_by_a_candle_outside_the_band(self):
        m5 = m5_top_raid_split_by_an_outside_candle()
        result = _run(m5=m5)
        _assert_stop_hides_behind(result, m5, slice(8, 11))

    def test_a_single_candle_wick_raid(self):
        """Today's common case: one clean wick through the boundary, no
        split at all."""
        m5 = m5_at_top_pierced(3204.0)
        result = _run(h1=h1_ranging_swept(3204.0), m5=m5)
        _assert_stop_hides_behind(result, m5, slice(8, 9))

    def test_an_uninterrupted_multi_candle_raid(self):
        m5 = m5_top_raid_uninterrupted()
        result = _run(m5=m5)
        _assert_stop_hides_behind(result, m5, slice(8, 11))

    def test_a_visit_with_no_raid_anchors_on_the_boundary_itself(self):
        """Nothing pierced the boundary, so the box's own edge is the
        furthest reference and the stop sits one buffer beyond it — above
        every candle of the visit either way."""
        m5 = m5_at_top()
        result = _run(m5=m5)
        assert result.setup.stop_loss == RANGE_TOP + 2.0
        assert result.setup.stop_loss > max(c.high for c in m5[7:10])

    def test_the_long_side_mirrors_it(self):
        m5 = _candles(_mirrored([
            *_M5_AT_TOP_SPEC[:8],
            (3199.0, 3202.5, 3198.8, 3202.0),
            (3202.0, 3215.0, 3204.0, 3205.0),
            (3205.0, 3205.5, 3199.0, 3199.5),
            *_M5_AT_TOP_SPEC[9:],
        ]))
        result = _run(m5=m5)
        assert result.setup.direction == Direction.LONG
        _assert_stop_hides_behind(result, m5, slice(8, 11), long=True)

    def test_the_setup_is_still_announced(self):
        """Detector mode: a wider stop costs RR, and RR is a label. The
        raid must not talk the bot out of the setup it just formed."""
        result = _run(m5=m5_top_raid_split_by_an_outside_candle())
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert any("RR to the opposite boundary" in w
                   for w in result.warnings)

    def test_the_choch_and_fvg_search_still_anchor_on_the_touch(self):
        """Only Rule 6's stop reference and the D13 sweep window widen; the
        entry the setup reports comes from the same imbalance as ever."""
        result = _run(m5=m5_top_raid_split_by_an_outside_candle())
        assert result.setup.entry == 3194.0


class TestTheBoundaryIsNotOfferedAsADeeperEntry:
    """The ladder lists untested zones further out as alternative entries.
    A RANGE band is synthetic (a cluster mean ± tolerance) and never equals
    a pivot-built order block's bounds, so exact-bounds exclusion let the
    H1 block sitting ON the boundary come back as a "deeper entry" at the
    level the setup had just entered from (review 2026-08-18)."""

    def test_the_h1_block_on_the_boundary_is_excluded_by_overlap(self):
        from app.services.smc.structure import zone_ladder

        h1 = h1_ranging()
        band = boundary_zone(
            detect_range(h1, TOLERANCE), Direction.SHORT, TOLERANCE
        )
        # The defect, still reproducible with the boundary not excluded:
        # an untested H1 block straddling the 3198.80–3200.80 band.
        naked = zone_ladder(h1, Direction.SHORT, 3194.0)
        assert any(
            z.bottom <= band.top and band.bottom <= z.top for z in naked
        )
        rungs = zone_ladder(h1, Direction.SHORT, 3194.0, exclude=band)
        assert not any(
            z.bottom <= band.top and band.bottom <= z.top for z in rungs
        )

    def test_the_range_setup_offers_no_rung_over_its_own_boundary(self):
        setup = _run().setup
        box = _run().market_range
        assert not any(
            z.bottom <= box.top and box.top - TOLERANCE <= z.top
            for z in setup.zones_ahead
        )

    def test_an_ordinary_setup_still_excludes_only_its_own_zone(self):
        """Overlap exclusion is RANGE-only: an OB `exclude` keeps matching
        by bounds, so a neighbouring zone that merely overlaps it stays on
        the ladder."""
        from app.services.smc.models import Zone
        from app.services.smc.structure import zone_ladder

        h1 = h1_ranging()
        overlapping = Zone(
            bottom=3196.0, top=3199.0, is_demand=False, pivot_index=-1,
            timestamp=SESSION_BASE, kind="OB",
        )
        rungs = zone_ladder(h1, Direction.SHORT, 3194.0, exclude=overlapping)
        assert [(z.bottom, z.top) for z in rungs] == [(3196.0, 3200.6)]


def m5_at_top_body_pierced():
    """m5_at_top with the excursion's highest candle closing ABOVE the top
    (body 3199.0-3203.5 against a boundary of 3200.8) and the next candle
    closing back inside — the raid D15 made tradeable, which is not a
    wick-only pierce."""
    spec = list(_M5_AT_TOP_SPEC)
    open_, _, low, _ = spec[8]
    spec[8] = (open_, 3204.0, low, 3203.5)
    return _candles(spec)


class TestRangeStarTier:
    """D9: a boundary pierced and reclaimed is liquidity taken. D13: only
    when the piercing happened in the setup's own excursion. D16: the
    pierce counts whether it closed beyond the boundary or only wicked
    through it."""

    def test_an_untouched_boundary_misses_the_sweep(self):
        setup = _run().setup
        assert setup.tier_star is False
        assert setup.tier_missed == ["sweep"]

    def test_a_shallow_sweep_is_named_by_a_liquidity_pool(self):
        """Pierced by 0.7 — less than the tolerance, so the boundary's EQH
        pool survives `find_liquidity._is_swept` and a named pool, not the
        range flag, is what `sweep_label` credits.

        Which name wins is priority, not merit: a range top touched on both
        days IS the previous day's high, so PDH outranks the EQH pool made
        of the same highs. Both are real; the point of this test is that
        something was named at all.
        """
        h1, m5 = h1_ranging_swept(3201.5), m5_at_top_pierced(3201.5)
        pools = find_liquidity(h1, "H1", TOLERANCE)
        assert any(
            lv.is_high and lv.equal_count >= 2
            and abs(lv.price - RANGE_TOP) <= TOLERANCE
            for lv in pools
        ), "a sub-tolerance pierce must leave the boundary's EQH pool intact"
        assert _sweep_label_at_top(h1, m5) == "PDH"
        assert _run(h1=h1, m5=m5).setup.tier_star is True

    def test_a_deep_sweep_still_earns_the_star(self):
        """Pierced by 3.2 — more than the tolerance, so `find_liquidity`
        drops the boundary's own EQH pool. The boundary sweep measured on
        the excursion itself is what carries the star here, and it is
        asserted directly rather than through `sweep_label` returning None:
        piercing a range top on the second day also takes the previous
        day's high, so a named pool legitimately exists as well.
        """
        h1, m5 = h1_ranging_swept(3204.0), m5_at_top_pierced(3204.0)
        assert not any(
            lv.is_high and abs(lv.price - RANGE_TOP) <= TOLERANCE
            for lv in find_liquidity(h1, "H1", TOLERANCE)
        ), "a pierce deeper than the tolerance must delete the boundary pool"
        assert _boundary_swept_at_top(h1, m5) is True
        setup = _run(h1=h1, m5=m5).setup
        assert setup.tier_star is True
        assert setup.tier_missed == []

    def test_a_sweep_outside_this_excursion_does_not_earn_the_star(self):
        """D13: the H1 box remembers a raid on the top from earlier in the
        window, but this setup's own touch→CHoCH excursion took nothing —
        `Range.swept_top` alone must not star it."""
        h1, m5 = h1_ranging_swept(3204.0), m5_at_top()
        assert detect_range(h1, TOLERANCE).swept_top is True
        setup = _run(h1=h1, m5=m5).setup
        assert setup.tier_star is False
        assert setup.tier_missed == ["sweep"]

    def test_a_reclaimed_body_close_pierce_earns_the_star(self):
        """D16 (owner decision 2026-08-18): the raid D15 made tradeable is
        the same raid the owner calls a sweep, so the two rules must agree
        about it. The pierce is deeper than the tolerance, so the boundary's
        own pool is gone and the boundary predicate is what has to answer —
        asserted here directly, since a raid above a range top also takes
        the previous day's high and `sweep_label` names that instead."""
        h1, m5 = h1_ranging_swept(3204.0), m5_at_top_body_pierced()
        assert m5[8].body_high > RANGE_TOP  # not a wick-only pierce
        assert m5[9].close < RANGE_TOP      # but price came back inside
        assert _boundary_swept_at_top(h1, m5) is True
        setup = _run(h1=h1, m5=m5).setup
        assert setup.tier_star is True
        assert setup.tier_missed == []

    def test_a_pierce_that_never_comes_back_is_no_sweep(self):
        """The other half of D16: only a RECLAIMED pierce is a sweep. Here
        the break still holds, so Rule 3 invalidates the boundary and there
        is no setup to star at all (D15)."""
        result = _run(m5=m5_top_pierced_and_held())
        assert result.verdict == Verdict.SKIP
        assert result.setup is None
