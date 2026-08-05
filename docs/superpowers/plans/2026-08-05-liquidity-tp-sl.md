# Liquidity-based TP and sweep-based SL — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed 1:2.5 take-profit with the nearest unswept liquidity level, anchor the stop to the sweep extreme, and let any setup with a real RR ≥ 1:1 through.

**Architecture:** A new pure module `app/services/smc/liquidity.py` turns the existing fractal pivots into unswept liquidity levels (single swings and EQH/EQL clusters). `TripleSyncEngine` consumes it at Rule 7 for the target and a new `structure.sweep_extreme` at Rule 6 for the stop. `plan.py` uses the same target search so `/plan` and live alerts agree. Everything stays pure and unit-testable on synthetic candles — no network in tests.

**Tech Stack:** Python 3.13, dataclasses, pytest (`asyncio_mode=auto`), flake8. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-05-liquidity-tp-sl-design.md`. Read it before Task 1.
- Branch: `feat/liquidity-tp-sl` (already created, spec already committed).
- All bot-facing text is **English**. Message timestamps are Prague time.
- Every dynamic string embedded in a Telegram message MUST pass through `notifier.escape_html`.
- Per-instrument parameters live in `instruments.py` only — never hardcode a pip, buffer or FVG size elsewhere.
- Liquidity tolerance is the **raw** `Instrument.min_fvg`, never the profile-scaled `_effective_min_fvg`.
- Quiet mode: Telegram receives found setups only. Low-RR skips go to logs, never to chat.
- Tests must be network-free and build candles via `tests/test_smc/helpers.py`.
- Run `pytest tests/ -v` and `flake8 app/ tests/ smc_watcher.py` before every commit.
- Line length and style follow `.flake8`; match surrounding comment density.

---

### Task 1: Liquidity module

**Files:**
- Create: `app/services/smc/liquidity.py`
- Test: `tests/test_smc/test_liquidity.py`

**Interfaces:**
- Consumes: `structure.find_pivots`, `models.Candle`, `models.Direction`.
- Produces:
  - `LiquidityLevel(price: float, is_high: bool, timeframe: str, equal_count: int, timestamp: datetime)` — frozen dataclass.
  - `find_liquidity(candles: List[Candle], timeframe: str, tolerance: float) -> List[LiquidityLevel]`
  - `nearest_liquidity(levels: List[LiquidityLevel], direction: Direction, entry: float) -> Optional[LiquidityLevel]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_liquidity.py`:

```python
"""Liquidity levels: unswept swings and EQH/EQL clusters (Rule 7 targets)."""

from app.services.smc.liquidity import (
    LiquidityLevel,
    find_liquidity,
    nearest_liquidity,
)
from app.services.smc.models import Direction
from tests.test_smc.helpers import candle

# One high pivot at index 2 (3160) and one low pivot at index 5 (3140),
# neither taken out by any later candle.
_BASE = [
    (3140.0, 3142.0, 3138.0, 3141.0),
    (3141.0, 3148.0, 3140.0, 3147.0),
    (3147.0, 3160.0, 3146.0, 3158.0),   # high pivot 3160
    (3158.0, 3159.0, 3150.0, 3151.0),
    (3151.0, 3152.0, 3144.0, 3145.0),
    (3145.0, 3146.0, 3140.0, 3141.0),   # low pivot 3140
    (3141.0, 3147.0, 3140.5, 3146.0),
    (3146.0, 3150.0, 3145.0, 3149.0),
    (3149.0, 3151.0, 3148.0, 3150.0),
]


def _series(spec):
    return [candle(*row, index=i) for i, row in enumerate(spec)]


class TestFindLiquidity:
    def test_unswept_swings_become_levels(self):
        levels = find_liquidity(_series(_BASE), "H1", tolerance=2.0)
        highs = [lv for lv in levels if lv.is_high]
        lows = [lv for lv in levels if not lv.is_high]
        assert [lv.price for lv in highs] == [3160.0]
        assert [lv.price for lv in lows] == [3140.0]
        assert highs[0].timeframe == "H1"
        assert highs[0].equal_count == 1

    def test_wick_beyond_tolerance_sweeps_the_level(self):
        # A later candle wicks to 3163 (> 3160 + 2.0) but closes far below:
        # the pool is taken even though no body closed above it.
        swept = _series(_BASE + [(3150.0, 3163.0, 3149.0, 3150.0)])
        highs = [lv for lv in find_liquidity(swept, "H1", 2.0) if lv.is_high]
        assert highs == []

    def test_poke_inside_tolerance_does_not_sweep(self):
        # 3161.5 is only 1.5 above 3160 — less than the 2.0 tolerance, so the
        # pool survives. Without this, two "equal" highs could never coexist.
        poked = _series(_BASE + [(3150.0, 3161.5, 3149.0, 3150.0)])
        highs = [lv for lv in find_liquidity(poked, "H1", 2.0) if lv.is_high]
        assert [lv.price for lv in highs] == [3160.0]


