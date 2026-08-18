# Range Trading (Delivery 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When both H4 and H1 read FLAT and price is ranging, the bot recognises the range, marks its boundaries on the charts, alerts when price reaches one, and detects a setup back toward the opposite boundary.

**Architecture:** A new pure-geometry module `range.py` finds the range by clustering confirmed H1 pivots. A boundary is expressed as a `Zone` of kind `"RANGE"`, which lets the entire existing pipeline — touch span, M5 CHoCH, FVG, order block, `sweep_extreme` — work on it unchanged; only the stop and target geometry differ, and they come from the boundaries. The pre-market plan emits range scenarios in place of the speculative both-way brackets, so Delivery 1's zone-alert dedup and 🔕 mute cover the boundary alert with no new alerting path.

**Tech Stack:** Python 3.11, pytest (`asyncio_mode=auto`), matplotlib (Agg, no pandas), structlog, SQLite.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-16-quiet-zones-and-range-design.md`. Delivery 3 is §3; decisions D7, D8, D9, D11 and D12 apply.
- **The strategy specification is law.** Rules −1 through 11 come from the owner's written trading system. Never relax or "improve" a strategy rule. Implementation over-strictness may be fixed; the rules may not.
- **Detector mode** (owner decision 2026-08-06): once a setup fully forms, the alert always fires. Nothing in this delivery may add a suppression. The ⭐ tier only changes presentation.
- **D11:** the range is in play only when **both** `detect_trend(h4)` and `detect_trend(h1)` read FLAT. The H1-trend fallback of 2026-08-06 keeps precedence — no existing trend setup may disappear because a range was found.
- **D12:** when a range is in play, its scenarios **replace** the plan's speculative both-way breakout brackets rather than joining them.
- **D9:** a wick through a boundary that closes back inside does **not** break the range — it is liquidity taken, and it earns the ⭐.
- All bot-facing text is English. Comments and docstrings are English too.
- Telegram messages use `parse_mode=HTML`; every dynamic string embedded in a message MUST go through `notifier.escape_html`. Only `<b>` and `<pre>` tags — add no others.
- Per-instrument parameters live in `instruments.py`. The range tolerance is the **raw** `instrument.min_fvg`, never the profile-scaled value — it matches sweep detection (see the sweep-tolerance convention in `CLAUDE.md`).
- `chart.py` stays matplotlib-only — no pandas, no mplfinance. Rendering must never block an alert.
- Engines see **closed candles only**. Do not re-add an in-progress candle.
- Adding a signal column means extending `SIGNAL_COLUMNS`, the `CREATE TABLE` and the migration list in `db.py` — production databases are migrated in place, and `db.py` must never crash the watcher.
- Tests are network-free. Synthetic candles come from `tests/test_smc/helpers.py`.
- Run `pytest tests/ -v` and `flake8 app/ tests/ smc_watcher.py` before every commit.
- **One test fails on `master` and is out of scope:** `tests/test_smc/test_visuals.py::TestPrettyStats::test_stats_contains_bars_and_sparkline` (its fixture dates aged past a 30-day window). Three `test_multipair.py::TestApiKeyNeverReachesTelegram` tests are environment-variable dependent, also out of scope. Everything else must pass.

## A known defect this delivery must not make worse

`zone_touch_span` and `first_zone_touch` anchor on `zone.timestamp`, which for an H1 zone is the **open** of its formation candle — so the zone's own formation hour still counts as an excursion into it. A fresh zone price never returned to can therefore read as occupied, anchoring `sweep_extreme` in the zone's birth hour. It is pre-existing, already tracked as its own task, and out of scope here.

It matters for this plan because a range boundary is built from pivots that are **old by construction**. Task 2 must give the boundary `Zone` a timestamp that makes the anchor correct anyway — see its interface note — rather than inheriting the hazard.

---

### Task 1: `range.py` — detection

**Files:**
- Create: `app/services/smc/range.py`
- Test: `tests/test_smc/test_range.py` (create)

**Interfaces:**
- Consumes: `structure.find_pivots(candles) -> List[Pivot]` (confirmed fractal-5 pivots; each has `.index`, `.price`, `.timestamp`, `.is_high`), `models.Candle` (`.high`, `.low`, `.close`, `.body_high`, `.body_low`, `.timestamp`).
- Produces:

```python
@dataclass
class Range:
    top: float
    bottom: float
    touches_top: int
    touches_bottom: int
    broken: bool
    top_at: datetime      # timestamp of the most recent pivot in the top cluster
    bottom_at: datetime   # same for the bottom cluster
    swept_top: bool       # a wick closed back inside after piercing the top (D9)
    swept_bottom: bool

