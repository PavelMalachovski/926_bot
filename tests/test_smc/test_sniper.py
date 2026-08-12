"""Tests for the sniper tier primitives (sweep/pd/room/classify).

Cases 1-5 are adapted from the reference implementation's tests
(C:\\temp\\926_bot_data\\scripts\\sn_test_rules.py, validated on a year
replay); the rest close the gaps the review ledger called out: SHORT-side
sweeps, EQ pools, H1-sourced pools, boundary crossings, and the classify
truth table.
"""

from datetime import datetime, timedelta, timezone

from app.services.smc.liquidity import LiquidityLevel
from app.services.smc.models import Candle, Direction
from app.services.smc.sniper import (
    MIN_ROOM_R,
    TierVerdict,
    classify,
    dealing_range,
    pd_state,
    room_r,
    sweep_label,
)
from tests.test_smc.helpers import candle

UTC = timezone.utc


def c(i, o, h, low, cl, base=datetime(2026, 3, 2, tzinfo=UTC), tf_min=5):
    return Candle(timestamp=base + timedelta(minutes=tf_min * i),
                  open=o, high=h, low=low, close=cl)


def h1c(i, o, h, low, cl):
    return c(i, o, h, low, cl, tf_min=60)


def make_h1_with_swings():
    """H1 series with a confirmed swing low at 100.0 (idx 4) and a confirmed
    swing high at 110.0 (idx 10), wings of 2 + 2 closed confirmation candles."""
    rows = [
        h1c(0, 104, 105, 103, 104), h1c(1, 104, 104.5, 102, 103),
        h1c(2, 103, 103.5, 101, 102), h1c(3, 102, 102.5, 100.5, 101),
        h1c(4, 101, 101.5, 100.0, 101),   # swing low 100.0
        h1c(5, 101, 103, 100.8, 102.5), h1c(6, 102.5, 104, 102, 103.5),
        h1c(7, 103.5, 106, 103, 105.5), h1c(8, 105.5, 108, 105, 107.5),
        h1c(9, 107.5, 109, 107, 108.5),
        h1c(10, 108.5, 110.0, 108, 109),  # swing high 110.0
        h1c(11, 109, 109.5, 107.5, 108), h1c(12, 108, 108.5, 106.5, 107),
        h1c(13, 107, 107.5, 106, 106.5), h1c(14, 106.5, 107, 105.5, 106),
    ]
    return rows


# --- Ported from sn_test_rules.py (5 cases) --------------------------------


def test_dealing_range_from_confirmed_h1_swings():
    rng = dealing_range(make_h1_with_swings())
    assert rng == (100.0, 110.0)


def test_dealing_range_none_without_both_swings():
    flat = [h1c(i, 100, 100.5, 99.5, 100) for i in range(10)]
    assert dealing_range(flat) is None


def test_pd_state_long_discount_ok():
    rng = (100.0, 110.0)
    assert pd_state(Direction.LONG, 104.0, rng) == "ok"     # below mid 105
    assert pd_state(Direction.LONG, 106.0, rng) == "bad"    # premium long
    assert pd_state(Direction.SHORT, 106.0, rng) == "ok"    # premium short
    assert pd_state(Direction.SHORT, 104.0, rng) == "bad"
    assert pd_state(Direction.LONG, 104.0, None) is None


def test_sweep_label_pdl_for_long():
    """M5 series whose touch..choch excursion wicks below the previous
    Prague-day low -> PDL sweep for a LONG."""
    base = datetime(2026, 3, 3, 8, 0, tzinfo=UTC)  # 09:00 Prague (CET+1)
    prev = [c(i, 101, 101.5, 100.0, 101,
              base=datetime(2026, 3, 2, 10, 0, tzinfo=UTC)) for i in range(6)]
    # today: descend, wick to 99.8 (below yesterday's 100.0 low), recover
    today = [
        c(0, 101, 101.2, 100.6, 100.8, base=base),
        c(1, 100.8, 100.9, 100.2, 100.4, base=base),
        c(2, 100.4, 100.5, 99.8, 100.3, base=base),   # the sweep wick
        c(3, 100.3, 101.0, 100.2, 100.9, base=base),
        c(4, 100.9, 101.4, 100.8, 101.3, base=base),  # CHoCH-ish recovery
    ]
    m5 = prev + today
    label = sweep_label(
        m5=m5, h1=[], direction=Direction.LONG,
        touch_idx=len(prev) + 1, choch_idx=len(prev) + 4,
        tolerance=0.05, ts_utc=base + timedelta(minutes=20),
    )
    assert label == "PDL"