class TestEqualHighsClustering:
    # A second attempt at the same area stalls 0.5 short of the first high:
    # two unswept highs 0.5 apart = one EQH pool.
    _EQH = _BASE + [
        (3150.0, 3159.5, 3149.0, 3158.0),   # high pivot 3159.5
        (3158.0, 3157.0, 3150.0, 3151.0),
        (3151.0, 3152.0, 3146.0, 3147.0),
        (3147.0, 3148.0, 3145.0, 3146.0),
        (3146.0, 3147.0, 3144.0, 3145.0),
    ]

    def test_two_close_highs_form_one_cluster(self):
        highs = [lv for lv in find_liquidity(_series(self._EQH), "H1", 2.0)
                 if lv.is_high]
        assert len(highs) == 1
        assert highs[0].equal_count == 2

    def test_cluster_price_is_the_near_side(self):
        # Price approaches highs from below, so the pool starts at the LOWER
        # of the two equal highs.
        highs = [lv for lv in find_liquidity(_series(self._EQH), "H1", 2.0)
                 if lv.is_high]
        assert highs[0].price == 3159.5

    def test_highs_beyond_tolerance_stay_separate(self):
        levels = find_liquidity(_series(self._EQH), "H1", tolerance=0.2)
        highs = sorted(
            (lv for lv in levels if lv.is_high), key=lambda lv: lv.price
        )
        assert [lv.price for lv in highs] == [3159.5, 3160.0]
        assert all(lv.equal_count == 1 for lv in highs)


class TestNearestLiquidity:
    def _level(self, price, is_high=True, tf="H1", count=1):
        return LiquidityLevel(
            price=price, is_high=is_high, timeframe=tf,
            equal_count=count, timestamp=None,
        )

    def test_long_ignores_levels_at_or_below_entry(self):
        levels = [self._level(3100.0), self._level(3210.0), self._level(3180.0)]
        best = nearest_liquidity(levels, Direction.LONG, entry=3150.0)
        assert best.price == 3180.0

    def test_long_ignores_lows(self):
        levels = [self._level(3180.0, is_high=False), self._level(3210.0)]
        best = nearest_liquidity(levels, Direction.LONG, entry=3150.0)
        assert best.price == 3210.0

    def test_short_takes_the_highest_low_below_entry(self):
        levels = [
            self._level(3100.0, is_high=False),
            self._level(3140.0, is_high=False),
        ]
        best = nearest_liquidity(levels, Direction.SHORT, entry=3150.0)
        assert best.price == 3140.0

    def test_equal_distance_prefers_the_cluster(self):
        levels = [
            self._level(3180.0, tf="M5", count=1),
            self._level(3180.0, tf="H1", count=3),
        ]
        best = nearest_liquidity(levels, Direction.LONG, entry=3150.0)
        assert best.equal_count == 3
        assert best.timeframe == "H1"

    def test_no_level_beyond_entry_returns_none(self):
        levels = [self._level(3100.0)]
        assert nearest_liquidity(levels, Direction.LONG, entry=3150.0) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_smc/test_liquidity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.smc.liquidity'`

- [ ] **Step 3: Write the implementation**

Create `app/services/smc/liquidity.py`:

```python
"""Liquidity levels: unswept swing highs/lows and EQH/EQL pools (Rule 7).

A zone (structure.py) is an order block — an area price reacted from. A
liquidity level is the resting stop-loss pool behind a swing extreme. They
are different objects: the take-profit targets liquidity, the H1 entry
targets a zone.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from app.services.smc.models import Candle, Direction, Pivot
from app.services.smc.structure import find_pivots

# Higher timeframes break ties last; a pool on H4 outranks the same price on M5.
_TF_RANK = {"M5": 0, "H1": 1, "H4": 2}


@dataclass(frozen=True)
class LiquidityLevel:
    """An unswept swing extreme, or a pool of equal ones."""

    price: float
    is_high: bool
    timeframe: str  # "M5" | "H1" | "H4"
    equal_count: int  # 1 = single swing, 2+ = EQH/EQL pool
    timestamp: Optional[datetime] = None


def _is_swept(candles: List[Candle], pivot: Pivot, tolerance: float) -> bool:
    """True once a later candle traded beyond the level by more than
    `tolerance`.

    Wick-based on purpose: liquidity is taken by the wick that runs the stops,
    not by a body close (that is zone invalidation, a different rule). The
    tolerance is what makes equal highs possible — a poke smaller than the
    instrument's minimum FVG has not taken the pool.
    """
    later = candles[pivot.index + 1:]
    if pivot.is_high:
        return any(c.high > pivot.price + tolerance for c in later)
    return any(c.low < pivot.price - tolerance for c in later)


def _cluster(
    pivots: List[Pivot], is_high: bool, timeframe: str, tolerance: float
) -> List[LiquidityLevel]:
    """Group same-side pivots within `tolerance` into single pools."""
    group = sorted(
        (p for p in pivots if p.is_high == is_high), key=lambda p: p.price
    )
    out: List[LiquidityLevel] = []
    i = 0
    while i < len(group):
        j = i
        while j + 1 < len(group) and group[j + 1].price - group[i].price <= tolerance:
            j += 1
        members = group[i:j + 1]
        # Price meets the pool from one side: the lowest high, the highest low.
        anchor = members[0] if is_high else members[-1]
        newest = max(members, key=lambda p: p.index)
        out.append(
            LiquidityLevel(
                price=anchor.price,
                is_high=is_high,
                timeframe=timeframe,
                equal_count=len(members),
                timestamp=newest.timestamp,
            )
        )
        i = j + 1
    return out


def find_liquidity(
    candles: List[Candle], timeframe: str, tolerance: float
) -> List[LiquidityLevel]:
    """All unswept liquidity on this timeframe, singles and pools alike."""
    unswept = [p for p in find_pivots(candles) if not _is_swept(candles, p, tolerance)]
    return (
        _cluster(unswept, True, timeframe, tolerance)
        + _cluster(unswept, False, timeframe, tolerance)
    )


def nearest_liquidity(
    levels: List[LiquidityLevel], direction: Direction, entry: float
) -> Optional[LiquidityLevel]:
    """Closest unswept pool beyond `entry` in the trade direction.

    Equal distance is broken by pool size, then by timeframe — a pool of
    three equal highs is a better objective than one lone swing.
    """
    if direction == Direction.LONG:
        candidates = [lv for lv in levels if lv.is_high and lv.price > entry]
        distance = lambda lv: lv.price - entry  # noqa: E731
    else:
        candidates = [lv for lv in levels if not lv.is_high and lv.price < entry]
        distance = lambda lv: entry - lv.price  # noqa: E731
    if not candidates:
        return None
    candidates.sort(
        key=lambda lv: (
            distance(lv), -lv.equal_count, -_TF_RANK.get(lv.timeframe, 0)
        )
    )
    return candidates[0]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_smc/test_liquidity.py -v`
Expected: PASS — 11 tests.

Then run the whole suite and lint to be sure nothing else moved:
Run: `pytest tests/ -q && flake8 app/ tests/ smc_watcher.py`
Expected: 192 passed + 11 new = 203 passed, flake8 silent.

- [ ] **Step 5: Commit**

```bash
git add app/services/smc/liquidity.py tests/test_smc/test_liquidity.py
git commit -m "Add liquidity levels: unswept swings and EQH/EQL pools"
```

---

### Task 2: Rule 6 — stop behind the sweep extreme

**Files:**
- Modify: `app/services/smc/structure.py` (add `sweep_extreme`, delete `last_protective_pivot`)
- Modify: `app/services/smc/engine.py:238-249`
- Modify: `tests/test_smc/helpers.py` (add `m5_long_trigger_deep_sweep`)
- Test: `tests/test_smc/test_engine.py`

**Interfaces:**
- Consumes: `zone_touch_span` (already used by the engine to produce `touch`), `find_choch` (produces `choch`).
- Produces: `sweep_extreme(candles: List[Candle], direction: Direction, touch_index: int, choch_index: int) -> float`.

- [ ] **Step 1: Add the test fixture**

Append to `tests/test_smc/helpers.py`:

```python
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
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_smc/test_engine.py` (imports: add `m5_long_trigger_deep_sweep` to the existing `tests.test_smc.helpers` import):

```python
class TestSweepStop:
    """Rule 6 (2026-08-05): SL sits behind the swept extreme, not behind the
    last fractal pivot."""

    def test_stop_is_below_the_sweep_not_the_later_pivot(self):
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        result = _engine().evaluate(
            h4=h4, h1=h1, m5=m5_long_trigger_deep_sweep(), result=_fresh_result()
        )
        assert result.setup is not None, result.reasons
        # sweep low 3126.0 minus the $2 buffer, NOT 3132.5 - 2 = 3130.5
        assert result.setup.stop_loss == 3124.0
```