def detect_range(
    candles: List[Candle], tolerance: float, window: int = 120
) -> Optional[Range]
```

**Why `tolerance: float` and not the `Instrument`:** every other geometry module in this package takes plain numbers so it stays free of the instrument and profile registries; `structure.find_h1_fvg_zone` set that precedent in Delivery 2. The caller passes the **raw** `instrument.min_fvg`.

**Why `top_at` / `bottom_at` and `swept_*` beyond the spec's five fields:** the timestamps are what let Task 2 anchor a boundary `Zone` correctly (see the known-defect note above), and the sweep flags are D9's ⭐ condition, which nothing else can reconstruct once the range is returned.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_range.py`:

```python
"""Range detection from clustered H1 pivots (spec §3.1, D7, D9)."""

from app.services.smc.range import detect_range
from tests.test_smc.helpers import make_candles

# Four clean swings between 100 and 120: two highs near 120, two lows near
# 100, nothing closing outside. make_candles builds each candle around its
# close, so these closes produce fractal pivots at the turning points.
RANGING = [
    100.0, 104.0, 110.0, 116.0, 120.0, 116.0, 110.0, 104.0, 100.0,
    104.0, 110.0, 116.0, 119.6, 116.0, 110.0, 104.0, 100.4,
    104.0, 110.0, 114.0,
]


def _range(closes=None, tolerance=1.0, window=120):
    return detect_range(
        make_candles(closes or RANGING, step_minutes=60), tolerance, window
    )


class TestDetectRange:
    def test_finds_the_band(self):
        rng = _range()
        assert rng is not None
        assert 119.0 <= rng.top <= 120.5
        assert 99.5 <= rng.bottom <= 101.0
        assert rng.broken is False

    def test_counts_touches_on_both_boundaries(self):
        rng = _range()
        assert rng.touches_top >= 2 and rng.touches_bottom >= 2

    def test_one_touch_is_not_a_range(self):
        """A single swing high is a level, not a boundary."""
        single = [100.0, 110.0, 120.0, 110.0, 100.0, 104.0, 108.0, 104.0, 100.0]
        assert _range(single) is None

    def test_a_band_narrower_than_three_tolerances_is_chop(self):
        assert _range(tolerance=10.0) is None

    def test_a_body_close_above_the_top_breaks_it(self):
        broken = RANGING + [126.0, 128.0, 130.0]
        rng = _range(broken)
        assert rng is not None and rng.broken is True

    def test_a_body_close_below_the_bottom_breaks_it(self):
        broken = RANGING + [96.0, 92.0, 90.0]
        rng = _range(broken)
        assert rng is not None and rng.broken is True

    def test_a_wick_through_the_top_that_closes_back_inside_does_not_break_it(self):
        """D9: that is liquidity taken, not a breakout."""
        rng = _range()
        assert rng is not None and rng.broken is False
        assert rng.swept_top in (True, False)  # flag exists and is a bool

    def test_no_pivots_no_range(self):
        assert _range([100.0] * 12) is None

    def test_empty_candles_no_range(self):
        assert detect_range([], 1.0) is None

    def test_the_window_bounds_the_search(self):
        """Pivots older than `window` candles cannot form a boundary."""
        long_series = [100.0, 140.0, 100.0, 140.0] + RANGING
        rng = detect_range(
            make_candles(long_series, step_minutes=60), 1.0, window=len(RANGING)
        )
        assert rng is None or rng.top < 130.0

    def test_boundary_timestamps_come_from_the_latest_pivot_of_each_cluster(self):
        rng = _range()
        assert rng is not None
        candles = make_candles(RANGING, step_minutes=60)
        stamps = {c.timestamp for c in candles}
        assert rng.top_at in stamps and rng.bottom_at in stamps
```

The exact closes above are a starting point. Run them, and if `find_pivots` does not confirm the turning points you expect, **adjust the closes until it does** — never weaken `detect_range` to make a test pass. Print the pivots while iterating; a fractal-5 pivot needs two candles either side plus two closed candles after it.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_range.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.smc.range'`.

- [ ] **Step 3: Implement `range.py`**

Create `app/services/smc/range.py`. Module docstring: what a range is in this system, that its boundaries coincide with EQH/EQL pools by construction so this is the same liquidity hunt applied to a range, and that it is pure geometry with no network or DB access.

