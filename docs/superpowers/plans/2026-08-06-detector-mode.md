# Detector Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the watcher from a trade prescriber into a setup detector — announce every completed Triple Sync + Imbalance with the levels the owner trades, and stop deciding for him whether the arithmetic is good enough.

**Architecture:** Three checks that currently return `Verdict.SKIP` after a setup has fully formed become warnings carried on the result. Two new pure helpers (an M5 order block and two ladders) add the levels he asked for. The notifier rebuilds the alert around four actionable lines plus ladders. The plan remembers its zones for the day so alerts can say whether they were foreseen. Yahoo is removed and a data-source failure now speaks up.

**Tech Stack:** Python 3.13, dataclasses, pytest (`asyncio_mode=auto`), flake8. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-06-detector-mode-design.md`. Read it before Task 1.
- Branch: `feat/detector-mode` (exists, spec already committed, based on merged master).
- **All bot-facing text is English** (`CLAUDE.md`). The owner reviewed the format in Russian and confirmed on 2026-08-06 that the shipped strings stay English. Translate the approved layout, keep its structure.
- Every dynamic string in a Telegram message MUST pass through `notifier.escape_html`.
- Per-instrument parameters live in `instruments.py` only.
- Distances render in the instrument's own units: dollars for crypto, pips for forex — reuse the `engine._fmt_size` split, do not re-derive it.
- Liquidity tolerance is the raw `Instrument.min_fvg`, never the profile-scaled value.
- `chart.py` stays matplotlib-only; chart rendering must never block an alert.
- Tests are network-free and build candles via `tests/test_smc/helpers.py`.
- Run `pytest tests/ -q` and `flake8 app/ tests/ smc_watcher.py scripts/` before every commit. 229 tests pass today.
- Do not touch: session windows, news blackouts, discipline rules, Rule 10 order life, or the detection chain (Rules 1–4). Only what suppresses a *message* changes.

## Target alert

The English rendering of the format approved on 2026-08-06:

```
🚨 SETUP READY — USDCAD · SHORT · H4 downtrend
   from this morning's plan

📍 H1 Supply zone      1.40295 – 1.40597
⚡ M5 imbalance (FVG)   1.40192 – 1.40243   ← limit order
🧱 M5 order block       1.40271 – 1.40306   ← deeper entry
🛑 Swept liquidity      1.40314   ← stop behind the wick (1.40329 with buffer)

🎯 Unswept liquidity ahead      RR from FVG / from OB
     1.40154      3.8 pips   1:0.2 / 1:1.8   H1
     1.40133      5.9 pips   1:0.3 / 1:2.1   M5
     1.40098      9.4 pips   1:0.6 / 1:2.7   M5 · EQL x5
     1.40036     15.6 pips   1:1.0 / 1:3.8   M5 · EQL x3
     1.39950     24.2 pips   1:1.7 / 1:5.3   M5 · EQL x3

🧱 Untested zones further out
     1.40729 – 1.40801   (0 touches)

   ⚠️ RR to the nearest liquidity is 1:0.2

   ref · FVG 0.00051, 0% filled · New York
   ref · 31.07 19:55 Prague · price 1.40183