`_engine()` and `_fresh_result()` are the existing module-level helpers at
`tests/test_smc/test_engine.py:18-29`. Use them; do not add new ones.

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_smc/test_engine.py::TestSweepStop -v`
Expected: FAIL — `assert 3130.5 == 3124.0` (the old pivot-based stop).

- [ ] **Step 4: Add `sweep_extreme` to structure.py**

Replace `last_protective_pivot` (lines 217-224) with:

```python
def sweep_extreme(
    candles: List[Candle], direction: Direction, touch_index: int, choch_index: int
) -> float:
    """Rule 6: the extreme of the excursion that swept liquidity before the
    CHoCH — the low of a long's pullback, the high of a short's.

    Anchoring the stop here instead of at the last fractal pivot matters when
    a shallower pivot forms after the sweep: the pivot stop would sit inside
    the wick that took the stops, and get taken with them.
    """
    window = candles[touch_index:choch_index + 1] or [candles[choch_index]]
    if direction == Direction.LONG:
        return min(c.low for c in window)
    return max(c.high for c in window)
```

- [ ] **Step 5: Wire it into the engine**

In `app/services/smc/engine.py`, replace the Rule 6 block (lines 238-249):

```python
        # Rule 6 — stop behind the swept extreme of the zone excursion
        extreme = sweep_extreme(m5, direction, touch, choch)
        if direction == Direction.LONG:
            stop_loss = extreme - self.sl_buffer
        else:
            stop_loss = extreme + self.sl_buffer
```

Update the import block at the top of `engine.py`: drop `last_protective_pivot`, add `sweep_extreme` (keep the list alphabetised as it already is).

Note the "No confirmed M5 pivot for the stop" SKIP branch disappears — an excursion always has an extreme. The `risk <= 0` guard immediately below stays.

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_smc/test_engine.py -v`
Expected: `TestSweepStop` PASSES. Other tests in the file may now fail on stop/RR numbers — that is expected, they assert the old pivot stop. Fix each by recomputing the expected value from the fixture, not by loosening the assertion.

Run: `pytest tests/ -q && flake8 app/ tests/ smc_watcher.py`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add app/services/smc/structure.py app/services/smc/engine.py tests/test_smc/helpers.py tests/test_smc/test_engine.py
git commit -m "Rule 6: anchor the stop to the sweep extreme, not the last pivot"
```

---

### Task 3: Rule 7 — take-profit at the nearest unswept liquidity

This task carries the `tp_rr` → `min_rr` rename through every call site in one
commit. Splitting it would leave the suite red between commits, which the
Global Constraints forbid — the rename ripples through config, engine, plan,
watcher, funnel and six test modules, and no reviewer could approve half of it.

**Files:**
- Modify: `app/core/config.py:64-68` (`tp_rr` → `min_rr`)
- Modify: `app/services/smc/models.py` (`TradeSetup.target`)
- Modify: `app/services/smc/engine.py` (constructor + Rule 7 block)
- Modify: `app/services/smc/plan.py:46-133` (`_scenario`, `build_plan`)
- Modify: `smc_watcher.py:103`, `smc_watcher.py:502`
- Modify: `scripts/funnel.py:44-63` (`classify_result`), `:81`, `:100`
- Test: `tests/test_smc/test_engine.py`, `test_improvements.py`, `test_plan.py`, `test_funnel.py`, `test_multipair.py`, `test_visuals.py`, `test_zone_ping.py`

**Interfaces:**
- Consumes: `liquidity.find_liquidity`, `liquidity.nearest_liquidity`, `LiquidityLevel` (Task 1).
- Produces:
  - `TripleSyncEngine(..., min_rr: float = 1.0, ...)` — `tp_rr` is gone.
  - `TradeSetup.target: Optional[LiquidityLevel] = None` — Task 4 renders it.
  - `SMCSettings.min_rr: float = 1.0` (env `SMC_MIN_RR`).
  - `build_plan(instrument, h4, h1, m5, min_rr: float = 1.0, profile=None, market_closed=False) -> PairPlan`
  - `_scenario(instrument, h1, h4, direction, price, speculative, min_rr, profile) -> Tuple[Optional[PlanScenario], Optional[str]]`

- [ ] **Step 1: Write the failing engine tests**

Add to `tests/test_smc/test_engine.py`:

```python
class TestLiquidityTarget:
    """Rule 7 (2026-08-05): TP is the nearest unswept liquidity, one buffer
    short of the level; RR is computed, not assigned."""

    def _run(self, **kwargs):
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        return _engine(**kwargs).evaluate(
            h4=h4, h1=h1, m5=m5_long_trigger_deep_sweep(), result=_fresh_result()
        )

    def test_tp_is_the_nearest_unswept_liquidity_minus_buffer(self):
        result = self._run()
        assert result.setup is not None, result.reasons
        # H1 swing high 3221.0 is the nearest unswept pool above entry 3140.5
        assert result.setup.take_profit == 3219.0
        assert result.setup.target.price == 3221.0
        assert result.setup.target.timeframe == "H1"

    def test_rr_is_computed_from_the_target(self):
        result = self._run()
        # entry 3140.5, SL 3124.0 -> risk 16.5; TP 3219.0 -> 78.5 / 16.5
        assert result.setup.rr == 4.76

    def test_rr_does_not_track_the_threshold(self):
        # The old rule assigned rr = tp_rr, so the RR moved with the setting.
        assert self._run(min_rr=1.0).setup.rr == self._run(min_rr=2.0).setup.rr

    def test_setup_below_the_threshold_is_skipped(self):
        result = self._run(min_rr=8.0)
        assert result.verdict == Verdict.SKIP
        assert result.setup is None
        assert "1:4.8" in result.reasons[0]
        assert "1:8" in result.reasons[0]