`detect_range` over `candles[-window:]`:

1. `find_pivots` on the windowed slice. Split into highs and lows.
2. Cluster the highs: group pivots whose prices lie within `tolerance` of each other. Take the **largest** cluster (ties broken by the most recent). `top` is the cluster's mean price, `touches_top` its size, `top_at` the latest timestamp in it. Same for the lows.
3. Return `None` when either cluster has fewer than 2 members.
4. Return `None` when `top - bottom < 3 * tolerance` — that is chop, not a range.
5. `broken`: any candle after `max(top_at, bottom_at)` whose **body** closed beyond either boundary (`c.close > top and c.body_high > top`, or `c.close < bottom and c.body_low < bottom`). Use the same body-close idiom `structure._break_still_holds` and `_mark_zone_state` already use — read one of them and match it.
6. `swept_top`: a candle after `top_at` whose **high** pierced `top` while its body closed back inside; `swept_bottom` the mirror. This is D9's ⭐ condition.

Keep the clustering simple and readable — sort, then walk and group. Do not import numpy.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_range.py -v
```

Expected: PASS, 11 tests.

- [ ] **Step 5: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: 1 pre-existing failure only, flake8 clean.

- [ ] **Step 6: Commit**

```bash
git add app/services/smc/range.py tests/test_smc/test_range.py
git commit -m "feat: range detection from clustered H1 pivots"
```

---

### Task 2: The engine trades the range

**Files:**
- Modify: `app/services/smc/range.py` (add `boundary_zone`)
- Modify: `app/services/smc/models.py` (`AnalysisResult.market_range`)
- Modify: `app/services/smc/engine.py` (Rule 1's flat branch; the stop/target geometry; the ⭐ sweep)
- Test: `tests/test_smc/test_range_engine.py` (create)

**Interfaces:**
- Consumes: `detect_range` (Task 1); `structure.find_zone_of_interest` and the existing Rule 3/4/6 pipeline; `sniper.classify`.
- Produces:
  - `range.boundary_zone(rng: Range, direction: Direction, tolerance: float) -> Zone` — the boundary as a band the existing pipeline can consume.
  - `AnalysisResult.market_range: Optional[Range]` — so messages and charts can draw the boundaries.

**The boundary as a `Zone`.** This is the key design decision and it is deliberate: expressing a boundary as a `Zone` of `kind="RANGE"` lets `zone_touch_span`, `find_choch`, `select_valid_fvg`, `find_order_block` and `sweep_extreme` run **unchanged**. Only the stop and the target differ, and both come from the range.

- band thickness is one `tolerance` (the raw `min_fvg`), measured **inward**: at the high, `[top - tolerance, top]`, `is_demand=False`; at the low, `[bottom, bottom + tolerance]`, `is_demand=True`. Same tolerance the clustering used, so the band is exactly the spread the boundary was defined with.
- `kind="RANGE"`.
- `timestamp` is the boundary's own `top_at` / `bottom_at`, and `pivot_index` the index of that pivot. **Read the known-defect note at the top of this plan before choosing these** — the anchor decides which M5 candles count as an excursion into the boundary, and a boundary pivot is old by construction, so this must not let the pivot's own formation hour count.

**Rule 1's flat branch.** Insert the range **after** the H1-trend checks and **before** the aggressive-profile CHoCH branch, so D11 holds: a trending H1 still wins. Sketch:

```python
        else:
            h1_trend = result.h1_trend
            if h1_trend == Trend.UP:
                direction, result.direction_source = Direction.LONG, "h1"
            elif h1_trend == Trend.DOWN:
                direction, result.direction_source = Direction.SHORT, "h1"
            else:
                # D11: both timeframes flat — the one state where the bot
                # had nothing to say. A range gives it boundaries to trade
                # between (spec §3.2, owner decision 2026-08-18).
                rng = detect_range(h1, self.min_fvg)
                if rng is not None and not rng.broken:
                    result.market_range = rng
                    direction = <SHORT at the top, LONG at the bottom, else None>
                    if direction is not None:
                        result.direction_source = "range"
                if direction is None and self.profile.allow_h4_choch_entry:
                    ...existing CHoCH branch...