```

---

### Task 1: Order block and ladders

Pure functions, no engine changes. Everything here is unit-testable on synthetic candles.

**Files:**
- Modify: `app/services/smc/structure.py`
- Modify: `app/services/smc/liquidity.py`
- Test: `tests/test_smc/test_structure.py`, `tests/test_smc/test_liquidity.py`

**Interfaces:**
- Produces:
  - `structure.find_order_block(candles: List[Candle], direction: Direction, before_index: int) -> Optional[Zone]`
  - `structure.zone_ladder(candles: List[Candle], direction: Direction, beyond: float, limit: int = 5) -> List[Zone]`
  - `liquidity.liquidity_ladder(levels: List[LiquidityLevel], direction: Direction, entry: float, limit: int = 5) -> List[LiquidityLevel]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_smc/test_structure.py`:

```python
class TestOrderBlock:
    """The M5 order block: the last candle opposing the trade before the
    impulse that broke structure (owner definition, 2026-08-06)."""

    def test_long_takes_the_last_bearish_candle_wick_to_body(self):
        # index 3 is the last bearish candle before the up-impulse
        spec = [
            (100.0, 101.0, 99.0, 100.5),   # 0 bullish
            (100.5, 101.0, 99.5, 100.0),   # 1 bearish
            (100.0, 100.5, 99.0, 100.2),   # 2 bullish
            (100.2, 100.4, 98.0, 98.5),    # 3 bearish  <- the block
            (98.5, 103.0, 98.4, 102.5),    # 4 impulse
            (102.5, 105.0, 102.0, 104.5),  # 5 impulse
        ]
        m5 = [candle(*row, index=i) for i, row in enumerate(spec)]
        ob = find_order_block(m5, Direction.LONG, before_index=5)
        # demand convention matches build_zone: low -> body_high
        assert (ob.bottom, ob.top) == (98.0, 100.2)
        assert ob.is_demand is True

    def test_short_takes_the_last_bullish_candle_body_to_wick(self):
        spec = [
            (100.0, 101.0, 99.5, 100.5),
            (100.5, 101.0, 100.0, 100.2),
            (100.2, 102.5, 100.1, 102.0),   # 2 bullish  <- the block
            (102.0, 102.2, 98.0, 98.5),     # 3 impulse down
            (98.5, 98.8, 96.0, 96.5),
        ]
        m5 = [candle(*row, index=i) for i, row in enumerate(spec)]
        ob = find_order_block(m5, Direction.SHORT, before_index=4)
        # supply convention: body_low -> high
        assert (ob.bottom, ob.top) == (100.2, 102.5)
        assert ob.is_demand is False

    def test_none_when_no_opposing_candle_exists(self):
        spec = [(100.0 + i, 101.0 + i, 99.5 + i, 100.5 + i) for i in range(5)]
        m5 = [candle(*row, index=i) for i, row in enumerate(spec)]
        assert find_order_block(m5, Direction.LONG, before_index=4) is None


class TestZoneLadder:
    def test_returns_untested_zones_beyond_the_entry_nearest_first(self):
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        zones = zone_ladder(h1, Direction.LONG, beyond=3140.0, limit=5)
        assert zones, "H1_PULLBACK_CLOSES has an untested supply above 3140"
        prices = [z.bottom for z in zones]
        assert prices == sorted(prices), "nearest first"
        assert all(z.bottom > 3140.0 for z in zones)

    def test_limit_is_respected(self):
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        assert len(zone_ladder(h1, Direction.LONG, 3000.0, limit=1)) <= 1
```

Add to `tests/test_smc/test_liquidity.py`:

```python
class TestLiquidityLadder:
    def _level(self, price, is_high=True, tf="H1", count=1):
        return LiquidityLevel(price=price, is_high=is_high, timeframe=tf,
                              equal_count=count, timestamp=None)

    def test_long_ladder_is_nearest_first_and_deduplicated(self):
        levels = [
            self._level(3180.0, tf="M5"), self._level(3180.0, tf="H1"),
            self._level(3210.0), self._level(3160.0), self._level(3100.0),
        ]
        out = liquidity_ladder(levels, Direction.LONG, entry=3150.0, limit=5)
        assert [lv.price for lv in out] == [3160.0, 3180.0, 3210.0]

    def test_short_ladder_walks_downward(self):
        levels = [self._level(p, is_high=False) for p in
                  (3140.0, 3100.0, 3120.0, 3200.0)]
        out = liquidity_ladder(levels, Direction.SHORT, entry=3150.0, limit=5)
        assert [lv.price for lv in out] == [3140.0, 3120.0, 3100.0]

    def test_limit_truncates(self):
        levels = [self._level(3150.0 + 10 * i) for i in range(1, 9)]
        assert len(liquidity_ladder(levels, Direction.LONG, 3150.0, limit=5)) == 5

    def test_duplicate_price_keeps_the_richer_pool(self):
        levels = [self._level(3180.0, tf="M5", count=1),
                  self._level(3180.0, tf="H1", count=4)]
        out = liquidity_ladder(levels, Direction.LONG, 3150.0, limit=5)
        assert len(out) == 1 and out[0].equal_count == 4
```

Import `find_order_block`, `zone_ladder`, `liquidity_ladder` and `Direction` in the respective modules.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_smc/test_structure.py::TestOrderBlock tests/test_smc/test_structure.py::TestZoneLadder tests/test_smc/test_liquidity.py::TestLiquidityLadder -v`
Expected: FAIL — `ImportError: cannot import name 'find_order_block'`

- [ ] **Step 3: Implement in structure.py**