```

Also update the two module-level engine builders at `tests/test_smc/test_engine.py:26-34` — both carry `tp_rr=2.5` in their `defaults` dict:

```python
def _engine(**kwargs) -> TripleSyncEngine:
    defaults = dict(min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0)
    defaults.update(kwargs)
    return TripleSyncEngine(**defaults)


def _agg_engine(**kwargs) -> TripleSyncEngine:
    defaults = dict(min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, profile=AGGRESSIVE)
    defaults.update(kwargs)
    return TripleSyncEngine(**defaults)
```

Delete `test_tp_scales_with_configured_tp_rr` and `test_skip_when_no_zone_beyond_fixed_tp` (lines ~102-120) — they assert the removed rule. Rewrite the `tp_rr=2.5` case in `tests/test_smc/test_improvements.py:49-52` to construct the engine with no RR argument and assert `result.setup.target is not None`.

- [ ] **Step 2: Write the failing plan tests**

In `tests/test_smc/test_plan.py`, replace every `tp_rr=2.5` argument with `min_rr=1.0`, and add:

```python
    def test_scenario_targets_liquidity(self):
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3150, 3152, 3151, 3153])
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        assert plan.scenarios
        s = plan.scenarios[0]
        # TP is a structural level, not a multiple of the risk
        assert s.take_profit != round(s.entry + 2.5 * abs(s.entry - s.stop_loss), 2)
        assert s.rr >= 1.0

    def test_scenario_below_threshold_is_explained(self):
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        m5 = make_candles([3150, 3152, 3151, 3153])
        plan = build_plan(ETH, h4, h1, m5, min_rr=99.0)
        assert plan.scenarios == []
        assert "1:" in plan.note
```

Add to `tests/test_smc/test_funnel.py`:

```python
def test_classify_result_labels_low_rr():
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=SESSION_BASE,
    )
    result.reasons.append(
        "RR 1:0.6 < minimum 1:1 to the nearest liquidity (H1 swing high 3221.00)"
    )
    assert classify_result(result) == "rr_low"
```

Import `classify_result`, `AnalysisResult`, `Verdict` and `SESSION_BASE` as that file already does for its other cases.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_smc/test_engine.py::TestLiquidityTarget tests/test_smc/test_plan.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'min_rr'`

- [ ] **Step 4: Add the config setting**

In `app/core/config.py`, replace the `tp_rr` field (lines 64-68) with:

```python
    min_rr: float = Field(
        default=1.0,
        description="Minimum risk/reward to the nearest unswept liquidity; "
        "setups below it are skipped (owner decision 2026-08-05)",
    )
```

- [ ] **Step 5: Add the target field to the model**

In `app/services/smc/models.py`, add to `TradeSetup` (after `lot_hint`):

```python
    target: Optional["LiquidityLevel"] = None
```

and at the top of the file:

```python
from app.services.smc.liquidity import LiquidityLevel
```

If that import creates a cycle (`liquidity` imports `structure`, which imports `models`), do NOT restructure the modules — keep the annotation as the string `Optional["LiquidityLevel"]` with `if TYPE_CHECKING:` guarding the import. Verify with `python -c "import app.services.smc.engine"`.

- [ ] **Step 6: Rewrite Rule 7 in the engine**

Constructor: rename the `tp_rr: float = 2.5` parameter to `min_rr: float = 1.0` and `self.tp_rr = tp_rr` to `self.min_rr = min_rr`.

Replace the whole Rule 7 block (`engine.py`, from the `# Rule 7` comment through the `if not has_room:` branch) with:

```python
        # Rule 7 (owner decision 2026-08-05) — take-profit at the nearest
        # unswept liquidity: the pool the move is actually reaching for. The
        # TP sits one buffer short of the level so the trade is out before
        # the sweep itself. Liquidity uses the raw per-instrument minimum FVG
        # as its tolerance — it is a property of the chart, not of the
        # profile the owner is trading.
        tolerance = self.min_fvg_size
        levels = (
            find_liquidity(m5, "M5", tolerance)
            + find_liquidity(h1, "H1", tolerance)
            + find_liquidity(h4, "H4", tolerance)
        )
        target = nearest_liquidity(levels, direction, entry)
        if target is None:
            result.verdict = Verdict.SKIP
            result.reasons.append(
                "No unswept liquidity beyond the entry — nothing to aim at"
            )
            return result

        if direction == Direction.LONG:
            take_profit = target.price - self.sl_buffer
        else:
            take_profit = target.price + self.sl_buffer
        rr = abs(take_profit - entry) / risk
        if rr < self.min_rr:
            result.verdict = Verdict.SKIP
            result.reasons.append(
                f"RR 1:{rr:.1f} < minimum 1:{self.min_rr:g} to the nearest "
                f"liquidity ({target.timeframe} "
                f"{'swing high' if target.is_high else 'swing low'} "
                f"{target.price:.{self.instrument.price_decimals}f})"
            )
            return result
```

Add `target=target` to the `TradeSetup(...)` construction below it.

Update the engine imports: add `from app.services.smc.liquidity import find_liquidity, nearest_liquidity`, and drop `find_target_zone` from the `structure` import (it keeps its other caller in `plan.py` until Step 7 removes that too — check with `grep -rn find_target_zone app/` after Step 7 and drop the now-dead function from `structure.py` if nothing calls it).

- [ ] **Step 7: Convert the pre-market plan to the same targets**

In `app/services/smc/plan.py`, replace `_scenario` (lines 46-85) with:

```python
def _scenario(
    instrument: Instrument,
    h1: List[Candle],
    h4: List[Candle],
    direction: Direction,
    price: float,
    speculative: bool,
    min_rr: float,
    profile: StrategyProfile,
) -> Tuple[Optional[PlanScenario], Optional[str]]:
    """Project a conditional setup aimed at the nearest unswept liquidity.

    Returns `(scenario, reason)` — exactly one is non-None. `reason` explains
    a live zone whose nearest pool does not reach `min_rr`, so the plan can
    say so instead of pretending no structure exists.

    M5 is not scanned here: hours before the session its swings will have
    been swept long before price reaches the zone.
    """
    zone = find_h1_zone(h1, direction, max_touches=profile.max_zone_touches)
    if zone is None:
        return None, None

    if direction == Direction.LONG:
        # a pullback-to-demand plan: the zone must sit at/below current price
        if zone.top >= price:
            return None, None
        entry, stop = zone.top, zone.bottom - instrument.sl_buffer
    else:
        if zone.bottom <= price:
            return None, None
        entry, stop = zone.bottom, zone.top + instrument.sl_buffer

    risk = abs(entry - stop)
    if risk <= 0:
        return None, None

    tolerance = instrument.min_fvg
    levels = (
        find_liquidity(h1, "H1", tolerance) + find_liquidity(h4, "H4", tolerance)
    )
    target = nearest_liquidity(levels, direction, entry)
    if target is None:
        return None, None

    sign = -1 if direction == Direction.LONG else 1
    take_profit = target.price + sign * instrument.sl_buffer
    rr = abs(take_profit - entry) / risk
    d = instrument.price_decimals
    if rr < min_rr:
        return None, (
            f"Zone {'Demand' if direction == Direction.LONG else 'Supply'} "
            f"{zone.bottom:.{d}f}-{zone.top:.{d}f} is live, but the nearest "
            f"liquidity gives 1:{rr:.1f} — waiting for other structure"
        )

    return PlanScenario(
        direction=direction,
        entry=round(entry, d),
        stop_loss=round(stop, d),
        take_profit=round(take_profit, d),
        rr=round(rr, 2),
        zone_bottom=round(zone.bottom, d),
        zone_top=round(zone.top, d),
        speculative=speculative,
    ), None
```

In `build_plan`: rename the `tp_rr: float = 2.5` parameter to `min_rr: float = 1.0`, update the docstring to "only scenarios reaching min_rr to the nearest unswept liquidity are shown", and change the loop:

```python
    reasons = []
    for direction, speculative in directions:
        scenario, reason = _scenario(
            instrument, h1, h4, direction, price, speculative, min_rr, profile,
        )
        if scenario:
            plan.scenarios.append(scenario)
        elif reason:
            reasons.append(reason)

    if not plan.scenarios:
        plan.note = reasons[0] if reasons else (
            "No clean H1 zone for a plan yet — wait for structure to form"
        )
    return plan
```

Imports: add `from app.services.smc.liquidity import find_liquidity, nearest_liquidity`, drop `find_target_zones` from the `structure` import if now unused, and make sure `Tuple` is imported from `typing`.