def test_sweep_label_none_when_nothing_swept():
    base = datetime(2026, 3, 3, 8, 0, tzinfo=UTC)
    prev = [c(i, 101, 101.5, 99.0, 101,
              base=datetime(2026, 3, 2, 10, 0, tzinfo=UTC)) for i in range(6)]
    today = [c(i, 100.5, 100.9, 100.2, 100.6, base=base) for i in range(5)]
    m5 = prev + today
    label = sweep_label(
        m5=m5, h1=[], direction=Direction.LONG,
        touch_idx=len(prev) + 1, choch_idx=len(prev) + 4,
        tolerance=0.05, ts_utc=base + timedelta(minutes=20),
    )
    assert label is None


# --- Gaps mandated by the review ledger -------------------------------------


def test_sweep_label_pdh_for_short():
    """SHORT mirror of the PDL case: excursion wicks above yesterday's high."""
    base = datetime(2026, 3, 3, 8, 0, tzinfo=UTC)
    prev = [c(i, 101, 102.0, 100.5, 101,
              base=datetime(2026, 3, 2, 10, 0, tzinfo=UTC)) for i in range(6)]
    today = [
        c(0, 101, 101.4, 100.8, 101.2, base=base),
        c(1, 101.2, 101.6, 101.0, 101.5, base=base),
        c(2, 101.5, 102.2, 101.4, 101.7, base=base),  # the sweep wick (> 102.0)
        c(3, 101.7, 101.9, 101.0, 101.1, base=base),
        c(4, 101.1, 101.3, 100.5, 100.7, base=base),  # CHoCH-ish reversal down
    ]
    m5 = prev + today
    label = sweep_label(
        m5=m5, h1=[], direction=Direction.SHORT,
        touch_idx=len(prev) + 1, choch_idx=len(prev) + 4,
        tolerance=0.05, ts_utc=base + timedelta(minutes=20),
    )
    assert label == "PDH"


def test_sweep_label_eq_pool_long():
    """Two equal unswept M5 lows below the touch form an EQL(2) pool that the
    excursion sweeps once no PDL/AsiaL level qualifies first."""
    base = datetime(2026, 3, 3, 9, 0, tzinfo=UTC)  # 10:00 Prague, past Asia
    # Two near-equal lows at 100.0 / 100.01 (within the 0.02 tolerance),
    # both unswept as-of touch_idx.
    pre = [
        candle(101.0, 101.5, 100.6, 101.2, index=0, start=base),
        candle(101.2, 101.6, 100.9, 101.4, index=1, start=base),
        candle(101.4, 101.6, 100.0, 100.2, index=2, start=base),  # low pivot 100.0
        candle(100.2, 101.0, 100.1, 100.9, index=3, start=base),
        candle(100.9, 101.5, 100.8, 101.3, index=4, start=base),
        candle(101.3, 101.8, 100.01, 100.3, index=5, start=base),  # low pivot 100.01
        candle(100.3, 101.2, 100.2, 101.0, index=6, start=base),
        candle(101.0, 101.6, 100.9, 101.4, index=7, start=base),
        candle(101.4, 101.8, 101.2, 101.6, index=8, start=base),
    ]
    touch_idx = len(pre)
    excursion = [
        candle(101.6, 101.7, 101.0, 101.1, index=touch_idx, start=base),
        candle(101.1, 101.2, 99.9, 100.5, index=touch_idx + 1, start=base),  # sweeps both
        candle(100.5, 101.4, 100.4, 101.3, index=touch_idx + 2, start=base),
    ]
    m5 = pre + excursion
    choch_idx = touch_idx + 2
    label = sweep_label(
        m5=m5, h1=[], direction=Direction.LONG,
        touch_idx=touch_idx, choch_idx=choch_idx,
        tolerance=0.02, ts_utc=base + timedelta(minutes=5 * choch_idx),
    )
    assert label == "EQL(2)"