```python
def find_order_block(
    candles: List[Candle], direction: Direction, before_index: int
) -> Optional[Zone]:
    """The last candle opposing the trade before the impulse (owner
    definition, 2026-08-06). Price reacts to it on the retest, and it sits
    deeper than the FVG edge — a second limit option.

    Boundaries follow `build_zone`'s convention so the bot has one rule
    everywhere: demand = low -> body_high, supply = body_low -> high.
    """
    want_bearish = direction == Direction.LONG
    for i in range(min(before_index, len(candles)) - 1, -1, -1):
        c = candles[i]
        opposing = (c.close < c.open) if want_bearish else (c.close > c.open)
        if not opposing:
            continue
        if want_bearish:
            return Zone(bottom=c.low, top=c.body_high, is_demand=True,
                        pivot_index=i, timestamp=c.timestamp)
        return Zone(bottom=c.body_low, top=c.high, is_demand=False,
                    pivot_index=i, timestamp=c.timestamp)
    return None


def zone_ladder(
    candles: List[Candle], direction: Direction, beyond: float, limit: int = 5
) -> List[Zone]:
    """Untested opposite zones past `beyond`, nearest first — the order blocks
    still standing between the entry and the horizon."""
    want_high = direction == Direction.LONG
    out: List[Zone] = []
    seen = set()
    for pivot in (p for p in find_pivots(candles) if p.is_high == want_high):
        zone = _mark_zone_state(candles, build_zone(candles, pivot))
        if zone.invalidated or zone.tested:
            continue
        if (zone.bottom > beyond) if want_high else (zone.top < beyond):
            key = (round(zone.bottom, 8), round(zone.top, 8))
            if key in seen:
                continue
            seen.add(key)
            out.append(zone)
    out.sort(key=lambda z: z.bottom if want_high else -z.top)
    return out[:limit]
```

- [ ] **Step 4: Implement in liquidity.py**

```python
def liquidity_ladder(
    levels: List[LiquidityLevel], direction: Direction, entry: float,
    limit: int = 5,
) -> List[LiquidityLevel]:
    """The pools ahead, nearest first — not just the closest one.

    The owner takes profit at liquidity and there is more than one pool; on a
    real USDCAD setup the first rung to reach 1:1 was the fourth. Duplicate
    prices across timeframes collapse to the richer pool.
    """
    if direction == Direction.LONG:
        ahead = [lv for lv in levels if lv.is_high and lv.price > entry]
        ahead.sort(key=lambda lv: lv.price)
    else:
        ahead = [lv for lv in levels if not lv.is_high and lv.price < entry]
        ahead.sort(key=lambda lv: -lv.price)
    best: dict = {}
    order: List[float] = []
    for lv in ahead:
        key = round(lv.price, 8)
        if key not in best:
            best[key] = lv
            order.append(key)
        elif (lv.equal_count, _TF_RANK.get(lv.timeframe, 0)) > (
            best[key].equal_count, _TF_RANK.get(best[key].timeframe, 0)
        ):
            best[key] = lv
    return [best[k] for k in order[:limit]]
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_smc/test_structure.py tests/test_smc/test_liquidity.py -v`
Expected: PASS.

Run: `pytest tests/ -q && flake8 app/ tests/ smc_watcher.py scripts/`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/services/smc/structure.py app/services/smc/liquidity.py tests/
git commit -m "Add the M5 order block and the liquidity/zone ladders"
```

---

### Task 2: Gates become labels

**Files:**
- Modify: `app/services/smc/models.py`
- Modify: `app/services/smc/engine.py`
- Modify: `app/services/smc/journal.py` (nullable take-profit)
- Modify: `app/services/smc/chart.py` (nullable take-profit)
- Test: `tests/test_smc/test_engine.py`

**Interfaces:**
- Consumes: Task 1's `find_order_block`, `liquidity_ladder`, `zone_ladder`.
- Produces:
  - `AnalysisResult.warnings: List[str]`
  - `TradeSetup.take_profit: Optional[float]` (was `float`)
  - `TradeSetup.order_block: Optional[Zone]`
  - `TradeSetup.ladder: List[LiquidityLevel]`
  - `TradeSetup.zones_ahead: List[Zone]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_smc/test_engine.py`. Note `_engine()`'s defaults carry `min_rr=1.0, max_entry_gap_r=99.0` today; the new tests set them explicitly.

```python
class TestGatesAreNowLabels:
    """Detector mode (2026-08-06): a completed setup is always announced; the
    thresholds annotate rather than suppress."""

    def _run(self, price=None, **kwargs):
        result = _fresh_result()
        if price is not None:
            result.price = price
        return _engine(**kwargs).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(), result=result,
        )

    def test_low_rr_is_announced_with_a_warning(self):
        result = self._run(min_rr=8.0)
        assert result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET)
        assert result.setup is not None
        assert any("RR" in w for w in result.warnings)

    def test_stale_entry_is_announced_with_a_warning(self):
        result = self._run(price=3160.0, max_entry_gap_r=0.5)
        assert result.setup is not None
        assert any("past the imbalance" in w for w in result.warnings)
        assert "1.2R" in " ".join(result.warnings)

    def test_clean_setup_has_no_warnings(self):
        result = self._run(min_rr=1.0, max_entry_gap_r=99.0)
        assert result.setup is not None
        assert result.warnings == []

    def test_setup_carries_the_order_block_and_both_ladders(self):
        setup = self._run().setup
        assert setup.order_block is not None
        assert setup.order_block.top < setup.entry, "OB is a deeper long entry"
        assert 1 <= len(setup.ladder) <= 5
        assert setup.ladder[0].price == setup.target.price, \
            "the ladder starts at the nearest pool"