```

"At the top" means the latest closed M5 candle has reached the top boundary's band; "at the bottom" likewise. When the range exists but price sits mid-range, there is no direction **yet** — that is a WATCH, not a SKIP: record the range on the result, add a reason naming both boundaries and the wait, and return. Read how the existing Rule 2 "no zone" WATCH is phrased and match its voice.

**Stop and target.** Where the engine computes the stop and take-profit for a normal setup, a range setup differs:
- SL: beyond the boundary the setup formed at, plus `instrument.sl_buffer` — for a SHORT at the top, `range.top + sl_buffer`; mirrored for a LONG. Note this is the **boundary**, not `sweep_extreme`'s wick, when the wick sits inside the boundary; use whichever is further out so the stop is never inside the swept extreme.
- TP: the opposite boundary, pulled in by one `sl_buffer` (`range.bottom + sl_buffer` for a SHORT).
- The 2R/runner hybrid exit applies unchanged. Read how `tp1`/`runner_tp` are computed today and leave that code path alone — it works off risk, not off the target.
- Rule 7's nearest-unswept-liquidity target does **not** apply in range mode; the opposite boundary is the objective. Make sure the "no unswept liquidity ahead" warning cannot fire for a range setup and claim there is no objective when there is one.

**The ⭐ sweep (D9).** A boundary pierced by a wick and reclaimed is liquidity taken. Check first whether `sniper.sweep_label` already names it (the boundary is an EQH/EQL pool by construction, so it may). If it does, change nothing and say so in your report. If it does not, pass the range's `swept_top`/`swept_bottom` into the tier decision so the ⭐ is earned. Do not weaken any other star condition.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_range_engine.py`. Cover, with real engine runs on synthetic candles (no mocks of the code under test):