- [ ] **Step 8: Close the remaining call sites**

`smc_watcher.py:103`: `tp_rr=smc.tp_rr,` → `min_rr=smc.min_rr,`
`smc_watcher.py:502`: `tp_rr=settings.smc.tp_rr,` → `min_rr=settings.smc.min_rr,`

`scripts/funnel.py:81`: `tp_rr: float = 2.5,` → `min_rr: float = 1.0,`
`scripts/funnel.py:100`: `instrument=instrument, tp_rr=tp_rr, ...` → `instrument=instrument, min_rr=min_rr, ...`
`scripts/funnel.py` docstring of `replay_funnel`: replace the `tp_rr` mention with `min_rr`.

`scripts/funnel.py` `classify_result`: replace the `no_room` branch

```python
    if "no room" in reason:
        return "no_room"
```

with

```python
    if "no unswept liquidity" in reason:
        return "no_liquidity"
    if reason.startswith("rr 1:"):
        return "rr_low"
```

and adjust the `no_zone` branch — `"no untested opposite"` no longer appears in any engine reason, so drop that clause and keep `"no valid untested"`.

Then run `grep -rn "tp_rr" tests/ scripts/ smc_watcher.py app/` and convert every remaining hit to `min_rr` with a sensible value (`1.0` unless the test is specifically forcing a skip). Expected remaining files: `test_multipair.py`, `test_visuals.py`, `test_zone_ping.py`.

- [ ] **Step 9: Run the full suite**

Run: `pytest tests/ -q && flake8 app/ tests/ smc_watcher.py`
Expected: all green, zero `tp_rr` left in `app/`, `tests/`, `scripts/`, `smc_watcher.py`.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "Rule 7: take-profit at the nearest unswept liquidity, RR >= SMC_MIN_RR"
```

---

### Task 4: Alert names the objective

**Files:**
- Modify: `app/services/smc/notifier.py` (`format_result`, line ~99)
- Test: `tests/test_smc/test_html_escaping.py` — the only module that exercises `format_result`

**Interfaces:**
- Consumes: `TradeSetup.target` (Task 3).
- Produces: `notifier.format_target(level: LiquidityLevel, decimals: int) -> str`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_smc/test_html_escaping.py` (import `format_target` from `app.services.smc.notifier` and `LiquidityLevel` from `app.services.smc.liquidity`):

```python
class TestTargetLine:
    def test_rr_line_names_the_liquidity(self):
        level = LiquidityLevel(
            price=3221.0, is_high=True, timeframe="H1",
            equal_count=1, timestamp=None,
        )
        assert format_target(level, 2) == "H1 swing high 3221.00"

    def test_pool_size_is_shown(self):
        level = LiquidityLevel(
            price=3221.0, is_high=True, timeframe="H1",
            equal_count=3, timestamp=None,
        )
        assert format_target(level, 2) == "H1 swing high 3221.00 (EQH x3)"

    def test_low_pool_uses_eql(self):
        level = LiquidityLevel(
            price=3050.0, is_high=False, timeframe="H4",
            equal_count=2, timestamp=None,
        )
        assert format_target(level, 2) == "H4 swing low 3050.00 (EQL x2)"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_smc/ -k TargetLine -v`
Expected: FAIL — `ImportError: cannot import name 'format_target'`

- [ ] **Step 3: Implement**

In `app/services/smc/notifier.py`:

```python
def format_target(level, decimals: int) -> str:
    """Name a liquidity objective: 'H1 swing high 3221.00 (EQH x2)'."""
    kind = "swing high" if level.is_high else "swing low"
    pool = ""
    if level.equal_count > 1:
        pool = f" (EQ{'H' if level.is_high else 'L'} x{level.equal_count})"
    return f"{level.timeframe} {kind} {level.price:.{decimals}f}{pool}"
```

Replace the RR line in `format_result` (currently `lines.append(f"📐 RR: 1:{setup.rr:.1f}")`):

```python
        rr_line = f"📐 RR: 1:{setup.rr:.1f}"
        if setup.target:
            rr_line += "  →  " + escape_html(format_target(setup.target, d))
        lines.append(rr_line)
```