def test_sweep_label_h1_sourced_pool():
    """No M5 pool below the touch, but an unswept H1 low sits inside the
    excursion window -> the H1 level supplies the swingL label."""
    base = datetime(2026, 3, 3, 9, 0, tzinfo=UTC)
    # M5: flat, no fractal pivots at all before the touch.
    pre = [candle(101.0, 101.2, 100.8, 101.0, index=i, start=base)
           for i in range(9)]
    touch_idx = len(pre)
    excursion = [
        candle(101.0, 101.1, 100.9, 101.0, index=touch_idx, start=base),
        candle(101.0, 101.1, 99.7, 100.8, index=touch_idx + 1, start=base),
        candle(100.8, 101.3, 100.7, 101.2, index=touch_idx + 2, start=base),
    ]
    m5 = pre + excursion
    choch_idx = touch_idx + 2
    # H1 PIT series with an unswept low pivot at 99.8 (inside the wick to 99.7).
    h1 = [
        h1c(0, 101.0, 101.5, 100.8, 101.2), h1c(1, 101.2, 101.6, 101.0, 101.4),
        h1c(2, 101.4, 101.5, 99.8, 100.2),   # low pivot 99.8
        h1c(3, 100.2, 100.6, 100.0, 100.5), h1c(4, 100.5, 100.9, 100.3, 100.7),
    ]
    label = sweep_label(
        m5=m5, h1=h1, direction=Direction.LONG,
        touch_idx=touch_idx, choch_idx=choch_idx,
        tolerance=0.02, ts_utc=base + timedelta(minutes=5 * choch_idx),
    )
    assert label == "swingL"


def test_sweep_label_extreme_equals_level_is_not_a_sweep():
    """The excursion's extreme lands EXACTLY on yesterday's low (no strict
    crossing) -> no PDL sweep, and nothing else qualifies either."""
    base = datetime(2026, 3, 3, 8, 0, tzinfo=UTC)
    prev = [c(i, 101, 101.5, 100.0, 101,
              base=datetime(2026, 3, 2, 10, 0, tzinfo=UTC)) for i in range(6)]
    today = [
        c(0, 101, 101.2, 100.6, 100.8, base=base),
        c(1, 100.8, 100.9, 100.2, 100.4, base=base),
        c(2, 100.4, 100.5, 100.0, 100.3, base=base),  # extreme == 100.0 exactly
        c(3, 100.3, 101.0, 100.2, 100.9, base=base),
        c(4, 100.9, 101.4, 100.8, 101.3, base=base),
    ]
    m5 = prev + today
    label = sweep_label(
        m5=m5, h1=[], direction=Direction.LONG,
        touch_idx=len(prev) + 1, choch_idx=len(prev) + 4,
        tolerance=0.05, ts_utc=base + timedelta(minutes=20),
    )
    assert label is None