```

Add a no-liquidity case. Build it by forcing the target search to come up empty — use a fixture whose entry sits above every unswept high:

```python
    def test_no_liquidity_ahead_is_announced_without_a_take_profit(self, monkeypatch):
        import app.services.smc.engine as E
        monkeypatch.setattr(E, "nearest_liquidity", lambda *a, **k: None)
        monkeypatch.setattr(E, "liquidity_ladder", lambda *a, **k: [])
        result = self._run()
        assert result.setup is not None
        assert result.setup.take_profit is None
        assert result.setup.target is None
        assert any("no unswept liquidity" in w.lower() for w in result.warnings)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_smc/test_engine.py::TestGatesAreNowLabels -v`
Expected: FAIL — `AttributeError: 'AnalysisResult' object has no attribute 'warnings'`

- [ ] **Step 3: Extend the models**

In `app/services/smc/models.py`, `TradeSetup`:

```python
    take_profit: Optional[float]
    ...
    order_block: Optional["Zone"] = None
    ladder: List["LiquidityLevel"] = field(default_factory=list)
    zones_ahead: List["Zone"] = field(default_factory=list)
```

`take_profit` loses its non-optional status — a setup with no unswept liquidity ahead is still a setup, it just has no structural objective. Keep the field ordering valid for dataclasses (non-default fields first).

`AnalysisResult`:

```python
    # Detector mode: thresholds annotate instead of suppressing. Each entry is
    # a finished English sentence ready for the alert.
    warnings: List[str] = field(default_factory=list)
```

- [ ] **Step 4: Rewrite the engine's three gates**

In `app/services/smc/engine.py`, the Rule 5.1 block (currently ~line 252) stops returning:

```python
        # Rule 5.1 (owner decision 2026-08-05, demoted to a label 2026-08-06)
        # — entry staleness. Replaying without this check, 80-90% of limit
        # orders never filled; but the owner sets his own entry, so this warns
        # instead of suppressing. A negative gap means price has not run past
        # the entry, so market entries never trigger it.
        price = result.price or m5[-1].close
        gap = price - entry if direction == Direction.LONG else entry - price
        if gap > self.max_entry_gap_r * risk:
            result.warnings.append(
                f"price has run {gap / risk:.1f}R past the imbalance"
            )
```

Rule 7 keeps computing the target but never rejects:

```python
        tolerance = self.instrument.min_fvg
        levels = (
            find_liquidity(m5, "M5", tolerance)
            + find_liquidity(h1, "H1", tolerance)
            + find_liquidity(h4, "H4", tolerance)
        )
        target = nearest_liquidity(levels, direction, entry)
        ladder = liquidity_ladder(levels, direction, entry)
        take_profit = None
        rr = 0.0
        if target is None:
            result.warnings.append("no unswept liquidity ahead")
        else:
            if direction == Direction.LONG:
                take_profit = target.price - self.sl_buffer
                reward = take_profit - entry
            else:
                take_profit = target.price + self.sl_buffer
                reward = entry - take_profit
            if reward <= 0:
                # The pool sits inside the buffer: the objective would land on
                # the wrong side of the entry. Report it, do not invent a TP.
                take_profit, target = None, None
                result.warnings.append(
                    "nearest liquidity sits inside the stop buffer"
                )
            else:
                rr = reward / risk
                if rr < self.min_rr:
                    result.warnings.append(
                        f"RR to the nearest liquidity is 1:{rr:.1f}"
                    )