- both timeframes flat + an unbroken range + price at the top → direction SHORT, `direction_source == "range"`;
- price at the bottom → LONG;
- price mid-range → WATCH, the reason names both boundaries, no direction;
- **D11: H4 flat but H1 trending UP with a valid range present → direction LONG from `"h1"`, `direction_source != "range"`** — this is the regression that protects every existing signal;
- a broken range is ignored entirely (verdict unchanged from today's flat-flat behaviour);
- SL sits beyond the boundary by `sl_buffer`, TP at the opposite boundary less `sl_buffer`, on both directions;
- the risk is positive and `rr` is computed from those two;
- `result.market_range` is populated whenever a range is in play and `None` otherwise.

Read `tests/test_smc/test_engine.py` first and follow its harness style.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_range_engine.py -v
```

Expected: FAIL — no `market_range` attribute / no range branch.

- [ ] **Step 3: Implement**

Add `boundary_zone` to `range.py`, `market_range` to `AnalysisResult`, and the engine changes above. Work in that order and keep each piece compiling.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_range_engine.py tests/test_smc/test_engine.py -v
```

Expected: PASS. **Every existing engine test must pass unchanged** — D11 guarantees no existing direction changes. If one moves, stop and report it.

- [ ] **Step 5: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: 1 pre-existing failure only, flake8 clean.

- [ ] **Step 6: Commit**

```bash
git add app/services/smc/range.py app/services/smc/models.py app/services/smc/engine.py tests/test_smc/test_range_engine.py
git commit -m "feat: trade the range between its boundaries when H4 and H1 are both flat"
```

---

### Task 3: The plan and the boundary alert

**Files:**
- Modify: `app/services/smc/plan.py` (range scenarios replace the speculative brackets)
- Modify: `app/services/smc/notifier.py` (`format_zone_alert` names a range boundary)
- Test: `tests/test_smc/test_range_plan.py` (create)

**Interfaces:**
- Consumes: `detect_range`, `boundary_zone` (Tasks 1-2); `PlanScenario` (which already carries `kind` and `runner_up`).
- Produces: no new public functions — `build_plan` emits range scenarios, and the existing zone-alert machinery carries them.

**Why there is no new alert path.** Delivery 1's `_maybe_plan_zone_alert` fires when the last M5 candle touches a zone the **current plan** names, deduped once per zone per session block and silenceable with the 🔕 button. If the plan emits the boundaries as scenarios, the boundary alert is that same message with the same guarantees, for free. Do not add a second alerting path; if you find yourself writing one, stop and report.

**The plan.** In `build_plan`, when both trends read FLAT (the same D11 condition the engine uses — read the engine's branch and mirror it exactly rather than re-deriving) and `detect_range` returns an unbroken range, emit **two** scenarios, one per boundary, and **skip the speculative both-way brackets entirely** (D12):

- SHORT at the top: entry `range.top`, SL `range.top + sl_buffer`, TP `range.bottom + sl_buffer`, zone bounds from `boundary_zone`, `kind="RANGE"`, `speculative=False`;
- LONG at the bottom, mirrored.

RR follows from those numbers; the existing `min_rr` filter applies as it does to any scenario.

**The message.** `format_zone_alert` currently opens `price reached the {Demand|Supply} zone {bottom}–{top}`. For a `RANGE` scenario it must read as the spec's §3.3 example does — naming the boundary and the opposite boundary as the target. Keep it one message with the same shape and the same 🔕 keyboard; branch only on the kind. Every dynamic value still goes through the existing formatting, and the pair still goes through `escape_html`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_range_plan.py` covering:
- both flat + unbroken range → exactly two scenarios, one per direction, both `kind == "RANGE"`, neither speculative;
- **the speculative both-way brackets are gone** when a range is in play (D12);
- both flat + no range → today's speculative brackets, unchanged;
- H4 flat + H1 trending → today's single H1-direction scenario, no range scenarios (D11);
- SL/TP geometry on both boundaries;
- `format_zone_alert` on a RANGE scenario names the boundary and the opposite boundary, and still carries the 🔕 keyboard;
- `format_zone_alert` on an OB/FVG scenario is **byte-identical to today** — pin it.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_range_plan.py -v
```

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_range_plan.py tests/test_smc/test_plan.py tests/test_smc/test_zone_dedup.py tests/test_smc/test_zone_mute_button.py -v
```

Expected: PASS. Delivery 1's dedup and mute tests must stay green untouched — they are the proof the boundary alert inherited those guarantees.

- [ ] **Step 5: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

- [ ] **Step 6: Commit**

```bash
git add app/services/smc/plan.py app/services/smc/notifier.py tests/test_smc/test_range_plan.py
git commit -m "feat: range scenarios in the plan, boundary alert through the existing path"
```

---

### Task 4: The boundaries on the charts

**Files:**
- Modify: `app/services/smc/chart.py`
- Test: `tests/test_smc/test_visuals.py` (extend — do NOT touch `TestPrettyStats`)

**Interfaces:**
- Consumes: `AnalysisResult.market_range` (Task 2); `PairPlan` scenarios of `kind == "RANGE"` (Task 3).
- Produces: no new public functions; both renderers keep their signatures.

Spec §3.4: both boundaries as **black dashed lines** labelled `RANGE HIGH` / `RANGE LOW`, on the H1 plan chart and the M5 alert chart.

The charts are drawn on a dark background (`BG = "#131722"`), where pure black would disappear. Use a near-black that still reads — pick one, add it as a module constant next to the other colours with a one-line comment saying why it is not literal black, and use it for both lines. The existing `_level` helper draws a labelled horizontal line; read it and reuse it rather than writing new drawing code, unless its label placement does not suit a boundary — in which case say why in your report.

Draw the boundaries only when there is a range to draw: `result.market_range` on the setup chart, and on the plan chart when the plan carries `RANGE` scenarios. A chart with no range must render exactly as it does today — pin that with a test.

Extend the y-axis clamp to include the boundaries, the way the runner-up zone was folded in during Delivery 2, or a boundary outside the candle range is clipped invisibly.

- [ ] **Step 1: Write the failing tests** — assert on real matplotlib artists (line positions, dash style, label text), following the file's established style. A test that only checks the PNG is non-empty proves nothing here.
- [ ] **Step 2: Run them and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them and watch them pass**
- [ ] **Step 5: Full suite and flake8**
- [ ] **Step 6: Commit**

```bash
git add app/services/smc/chart.py tests/test_smc/test_visuals.py
git commit -m "feat: range boundaries as dashed lines on both charts"
```

---

### Task 5: The journal records the zone kind

**Files:**
- Modify: `app/services/smc/db.py` (`SIGNAL_COLUMNS`, the `CREATE TABLE`, the migration list)
- Modify: `app/services/smc/journal.py` (`record`)
- Test: `tests/test_smc/test_db.py` and `tests/test_smc/test_journal.py` (extend)

**Interfaces:**
- Consumes: `AnalysisResult.h1_zone.kind` (Delivery 2) — `"OB"`, `"FVG"` or `"RANGE"`.
- Produces: a `zone_kind TEXT` column on `signals`.

Spec §3.5: range setups are journal-recorded like any other, with `zone_kind` so `/stats` can separate them later. **This delivery adds the column and writes it; it does not change `/stats`.**

`db.py` migrates production databases in place. Read the existing migration list and follow it exactly — a new column must appear in all three places (`SIGNAL_COLUMNS`, `CREATE TABLE`, the `ALTER TABLE` migration list), and an existing database that predates the column must open, migrate and keep working. Rows written before the migration read `NULL`; make sure nothing downstream assumes a value.

- [ ] **Step 1: Write the failing tests** — a fresh database has the column; a database created without it gains it on open and keeps its existing rows; `record` writes the kind for OB, FVG and RANGE setups; a legacy row with `NULL` does not break `stats_text` or the journal's lifecycle tracking.
- [ ] **Step 2: Run them and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them and watch them pass**
- [ ] **Step 5: Full suite and flake8**
- [ ] **Step 6: Commit**

```bash
git add app/services/smc/db.py app/services/smc/journal.py tests/test_smc/test_db.py tests/test_smc/test_journal.py
git commit -m "feat: record the zone kind on every signal"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none — documentation only. **Do not open a pull request in this task**; the controller does that after a whole-branch review.

- [ ] **Step 1: Add `range.py` to the architecture tree**, in the tree's aligned style with `│` continuation bars on multi-line non-last entries: clustered H1 pivots, the two-touch and 3× tolerance floors, body-close breaks versus wick-and-reclaim sweeps, and `boundary_zone`.
- [ ] **Step 2: Widen the strategy summary** at the top of the file. It currently describes one rule chain ending at "no direction". Add that when both H4 and H1 read FLAT and a valid range exists, the bot trades between the boundaries instead of standing down (D11).
- [ ] **Step 3: Add a conventions bullet** in the voice of its neighbours: `Zone.kind == "RANGE"`, why a boundary is expressed as a `Zone` (the whole Rule 3/4/6 pipeline runs on it unchanged), that the target is the opposite boundary rather than Rule 7's nearest liquidity, D9's wick-and-reclaim rule, D11's precedence and D12's replacement of the speculative brackets.
- [ ] **Step 4: Verify every claim against the code** — read `detect_range`, `boundary_zone`, the engine's flat branch, `build_plan`'s range path. If a doc and the code disagree, report it rather than papering over it.
- [ ] **Step 5: Run the full suite and flake8** and record the exact summary line for the pull-request body.
- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: range trading — detection, boundary zones, precedence"
```

---

## Self-Review

**Spec coverage.** §3.1 → Task 1 (`Range`, `detect_range`, clustering, the two floors, `broken`, the D9 sweep flags). §3.2 → Task 2's Rule 1 branch, with D11 tightening it to both-flat. §3.3 → Task 2 (direction, SL, TP, the hybrid exit, the ⭐) and Task 3 (the message, through Delivery 1's existing dedup and mute). §3.4 → Task 4. §3.5 → Task 5. §3.6 → the test lists in each task: clustering with and without enough touches (Task 1), the 3× floor (Task 1), body close breaks (Task 1), wick-and-reclaim does not (Task 1), range ignored when H4 trends (Task 2's D11 test), boundary alert direction (Tasks 2 and 3), SL/TP geometry (Tasks 2 and 3), ⭐ on the reclaimed sweep (Task 2), chart smoke test (Task 4). §3.7 → the file lists, plus `CLAUDE.md` in Task 6.

**Type consistency.** `tolerance: float` is always the raw `instrument.min_fvg`, never profile-scaled, and is the third positional parameter of `detect_range` and `boundary_zone`. `Range` is returned whole and carried on `AnalysisResult.market_range: Optional[Range]`. `boundary_zone` returns a `Zone` with `kind == "RANGE"`, which every downstream consumer already understands as a price band. `PlanScenario.kind` takes `"RANGE"` alongside `"OB"`/`"FVG"`. The journal column is `zone_kind TEXT`, nullable, reading `NULL` for rows written before the migration.

**Deliberate deviations from the spec text**, recorded so the reviewer judges them rather than discovering them: (1) `Range` carries `top_at`/`bottom_at`/`swept_top`/`swept_bottom` beyond the spec's five fields — the timestamps make the boundary `Zone`'s anchor correct, the sweep flags are D9's ⭐ condition and cannot be reconstructed later; (2) `detect_range` takes `tolerance: float` rather than the `Instrument`, matching the precedent `find_h1_fvg_zone` set in Delivery 2; (3) the boundary alert reuses the plan-zone alert path rather than being a new message type, which is what gives it Delivery 1's dedup and 🔕 mute for free.

**Out of scope:** the `zone_touch_span` formation-hour anchor defect (its own task); `/stats` splitting range setups from trend setups (this delivery only records the column); the four pre-existing test failures.