def test_dealing_range_low_gte_high_returns_none():
    """A degenerate pivot pair (low >= high) is not a usable dealing range.

    A steady decline: the last confirmed swing LOW forms early at 98.0
    (idx4), then price keeps falling and the last confirmed swing HIGH
    (a small bounce) forms later at 97.0 (idx10) — numerically below the
    earlier low. Both are genuine fractal pivots; the pairing is just
    degenerate, and dealing_range must refuse it rather than return an
    inverted range.
    """
    inverted = [
        h1c(0, 104, 105, 103, 104), h1c(1, 104, 104.5, 102, 103),
        h1c(2, 103, 103.5, 101, 102), h1c(3, 102, 102.5, 100.5, 101),
        h1c(4, 101, 101.5, 98.0, 99),      # swing low 98.0
        h1c(5, 99, 100, 98.5, 99.5), h1c(6, 99.5, 100, 99, 99.7),
        h1c(7, 99.7, 99.8, 95, 95.5), h1c(8, 95.5, 96, 93, 93.5),
        h1c(9, 93.5, 94, 91, 91.5),
        h1c(10, 91.5, 97.0, 91, 96.5),     # swing high 97.0 (< the swing low)
        h1c(11, 96.5, 96.8, 90, 91), h1c(12, 91, 92, 88, 89),
        h1c(13, 89, 90, 86, 87), h1c(14, 87, 88, 84, 85),
    ]
    assert dealing_range(inverted) is None


def test_pd_state_exact_mid_is_ok_for_both_directions():
    rng = (100.0, 110.0)
    assert pd_state(Direction.LONG, 105.0, rng) == "ok"
    assert pd_state(Direction.SHORT, 105.0, rng) == "ok"


def test_room_r_no_pool_returns_none():
    # No pivots at all on either series -> find_liquidity yields nothing.
    h1 = [h1c(i, 100, 100.2, 99.8, 100) for i in range(10)]
    h4 = [c(i, 100, 100.2, 99.8, 100, tf_min=240) for i in range(10)]
    assert room_r(
        h1, h4, Direction.LONG, entry=100.0, risk=1.0, tolerance=0.05
    ) is None


def test_room_r_numeric():
    """One unswept H1 high 10 above the entry, risk 2.0 -> room 5.0R."""
    h1 = [
        h1c(0, 100, 100.5, 99.5, 100), h1c(1, 100, 100.5, 99.7, 100.2),
        h1c(2, 100.2, 110.0, 100.0, 109.0),  # high pivot 110.0
        h1c(3, 109.0, 109.5, 105.0, 106.0), h1c(4, 106.0, 106.5, 104.0, 105.0),
    ]
    h4 = [c(i, 100, 100.2, 99.8, 100, tf_min=240) for i in range(5)]
    room = room_r(
        h1, h4, Direction.LONG, entry=100.0, risk=2.0, tolerance=0.05
    )
    assert room == 5.0


# --- classify truth table ---------------------------------------------------


def test_classify_all_pass_is_star():
    v = classify(room=3.0, sweep="PDL", pd="ok", stale=False)
    assert v == TierVerdict(star=True, missed=[])


def test_classify_room_none_passes():
    v = classify(room=None, sweep="PDL", pd="ok", stale=False)
    assert v.star is True


def test_classify_pd_none_passes():
    v = classify(room=3.0, sweep="PDL", pd=None, stale=False)
    assert v.star is True


def test_classify_missing_room_alone():
    v = classify(room=1.0, sweep="PDL", pd="ok", stale=False)
    assert v.star is False
    assert v.missed == ["room"]


def test_classify_missing_sweep_alone():
    v = classify(room=3.0, sweep=None, pd="ok", stale=False)
    assert v.star is False
    assert v.missed == ["sweep"]


def test_classify_missing_pd_alone():
    v = classify(room=3.0, sweep="PDL", pd="bad", stale=False)
    assert v.star is False
    assert v.missed == ["pd"]


def test_classify_stale_alone():
    v = classify(room=3.0, sweep="PDL", pd="ok", stale=True)
    assert v.star is False
    assert v.missed == ["stale"]


def test_classify_all_miss_reports_in_order():
    v = classify(room=1.0, sweep=None, pd="bad", stale=True)
    assert v.star is False
    assert v.missed == ["room", "sweep", "pd", "stale"]


def test_classify_room_boundary_is_inclusive():
    at_min = classify(room=MIN_ROOM_R, sweep="PDL", pd="ok", stale=False)
    below_min = classify(room=MIN_ROOM_R - 0.01, sweep="PDL", pd="ok", stale=False)
    assert at_min.star is True
    assert below_min.star is False