```

Then build the setup with the new fields:

```python
        order_block = find_order_block(m5, direction, fvg.index - 1)
        zones_ahead = zone_ladder(h1, direction, entry)
        ...
        result.setup = TradeSetup(
            direction=direction,
            entry=round(entry, d),
            stop_loss=round(stop_loss, d),
            take_profit=round(take_profit, d) if take_profit is not None else None,
            rr=round(rr, 2),
            fvg=fvg,
            entry_is_market=entry_is_market,
            lot_hint=lot_hint,
            target=target,
            order_block=order_block,
            ladder=ladder,
            zones_ahead=zones_ahead,
        )
```

Add the imports: `find_order_block`, `zone_ladder` from `structure`; `liquidity_ladder` from `liquidity`.

- [ ] **Step 5: Handle the nullable take-profit downstream**

`grep -rn "take_profit" app/ smc_watcher.py` and make every consumer None-safe:

- `journal.py` — a signal with no take-profit can still fill and still stop out. Persist `NULL`, and in the outcome check treat "TP hit" as impossible when it is None. Do not invent a target.
- `chart.py` — skip the TP line and its edge annotation when None.
- `notifier.py` — Task 4 rewrites this file; leave it for now beyond keeping it from crashing.

Add one regression test per file you touch, asserting the None path does not raise.

- [ ] **Step 6: Run the tests**

Run: `pytest tests/ -q && flake8 app/ tests/ smc_watcher.py scripts/`
Expected: all green. Tests that asserted a SKIP on low RR or a stale entry now assert the warning instead — recompute them, do not delete them.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Detector mode: the three post-setup gates become warnings"
```

---

### Task 3: Direction from H1 when H4 is flat

**Files:**
- Modify: `app/services/smc/engine.py`
- Modify: `app/services/smc/models.py` (`AnalysisResult.direction_source`)
- Test: `tests/test_smc/test_engine.py`

**Interfaces:**
- Produces: `AnalysisResult.direction_source: str` — one of `"h4"`, `"h1"`, `"h4_choch"`.

- [ ] **Step 1: Write the failing test**

```python
class TestH1DirectionFallback:
    """When H4 has no clean structure but H1 does, announce with H1's
    direction (owner decision 2026-08-06). Measured at about +40% announced
    setups, not a multiple."""

    def test_flat_h4_takes_direction_from_h1(self):
        # H4 flat: alternating closes give neither HH+HL nor LH+LL
        h4 = make_candles([3000, 3100, 3000, 3100, 3000, 3100, 3000] * 4,
                          step_minutes=240)
        result = _engine().evaluate(
            h4=h4, h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(), result=_fresh_result(),
        )
        assert result.h4_trend is Trend.FLAT
        assert result.direction_source == "h1"

    def test_clean_h4_still_wins(self):
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(), result=_fresh_result(),
        )
        assert result.direction_source == "h4"
```

Verify the flat-H4 fixture really reads FLAT before relying on it; adjust the closes until `detect_trend` agrees, and record the fixture you settled on in the test's comment.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_smc/test_engine.py::TestH1DirectionFallback -v`
Expected: FAIL — no `direction_source`.

- [ ] **Step 3: Implement**

In `evaluate`, replace the direction block:

```python
        result.h4_trend = detect_trend(h4)
        direction = None
        result.direction_source = "h4"
        if result.h4_trend == Trend.UP:
            direction = Direction.LONG
        elif result.h4_trend == Trend.DOWN:
            direction = Direction.SHORT
        else:
            # H4 has no clean HH+HL / LH+LL. H1 is still a trend, just a
            # lower one — the owner trades those (2026-08-06). This is not
            # the aggressive profile's first-leg CHoCH entry, which stays
            # below and keeps its own label.
            h1_trend = detect_trend(h1)
            if h1_trend == Trend.UP:
                direction, result.direction_source = Direction.LONG, "h1"
            elif h1_trend == Trend.DOWN:
                direction, result.direction_source = Direction.SHORT, "h1"
            elif self.profile.allow_h4_choch_entry:
                direction = h4_choch_direction(h4)
                result.direction_source = "h4_choch"
```

Keep the existing "no direction" SKIP when `direction is None` — that is a pre-setup suppression and stays.

- [ ] **Step 4: Run and commit**

Run: `pytest tests/ -q && flake8 app/ tests/ smc_watcher.py scripts/`

```bash
git add -A
git commit -m "Take direction from H1 when H4 has no clean structure"
```

---

### Task 4: The alert