Plain ASCII `x` rather than `×` in the pool suffix: the alert already carries emoji, and `x` avoids any doubt about encoding on the Railway console. `escape_html` is applied because the string is dynamic — that rule has no exceptions in this codebase.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_smc/ -k TargetLine -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/smc/notifier.py tests/
git commit -m "Alert: name the liquidity objective on the RR line"
```

---

### Task 5: env, docs and the PR

**Files:**
- Modify: `env.example:31`
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: `SMCSettings.min_rr` (Task 3). No code changes — docs only, so the suite is unaffected.

- [ ] **Step 1: Update env.example**

Replace line 31:

```
SMC_MIN_RR=1.0                    # minimum RR to the nearest unswept liquidity; below it the setup is skipped
```

- [ ] **Step 2: Update CLAUDE.md**

In the Architecture block, add the new module under `app/services/smc/` in file order:

```
├── liquidity.py          unswept swing highs/lows + EQH/EQL pools; Rule 7
│                         targets and the sweep-extreme stop reference
```

In "Conventions and gotchas", add:

```
- Liquidity (`liquidity.py`) and zones (`structure.py`) are different objects:
  a zone is an order block price reacts from, a liquidity level is the stop
  pool behind a swing extreme. Rule 7 aims at liquidity; Rule 2 enters at a
  zone. Sweep detection is wick-based with a tolerance of the raw per-
  instrument `min_fvg` — never the profile-scaled value.
```

- [ ] **Step 3: Update README.md**

Run `grep -n "2.5\|TP_RR\|take-profit" README.md` and replace every description of the fixed 2.5R take-profit with the liquidity rule and `SMC_MIN_RR`.

- [ ] **Step 4: Verify nothing references the removed setting**

Run: `grep -rn "TP_RR\|tp_rr" . --include="*.py" --include="*.md" --include="*.example" | grep -v docs/superpowers`
Expected: no output.

Run: `pytest tests/ -q && flake8 app/ tests/ smc_watcher.py`
Expected: all green (docs changes cannot break it, but confirm before the PR).

- [ ] **Step 5: Commit and open the PR**

```bash
git add -A
git commit -m "Document the liquidity take-profit rule and SMC_MIN_RR"
git push -u origin feat/liquidity-tp-sl
gh pr create --fill
```

Follow `.github/pull_request_template.md`. In the PR body, state explicitly that `SMC_TP_RR` must be deleted from the Railway service variables after the merge — it is ignored by `extra="ignore"` but will otherwise sit there looking meaningful.

---

### Task 6: Replay the new rule and report the numbers

**Files:**
- No production code. Scratch scripts go in the session scratchpad, not the repo.

**Interfaces:**
- Consumes: everything above, on the merged branch.

- [ ] **Step 1: Run the funnel on both profiles**

```bash
python -m scripts.funnel --pair ETHUSD --days 59
```

Record the per-stage counts, especially the new `rr_low` and `no_liquidity` stages, and `distinct_setups` per week. Compare against the pre-change baseline in the spec (~1.3 setups/wk conservative).

- [ ] **Step 2: Run the outcome replay**

Reuse the 2026-07-30 scratchpad replay methodology recorded in the `strategy-replay-findings` memory: point-in-time slicing, journal outcome semantics (entry fill = candle touch; TP and SL in the same candle counts as SL; pending orders expire with their session). Same ETHUSD window, 59 days.

Report: alert count, win rate, total R, average R per closed trade, and the share of limit orders that expire unfilled — for `conservative` and `aggressive`.

- [ ] **Step 3: Report to the owner**

Present the numbers next to the 2026-07-30 baseline (conservative planned-TP 18% WR / −9.3R, fixed 2R 50% WR / +14.0R, fixed 2.5R +4.0R). State plainly whether the new rule beats or loses to them, and do not soften the result if it loses. The owner chose this rule knowing the old structural TP scored −9.3R; he is entitled to a straight answer on whether the liquidity version is different.

- [ ] **Step 4: Update the memory file**

Append the findings to `C:\Users\Dell\.claude\projects\C--Git-926-bot\memory\strategy-replay-findings.md` with the date, so the next session does not re-derive them.

---

## Notes for the implementer

- **Fixture arithmetic is load-bearing.** The expected values in Tasks 2 and 3 (entry 3140.5, SL 3124.0, risk 16.5, TP 3219.0, RR 4.76) were derived by hand from `m5_long_trigger_deep_sweep`, `H1_PULLBACK_CLOSES` and `H4_UPTREND_CLOSES` with `min_fvg_size=2.0` and `sl_buffer=2.0`. If a number comes out different, recompute it from the fixture and find out why — do not adjust the assertion to match whatever the code printed. A test that agrees with a bug is worse than no test.
- **`make_candles` wick convention:** bullish candles get `high = max(o,c) + 1.0`, `low = min(o,c) - 0.4`; bearish the mirror. Every pivot price in the H1/H4 fixtures comes from that rule, not from the close.
- **`find_pivots` eligibility:** index range is `range(2, min(n - 2, n - 2))`, so the last two candles can never be pivots. A fixture must leave confirmation room after the extreme it wants detected.
- **Do not touch** session windows, news blackouts, discipline rules, journal semantics, or the strategy profiles' four decision points. If a change seems to require it, stop and ask.