**Files:**
- Modify: `app/services/smc/notifier.py`
- Test: `tests/test_smc/test_html_escaping.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `notifier.format_distance(value: float, instrument) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
def _approved(min_rr=1.0, max_entry_gap_r=99.0, price=None, no_liquidity=False,
              monkeypatch=None):
    """A real APPROVED result, produced by the engine rather than hand-built —
    the alert must render what the engine actually emits."""
    if no_liquidity:
        import app.services.smc.engine as E
        monkeypatch.setattr(E, "nearest_liquidity", lambda *a, **k: None)
        monkeypatch.setattr(E, "liquidity_ladder", lambda *a, **k: [])
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )
    result.session_name = "New York"
    if price is not None:
        result.price = price
    engine = TripleSyncEngine(min_fvg_size=2.0, sl_buffer=2.0,
                              min_rr=min_rr, max_entry_gap_r=max_entry_gap_r)
    return engine.evaluate(
        h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        m5=m5_long_trigger_deep_sweep(), result=result,
    )


class TestDetectorAlert:

    def test_four_actionable_lines_come_first(self):
        text = format_result(_approved())
        head = text.split("🎯")[0]
        for marker in ("H1 Demand zone", "M5 imbalance", "M5 order block",
                       "Swept liquidity"):
            assert marker in head

    def test_ladder_shows_both_rr_columns(self):
        text = format_result(_approved())
        assert "RR from FVG / from OB" in text
        assert text.count("1:") >= 5

    def test_warnings_render_between_levels_and_reference(self):
        text = format_result(_approved(min_rr=8.0))
        assert text.index("⚠️") > text.index("🎯")
        assert text.index("⚠️") < text.index("ref ·")

    def test_no_take_profit_renders_without_a_bogus_level(self, monkeypatch):
        text = format_result(_approved(no_liquidity=True, monkeypatch=monkeypatch))
        assert "no unswept liquidity ahead" in text
        assert "🎯" not in text or "—" in text

    def test_output_is_valid_telegram_html(self):
        _assert_valid_telegram_html(format_result(_approved(min_rr=8.0)))

    def test_distance_units_follow_the_instrument(self):
        assert format_distance(10.08, get_instrument("ETHUSD")) == "$10.08"
        assert format_distance(0.00038, get_instrument("USDJPY")) == "0.0 pips"
```

Fix the USDJPY expectation once you know the real conversion — `0.00038 / 0.01 = 0.038` pips. Pick a value that reads sensibly and assert that; do not adjust the function to match a guessed string.

- [ ] **Step 2: Run to verify failure, then implement**

```python
def format_distance(value: float, instrument) -> str:
    """Distances read in the instrument's own units — the same split
    `engine._fmt_size` makes. "1008 pips" for a $10 ETH move is how a level
    gets misjudged at a glance."""
    if instrument.source == "crypto":
        return f"${value:,.2f}"
    return f"{value / instrument.pip:.1f} pips"


def _ladder_lines(setup, instrument) -> List[str]:
    """One rung per pool: price, distance, RR from the FVG edge and — when an
    order block exists — from the block. Two entries, two RRs; the owner picks."""
    d = instrument.price_decimals
    risk = abs(setup.entry - setup.stop_loss)
    is_long = setup.direction == Direction.LONG
    ob_entry = None
    if setup.order_block:
        ob_entry = setup.order_block.top if is_long else setup.order_block.bottom
    ob_risk = abs(ob_entry - setup.stop_loss) if ob_entry is not None else None

    header = "🎯 Unswept liquidity ahead"
    header += "      RR from FVG / from OB" if ob_risk else "      RR from FVG"
    out = [header]
    for lv in setup.ladder:
        tp = lv.price - instrument.sl_buffer if is_long             else lv.price + instrument.sl_buffer
        rr = (tp - setup.entry if is_long else setup.entry - tp) / risk
        cell = f"1:{rr:.1f}"
        if ob_risk:
            rr_ob = (tp - ob_entry if is_long else ob_entry - tp) / ob_risk
            cell += f" / 1:{rr_ob:.1f}"
        pool = f" · EQ{'H' if lv.is_high else 'L'} x{lv.equal_count}"             if lv.equal_count > 1 else ""
        out.append(
            f"     {lv.price:.{d}f}   "
            f"{format_distance(abs(lv.price - setup.entry), instrument):>10}   "
            f"{cell}   {escape_html(lv.timeframe + pool)}"
        )
    if not setup.ladder:
        out.append("     — none ahead")
    return out
```

`format_result` gains an `in_plan: Optional[bool] = None` parameter and rebuilds its approved branch to the target layout at the top of this plan. Rules:

- Header: symbol · side · direction source. `direction_source == "h1"` renders `⚠️ H4 flat — direction from H1 downtrend`; `"h4_choch"` renders `⚠️ H4 flat — direction from CHoCH (first leg, not with-trend)`.
- The plan-provenance line is emitted only when the caller passes one (Task 5); absent otherwise.
- Order-block line only when `setup.order_block` is set.
- Ladder: up to five rungs, each `price · distance · RR-from-FVG / RR-from-OB · timeframe · pool`. The second RR column appears only when an order block exists.
- Zones-ahead block only when `setup.zones_ahead` is non-empty.
- Warnings from `result.warnings`, one per line, prefixed `⚠️`, plus the `▶️ price is inside the imbalance right now` line when `entry_is_market`.
- Every dynamic value through `escape_html`.

- [ ] **Step 3: Run, then commit**

```bash
git add -A
git commit -m "Rebuild the alert around four levels and two ladders"
```

---

### Task 5: Plan anchoring, zone dedup, and the plan's blocker

**Files:**
- Modify: `app/services/smc/state.py`
- Modify: `smc_watcher.py` (`_setup_fingerprint`, `_send_pair_plan`, alert path)
- Modify: `app/services/smc/plan.py` (blocker note)
- Modify: `app/services/smc/notifier.py` (`format_plan` note rendering)
- Test: `tests/test_smc/test_plan.py`, `tests/test_smc/test_multipair.py`

**Interfaces:**
- Produces:
  - `WatcherState.plan_zones: Dict[str, List[Tuple[float, float]]]` keyed by pair, cleared on a new Prague day.
  - `PairPlan.blocker: Optional[str]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_plan_zones_are_remembered_and_matched_by_overlap():
    state = WatcherState(Database(":memory:"), ["ETHUSD"])
    state.remember_plan_zones("ETHUSD", [(3131.0, 3138.0)])
    assert state.zone_was_planned("ETHUSD", 3135.0, 3142.0) is True   # overlaps
    assert state.zone_was_planned("ETHUSD", 3200.0, 3210.0) is False

def test_dedup_key_is_the_zone_not_the_entry():
    # two different imbalances in the same zone, same session -> one key
    a = _result_with(zone=(3131.0, 3138.0), entry=3140.5)
    b = _result_with(zone=(3131.0, 3138.0), entry=3139.0)
    assert _setup_fingerprint(a) == _setup_fingerprint(b)

def test_plan_names_the_missing_stage():
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles([3100] * 20, step_minutes=60)   # no clean zone
    plan = build_plan(ETH, h4, h1, make_candles([3100, 3101]), min_rr=1.0)
    assert plan.scenarios == []
    assert "untested" in plan.blocker.lower()
```

- [ ] **Step 2: Implement**

`state.py` — a dict of pair → list of `(bottom, top)` plus the Prague date it was stored for; `remember_plan_zones` overwrites for the day, `zone_was_planned` returns True on any overlap in stored ranges. Persist through the existing kv store like `pair_profile` does.

`smc_watcher._setup_fingerprint` keys on the zone instead of the entry:

```python
def _setup_fingerprint(result: AnalysisResult) -> str:
    """One announcement per zone per session block. A second imbalance in the
    same zone is the same trading idea — the owner has already placed his
    order or decided not to."""
    z = result.h1_zone
    day = result.checked_at.strftime("%Y-%m-%d")
    return (
        f"{result.symbol}:{result.setup.direction.value}:"
        f"{z.bottom}-{z.top}:{result.session_name}:{day}"
    )
```

`_send_pair_plan` calls `state.remember_plan_zones(key, [(s.zone_bottom, s.zone_top) for s in plan.scenarios])`.

The alert path passes `in_plan=state.zone_was_planned(...)` to `format_result`, or `None` when no plan was stored today.

`plan.py` sets `PairPlan.blocker` from the stage it stopped at, reusing the live checklist's vocabulary (`AnalysisResult.watch_notes` wording): no H4/H1 direction → "Wait for a clear HH+HL or LH+LL structure on H4"; no zone → "Wait for a fresh untested H1 demand/supply zone"; below `min_rr` → the existing RR sentence. Stages the plan cannot evaluate (CHoCH, imbalance) are never reported as missing.

- [ ] **Step 3: Say what /stats actually measures**

Spec §4: the journal still tracks the bot's reference entry/SL/TP, but the
owner now sets his own levels, so `/stats` must not read as his performance.
Add one line to `journal.stats_text`:

```
Tracked against the bot's reference levels, not your actual orders.
```

Real trade performance already comes from `/journal` (MT4 screenshots) and is
untouched. Add a test asserting the line is present in `stats_text()` output.

- [ ] **Step 4: Run and commit**

```bash
git add -A
git commit -m "Anchor alerts to the morning plan; dedup by zone; name the plan's blocker"
```

---

### Task 6: Remove Yahoo, speak up when the data source fails

**Files:**
- Delete: `app/services/smc/yahoo.py` and its tests
- Modify: `smc_watcher.py` (`_forex_source`, `_build_fetcher`, module docstring), `app/core/config.py` (`forex_source` description)
- Modify: `env.example`
- Test: `tests/test_smc/test_multipair.py`

**Interfaces:**
- Produces: a Telegram warning when a forex fetch fails, at most once per pair per hour.

- [ ] **Step 1: Write the failing test**

```python
async def test_data_source_failure_warns_once_per_hour(monkeypatch):
    """The owner rotates his TwelveData key. Without Yahoo as a keyless
    fallback, an expired key means silence — which looks exactly like a quiet
    market. It must not."""
    watcher = _watcher()
    monkeypatch.setattr(watcher, "_build_fetcher", _raising_fetcher)
    await watcher.run_cycle()
    assert any("data source" in m.lower() for m in watcher.notifier.sent)
    watcher.notifier.sent.clear()
    await watcher.run_cycle()
    assert watcher.notifier.sent == [], "second failure inside the hour is quiet"
```

- [ ] **Step 2: Implement**

Delete `yahoo.py`, its import, and the fallback branch. `_forex_source` returns `twelvedata` / `oanda` and raises a clear error if neither key is configured. `_build_fetcher` no longer has a keyless default.

Wrap the per-pair fetch in the cycle: on `DataFetchError`, log, and send one warning naming the pair and the source, throttled per pair per hour via a new `WatcherState.source_warned` dict (mirror `news_warned`).

- [ ] **Step 3: Verify nothing references Yahoo**

Run: `grep -rni "yahoo" app/ tests/ scripts/ smc_watcher.py env.example README.md CLAUDE.md`
Expected: no output.

- [ ] **Step 4: Run and commit**

```bash
git add -A
git commit -m "Remove the Yahoo fallback; warn when a forex source fails"
```

---

### Task 7: Docs, env and PR

- [ ] **Step 1: Update `CLAUDE.md`**

The architecture block (`liquidity.py`, `structure.py` entries), the strategy summary in "What this project is" (the three gates are now labels; direction may come from H1), and a Conventions bullet:

```
- Detector mode: after a setup completes, nothing suppresses the message.
  `SMC_MIN_RR` and `SMC_MAX_ENTRY_GAP_R` are warning thresholds, not gates.
  Everything that suppresses BEFORE a setup completes is unchanged.
```

- [ ] **Step 2: Update `env.example` and `README.md`**

Reword both settings from "skip below this" to "warn below this". Remove `yahoo` from the `SMC_FOREX_SOURCE` documentation and state that a forex key is now required.

- [ ] **Step 3: Full verification**

Run: `pytest tests/ -q && flake8 app/ tests/ smc_watcher.py scripts/`
Run: `python -m scripts.funnel --pair ETHUSD --days 59 --legacy` — `rr_low` / `entry_stale` / `no_liquidity` should no longer appear as death stages; if `classify_result` still lists them, move them to a warning counter so the funnel does not imply rejections that no longer happen.

- [ ] **Step 4: Commit and open the PR**

```bash
git add -A
git commit -m "Document detector mode"
git push -u origin feat/detector-mode
gh pr create --fill
```

Follow `.github/pull_request_template.md`. In the body, state the measured alert rates (conservative, 90 days, production depth: ETHUSD 6.85 → 9.92/wk with the H1 fallback; five pairs pooled ~11/wk) and that a forex API key is now mandatory.

---

## Notes for the implementer

- **The spec is the authority on why**, not this plan. Read `2026-08-06-detector-mode-design.md` first; it records which decisions were measured and which were the owner's call.
- **Nothing in the detection chain changes.** If a change seems to require touching Rules 1–4, the trigger conditions, sessions, news or discipline — stop and ask.
- **Do not add a rule preferring the FVG entry over the order block, or one ladder rung over another.** Presenting both is the entire point.
- Fixture arithmetic is load-bearing. Values in these tests were derived from `m5_long_trigger_deep_sweep` with `min_fvg_size=2.0`, `sl_buffer=2.0`: entry 3140.5, stop 3124.0, risk 16.5. If a number differs, recompute from the fixture — never adjust an assertion to match output.
- `make_candles` wick convention: bullish `high = max(o,c) + 1.0`, `low = min(o,c) - 0.4`; bearish mirrored.
