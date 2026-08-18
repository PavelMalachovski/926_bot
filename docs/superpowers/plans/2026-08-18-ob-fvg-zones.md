# OB and Imbalance as Zones of Interest (Delivery 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An untouched H1 FVG becomes a zone of interest alongside the H1 order block, the zone alert names the M5 order block and imbalance inside the zone, and a setup whose H1 trend opposes H4 is labelled and denied the ⭐.

**Architecture:** `Zone` gains a `kind` field so a zone can say whether it is an order block or an imbalance. `structure.py` gains `find_h1_fvg_zone` (an untouched H1 gap expressed as a `Zone`) and `find_zone_of_interest` (order block first, imbalance as the fallback — owner decision D4); the engine's Rule 2 and `plan._scenario` both switch to the latter. A second new helper, `m5_marks`, reports the M5 order block and imbalance sitting inside the touched zone, which the zone alert prints as one extra line. The engine records the H1 trend on every path so the alert header can show H4/H1 agreement and `sniper.classify` can deny the star on disagreement.

**Tech Stack:** Python 3.11, pytest (`asyncio_mode=auto`), matplotlib (Agg, no pandas), structlog, SQLite.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-16-quiet-zones-and-range-design.md`. Delivery 2 is §2; decisions D4, D5, D6 and D10 apply.
- **The strategy specification is law.** Rules −1 through 11 come from the owner's written trading system. Never relax or "improve" a strategy rule. Implementation over-strictness may be fixed; the rules may not.
- **Detector mode** (owner decision 2026-08-06): once a setup fully forms, the alert always fires. Nothing in this delivery may add a new suppression. The ⭐ tier only changes presentation.
- **All bot-facing text is English.** Comments and docstrings are English too.
- Telegram messages use `parse_mode=HTML`; every dynamic string embedded in a message MUST go through `notifier.escape_html`. Only `<b>` and `<pre>` tags are used — add no others.
- Per-instrument parameters (`min_fvg`, `sl_buffer`, `pip`, decimals) live in `instruments.py` — never hardcode them elsewhere. `fvg_size_factor` is a **multiplier** on `Instrument.min_fvg`; the product is the minimum size, computed at the call site and passed in as a plain float.
- D10: an H1 FVG is a fresh zone of interest only while **untouched** — `measure_fill` reports zero penetration. This is deliberately stricter than Rule 4's `fill < 50%` for the M5 entry imbalance.
- D4: when both an order block and an imbalance qualify, the **order block wins**; the runner-up joins the zone ladder.
- `chart.py` stays matplotlib-only — no pandas, no mplfinance. Chart rendering must never block an alert (`_send_alert` already wraps it in try/except; keep it that way).
- Engines see **closed candles only**; every fetcher already drops the in-progress candle. Do not re-add one.
- Tests are network-free. Synthetic candles come from `tests/test_smc/helpers.py`.
- Run `pytest tests/ -v` and `flake8 app/ tests/ smc_watcher.py` before every commit.
- **Four tests fail on `master` and are out of scope for this branch** — `tests/test_smc/test_multipair.py::TestApiKeyNeverReachesTelegram` (3 tests) and `tests/test_smc/test_visuals.py::TestPrettyStats::test_stats_contains_bars_and_sparkline`. They are environment/date dependent. Everything else must pass.

---

### Task 1: `Zone.kind` and the untouched H1 imbalance

**Files:**
- Modify: `app/services/smc/models.py` (the `Zone` dataclass)
- Modify: `app/services/smc/structure.py` (new function after `find_h1_zone`)
- Test: `tests/test_smc/test_h1_fvg_zone.py` (create)

**Interfaces:**
- Consumes: `fvg.find_fvgs(candles, direction, from_index) -> List[FVG]` and `fvg.measure_fill(candles, fvg) -> FVG` (both exist; `measure_fill` sets `fill_pct` and `closed_through`). `structure.py` does not import `fvg` today — this task adds that import. The reverse edge does not exist (`fvg.py` imports only `models` and `sessions`), so there is no cycle.
- Produces:
  - `Zone.kind: str` — `"OB"` (default) or `"FVG"`.
  - `structure.find_h1_fvg_zone(candles: List[Candle], direction: Direction, min_size: float) -> Optional[Zone]`

**Why `min_size: float` and not the `Instrument`:** `structure.py` imports only `models` today, and keeping it free of the instrument/profile registries is what lets it stay a pure geometry module. Every call site already holds both objects and can multiply. This deviates from the spec's illustrative signature (`..., instrument`) for that reason; the value passed is exactly `instrument.min_fvg * profile.fvg_size_factor`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_h1_fvg_zone.py`:

```python
"""An untouched H1 imbalance as a zone of interest (spec §2.1, D10)."""

from app.services.smc.models import Direction
from app.services.smc.structure import find_h1_fvg_zone
from tests.test_smc.helpers import make_candles


def _gap_up():
    """Closes that leave a bullish H1 gap and never trade back into it.

    make_candles builds each candle around its close, so a jump between
    consecutive closes opens a gap between candle i-2's high and candle
    i's low. Read helpers.py before adjusting these numbers.
    """
    return make_candles(
        [100.0, 101.0, 102.0, 120.0, 121.0, 122.0, 123.0, 124.0],
        step_minutes=60,
    )


class TestFindH1FvgZone:
    def test_finds_an_untouched_bullish_gap(self):
        zone = find_h1_fvg_zone(_gap_up(), Direction.LONG, min_size=1.0)
        assert zone is not None
        assert zone.is_demand is True
        assert zone.kind == "FVG"
        assert zone.bottom < zone.top

    def test_gap_smaller_than_min_size_is_rejected(self):
        zone = find_h1_fvg_zone(_gap_up(), Direction.LONG, min_size=500.0)
        assert zone is None

    def test_no_gap_no_zone(self):
        flat = make_candles([100.0, 100.5, 101.0, 101.5, 102.0], step_minutes=60)
        assert find_h1_fvg_zone(flat, Direction.LONG, min_size=0.01) is None

    def test_wrong_direction_finds_nothing(self):
        assert find_h1_fvg_zone(_gap_up(), Direction.SHORT, min_size=1.0) is None

    def test_a_touched_gap_is_not_fresh(self):
        """D10: any penetration disqualifies it — price already arrived."""
        candles = make_candles(
            [100.0, 101.0, 102.0, 120.0, 121.0, 110.0, 121.0, 122.0],
            step_minutes=60,
        )
        assert find_h1_fvg_zone(candles, Direction.LONG, min_size=1.0) is None

    def test_the_freshest_gap_wins_when_several_are_untouched(self):
        candles = make_candles(
            [100.0, 101.0, 102.0, 120.0, 121.0, 122.0, 140.0, 141.0, 142.0],
            step_minutes=60,
        )
        zone = find_h1_fvg_zone(candles, Direction.LONG, min_size=1.0)
        assert zone is not None
        # the later gap sits higher than the earlier one
        assert zone.bottom > 120.0

    def test_bearish_gap_is_supply(self):
        candles = make_candles(
            [140.0, 139.0, 138.0, 120.0, 119.0, 118.0, 117.0, 116.0],
            step_minutes=60,
        )
        zone = find_h1_fvg_zone(candles, Direction.SHORT, min_size=1.0)
        assert zone is not None and zone.is_demand is False and zone.kind == "FVG"

    def test_zone_carries_the_gap_formation_candle(self):
        candles = _gap_up()
        zone = find_h1_fvg_zone(candles, Direction.LONG, min_size=1.0)
        assert zone is not None
        assert candles[zone.pivot_index].timestamp == zone.timestamp
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_h1_fvg_zone.py -v
```

Expected: FAIL — `ImportError: cannot import name 'find_h1_fvg_zone'`.

- [ ] **Step 3: Add `Zone.kind`**

In `app/services/smc/models.py`, add the field to `Zone` after `invalidated` (it must come after the existing defaulted fields so no call site breaks):

```python
    # What kind of zone this is: "OB" (an order block built from a pivot
    # candle, the historical default) or "FVG" (an untouched H1 imbalance,
    # spec 2026-08-16 §2.1). Messages and charts name it; nothing branches
    # on it for geometry — both kinds are a price band price reacts from.
    kind: str = "OB"
```

- [ ] **Step 4: Implement `find_h1_fvg_zone`**

In `app/services/smc/structure.py`, extend the imports:

```python
from app.services.smc.fvg import find_fvgs, measure_fill
```

and add this function directly after `find_h1_zone`:

```python
def find_h1_fvg_zone(
    candles: List[Candle], direction: Direction, min_size: float
) -> Optional[Zone]:
    """The most recent UNTOUCHED H1 imbalance on the trade's side, as a Zone.

    A zone of interest the owner waits for, alongside the order block
    (spec 2026-08-16 §2.1). Freshness is D10 (owner decision 2026-08-18):
    the gap must have zero penetration — the exact mirror of `find_h1_zone`'s
    `touches == 0`. A partially filled gap is not something the bot is still
    waiting for, because price already arrived; announcing it would fire the
    zone alert late. This is deliberately stricter than Rule 4's
    `fill < 50%`, which judges the M5 entry imbalance price is trading in
    right now.

    `min_size` is `Instrument.min_fvg * StrategyProfile.fvg_size_factor`,
    computed by the caller — this module stays free of the instrument and
    profile registries.

    The returned Zone's `pivot_index`/`timestamp` carry the gap's own
    formation candle, not a fractal pivot, the same way `find_order_block`
    reuses those fields.
    """
    for gap in reversed(find_fvgs(candles, direction, from_index=0)):
        if gap.size < min_size:
            continue
        if measure_fill(candles, gap).fill_pct > 0:
            continue  # D10: price has already traded into it
        return Zone(
            bottom=gap.bottom,
            top=gap.top,
            is_demand=direction == Direction.LONG,
            pivot_index=gap.index,
            timestamp=gap.timestamp,
            kind="FVG",
        )
    return None
```

Note `measure_fill` mutates and returns the same `FVG`; `fill_pct` is `0.0` only when no later candle traded into the gap, which also implies `closed_through` is False — so the single `fill_pct > 0` test covers both clauses of the original spec sentence.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_h1_fvg_zone.py -v
```

Expected: PASS, 8 tests. If a synthetic-candle helper does not produce the gap a test assumes, fix that test's closes until it does — do not weaken `find_h1_fvg_zone`.

- [ ] **Step 6: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: 4 pre-existing failures only, flake8 clean.

- [ ] **Step 7: Commit**

```bash
git add app/services/smc/models.py app/services/smc/structure.py tests/test_smc/test_h1_fvg_zone.py
git commit -m "feat: untouched H1 imbalance as a zone of interest (Zone.kind)"
```

---

### Task 2: `find_zone_of_interest` wired into the engine and the plan

**Files:**
- Modify: `app/services/smc/structure.py` (new function after `find_h1_fvg_zone`)
- Modify: `app/services/smc/engine.py` (Rule 2, and the zone ladder)
- Modify: `app/services/smc/plan.py` (`_scenario`)
- Modify: `app/services/smc/notifier.py` (the `📍 H1 … zone` line of `format_result`)
- Test: `tests/test_smc/test_zone_of_interest.py` (create), `tests/test_smc/test_engine.py` (extend)

**Interfaces:**
- Consumes: `structure.find_h1_zone(candles, direction, max_touches) -> Optional[Zone]` (unchanged), `structure.find_h1_fvg_zone(candles, direction, min_size) -> Optional[Zone]` (Task 1), `Zone.kind` (Task 1).
- Produces: `structure.find_zone_of_interest(candles, direction, min_size, max_touches=0) -> Optional[Zone]` — the order block when one qualifies, otherwise the untouched imbalance, otherwise None.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_zone_of_interest.py`:

```python
"""D4: the order block wins; the imbalance is the fallback (spec §2.1)."""

from datetime import datetime, timezone

import app.services.smc.structure as structure
from app.services.smc.models import Direction, Zone
from tests.test_smc.helpers import H1_PULLBACK_CLOSES, make_candles


def _zone(kind, bottom=100.0, top=101.0):
    return Zone(
        bottom=bottom, top=top, is_demand=True, pivot_index=3,
        timestamp=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc), kind=kind,
    )


class TestFindZoneOfInterest:
    def test_order_block_wins_when_both_qualify(self, monkeypatch):
        monkeypatch.setattr(structure, "find_h1_zone", lambda *a, **k: _zone("OB"))
        monkeypatch.setattr(
            structure, "find_h1_fvg_zone", lambda *a, **k: _zone("FVG", 90.0, 91.0)
        )
        zone = structure.find_zone_of_interest(
            [], Direction.LONG, min_size=1.0, max_touches=0
        )
        assert zone is not None and zone.kind == "OB"

    def test_imbalance_used_when_no_order_block(self, monkeypatch):
        monkeypatch.setattr(structure, "find_h1_zone", lambda *a, **k: None)
        monkeypatch.setattr(
            structure, "find_h1_fvg_zone", lambda *a, **k: _zone("FVG", 90.0, 91.0)
        )
        zone = structure.find_zone_of_interest(
            [], Direction.LONG, min_size=1.0, max_touches=0
        )
        assert zone is not None and zone.kind == "FVG"

    def test_none_when_neither_qualifies(self, monkeypatch):
        monkeypatch.setattr(structure, "find_h1_zone", lambda *a, **k: None)
        monkeypatch.setattr(structure, "find_h1_fvg_zone", lambda *a, **k: None)
        assert structure.find_zone_of_interest(
            [], Direction.LONG, min_size=1.0, max_touches=0
        ) is None

    def test_real_candles_still_find_the_order_block(self):
        """No mocks: the existing H1 pullback fixture must behave as before."""
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        before = structure.find_h1_zone(h1, Direction.LONG, max_touches=0)
        after = structure.find_zone_of_interest(
            h1, Direction.LONG, min_size=1.0, max_touches=0
        )
        assert before is not None
        assert after is not None
        assert (after.bottom, after.top, after.kind) == (
            before.bottom, before.top, "OB",
        )
```

Add to `tests/test_smc/test_engine.py`:

```python
class TestZoneOfInterestInEngine:
    def test_engine_still_approves_the_long_fixture_with_an_ob_zone(self):
        from datetime import datetime, timezone

        from app.services.smc.engine import TripleSyncEngine
        from app.services.smc.models import AnalysisResult, Verdict
        from tests.test_smc.helpers import (
            H1_PULLBACK_CLOSES, H4_UPTREND_CLOSES, m5_long_trigger, make_candles,
        )

        result = AnalysisResult(
            symbol="ETHUSD", verdict=Verdict.SKIP,
            checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
        )
        res = TripleSyncEngine(max_entry_gap_r=99.0).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=result,
        )
        assert res.verdict == Verdict.APPROVED_LIMIT
        assert res.h1_zone is not None and res.h1_zone.kind == "OB"
```

Match the file's existing import style — if `test_engine.py` already imports these at module level, use those imports instead of the local ones above.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_zone_of_interest.py -v
```

Expected: FAIL — `AttributeError: module 'app.services.smc.structure' has no attribute 'find_zone_of_interest'`.

- [ ] **Step 3: Implement `find_zone_of_interest`**

In `app/services/smc/structure.py`, after `find_h1_fvg_zone`:

```python
def find_zone_of_interest(
    candles: List[Candle],
    direction: Direction,
    min_size: float,
    max_touches: int = 0,
) -> Optional[Zone]:
    """The H1 zone the owner waits for: the order block if one qualifies,
    otherwise the untouched imbalance (owner decision D4, 2026-08-16).

    The order block wins because it is the footprint of a filled order —
    price reacted there once and the level is proven. An imbalance is a
    gap nobody has traded back into yet, which is a weaker claim; it earns
    the slot only when no valid order block exists.

    Both are the same object downstream: a price band the setup enters
    from. `Zone.kind` records which one this is so messages and charts can
    name it.
    """
    block = find_h1_zone(candles, direction, max_touches=max_touches)
    if block is not None:
        return block
    return find_h1_fvg_zone(candles, direction, min_size)
```

- [ ] **Step 4: Switch the engine's Rule 2**

Read `TripleSyncEngine.__init__` first and find the attribute that already holds the per-instrument minimum FVG (it is passed in by `_build_engine` in `smc_watcher.py`), and how the engine already scales it by `profile.fvg_size_factor` for Rule 4. **Reuse that expression** — if it appears inline in Rule 4, extract it to one place (a property or a local computed once) rather than writing the multiplication a second time.

Add `find_zone_of_interest` to the `structure` import list, then replace the Rule 2 zone lookup:

```python
        # Rule 2 — H1 zone of interest: order block first, untouched
        # imbalance as the fallback (owner decision D4, spec §2.1). The
        # minimum gap size is the per-instrument floor scaled by the
        # profile, exactly as Rule 4 scales the M5 imbalance.
        zone = find_zone_of_interest(
            h1,
            direction,
            min_size=<the existing scaled-min-fvg expression>,
            max_touches=self.profile.max_zone_touches,
        )
```

Widen the no-zone WATCH reason to mention both kinds, keeping the existing sentence shape:

```python
            result.reasons.append(
                f"H4 is {'bullish' if direction == Direction.LONG else 'bearish'}, "
                "but H1 has no valid untested "
                f"{'Demand' if direction == Direction.LONG else 'Supply'} zone "
                "(no order block, no untouched imbalance)"
            )
```

- [ ] **Step 5: Put the runner-up zone in the ladder**

Still in `engine.py`, where `zones_ahead = zone_ladder(h1, direction, entry, exclude=zone)` is built: when the chosen zone is an order block, the untouched imbalance — if one exists and sits further out than the entry on the trade's own side — is a genuine deeper entry the owner wants to see (spec §2.1, "the runner-up zone joins the existing zone ladder"). Prepend it when it qualifies:

```python
        zones_ahead = zone_ladder(h1, direction, entry, exclude=zone)
        if zone.kind == "OB":
            runner_up = find_h1_fvg_zone(h1, direction, <scaled min fvg>)
            if runner_up is not None and _is_deeper_than(runner_up, direction, entry):
                zones_ahead = [runner_up] + zones_ahead
```

Define `_is_deeper_than(zone, direction, entry)` as a module-level helper in `engine.py`. Read `zone_ladder` in `structure.py` first and match its "further out on the trade's own side" convention exactly (it compares `zone.bottom > beyond` for a SHORT and `zone.top < beyond` for a LONG) rather than inventing a second one.

- [ ] **Step 6: Switch the plan**

In `app/services/smc/plan.py`, `_scenario` currently calls `find_h1_zone(h1, direction, max_touches=profile.max_zone_touches)`. Switch it to `find_zone_of_interest` with `min_size=instrument.min_fvg * profile.fvg_size_factor` (the function already receives both `instrument` and `profile`). Update the import.

The plan's sentences must name the kind. `zone_label` currently reads:

```python
    zone_label = f"H1 {kind} zone {zone.bottom:.{d}f}-{zone.top:.{d}f}"
```

Change it to:

```python
    zone_label = (
        f"H1 {kind} zone ({zone.kind}) {zone.bottom:.{d}f}-{zone.top:.{d}f}"
    )
```

`_zone_note` (the "wait for a fresh H1 zone to form" sentence) is quoted verbatim against the engine's own wording — widen it exactly the same way you widened the engine's reason in Step 4, and read both afterwards to confirm they still match word for word.

- [ ] **Step 7: Name the kind in the setup alert**

In `app/services/smc/notifier.py`'s `format_result`:

```python
    if result.h1_zone:
        kind = "Demand" if result.h1_zone.is_demand else "Supply"
        lines.append(
            f"📍 H1 {kind} zone ({result.h1_zone.kind})  "
            f"{result.h1_zone.bottom:.{d}f} – {result.h1_zone.top:.{d}f}"
        )
```

The surrounding block is space-padded so the price column lines up; adjust this line's padding so it still matches its neighbours after the insertion.

- [ ] **Step 8: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_zone_of_interest.py tests/test_smc/test_engine.py tests/test_smc/test_plan.py -v
```

Expected: PASS. Existing engine and plan tests must pass **unchanged** — the order block still wins on every existing fixture, so no existing expectation should need editing. If one does, stop and report it: it means the fallback is firing where an order block used to be found, which this delivery did not intend.

- [ ] **Step 9: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: 4 pre-existing failures only, flake8 clean.

- [ ] **Step 10: Commit**

```bash
git add app/services/smc/structure.py app/services/smc/engine.py app/services/smc/plan.py app/services/smc/notifier.py tests/test_smc/test_zone_of_interest.py tests/test_smc/test_engine.py
git commit -m "feat: zone of interest = order block first, untouched imbalance as fallback"
```

---

### Task 3: The M5 detail line on the zone alert

**Files:**
- Modify: `app/services/smc/structure.py` (`find_order_block` gains an optional-guard mode; new `m5_marks`)
- Modify: `app/services/smc/notifier.py` (`format_zone_alert`)
- Modify: `smc_watcher.py` (`_maybe_plan_zone_alert`)
- Test: `tests/test_smc/test_m5_marks.py` (create)

**Interfaces:**
- Consumes: `structure.zone_touch_span(candles, zone) -> Optional[Tuple[int, int]]` (exists — read its signature; it takes a `Zone`), `structure.find_order_block(...)` (exists, extended here), `fvg.find_fvgs`/`measure_fill` (exist), `Zone` (Task 1).
- Produces:
  - `structure.m5_marks(m5, direction, zone_bottom, zone_top, min_size) -> Tuple[Optional[Zone], Optional[FVG]]`
  - `notifier.format_zone_alert(pair, scenario, decimals, marks=None)`

**Why this is one line and not a second alert:** owner decision D5. The zone alert already fired for this touch; the M5 detail is what the owner would actually buy from inside that zone, so it rides along in the same message. Nothing here may send a message of its own.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_m5_marks.py`:

```python
"""The 5m order block / imbalance detail inside a touched zone (D5, §2.2)."""

from datetime import datetime, timezone

from app.services.smc.models import Direction, FVG, Zone
from app.services.smc.notifier import format_zone_alert
from app.services.smc.plan import PlanScenario
from app.services.smc.structure import m5_marks
from tests.test_smc.helpers import m5_long_trigger

TS = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


def _band(m5):
    """A band around the pullback low the fixture actually visits."""
    bottom = min(c.low for c in m5[-8:])
    top = bottom + (max(c.high for c in m5) - bottom) * 0.5
    return bottom, top


class TestM5Marks:
    def test_finds_something_before_any_choch_exists(self):
        """§2.2: the marks must be visible while the CHoCH is still pending —
        this is the zone-alert moment, not the setup moment."""
        m5 = m5_long_trigger()[:16]  # in the zone, no CHoCH yet
        bottom, top = _band(m5)
        block, gap = m5_marks(m5, Direction.LONG, bottom, top, min_size=0.01)
        assert block is not None or gap is not None

    def test_returns_none_none_when_price_never_entered_the_band(self):
        m5 = m5_long_trigger()
        far = max(c.high for c in m5) + 100.0
        assert m5_marks(
            m5, Direction.LONG, far, far + 10.0, min_size=0.01
        ) == (None, None)

    def test_returns_none_none_on_empty_candles(self):
        assert m5_marks([], Direction.LONG, 1.0, 2.0, min_size=0.01) == (None, None)

    def test_a_found_block_is_a_demand_zone_for_a_long(self):
        m5 = m5_long_trigger()[:16]
        bottom, top = _band(m5)
        block, _gap = m5_marks(m5, Direction.LONG, bottom, top, min_size=0.01)
        if block is not None:
            assert block.is_demand is True and block.bottom < block.top

    def test_an_absurd_min_size_rejects_the_gap(self):
        m5 = m5_long_trigger()[:16]
        bottom, top = _band(m5)
        _block, gap = m5_marks(m5, Direction.LONG, bottom, top, min_size=1e9)
        assert gap is None


def _scenario():
    return PlanScenario(
        direction=Direction.LONG, entry=3138.0, stop_loss=3129.0,
        take_profit=3158.0, rr=2.1, zone_bottom=3131.0, zone_top=3138.0,
        speculative=False,
    )


class TestZoneAlertMarksLine:
    def test_no_marks_no_extra_line(self):
        assert "🔎" not in format_zone_alert("ETHUSD", _scenario(), 2)

    def test_empty_marks_no_extra_line(self):
        text = format_zone_alert("ETHUSD", _scenario(), 2, marks=(None, None))
        assert "🔎" not in text

    def test_block_and_gap_both_render(self):
        block = Zone(bottom=3131.0, top=3133.0, is_demand=True,
                     pivot_index=1, timestamp=TS)
        gap = FVG(index=2, bottom=3134.0, top=3136.0, is_bullish=True,
                  timestamp=TS)
        text = format_zone_alert("ETHUSD", _scenario(), 2, marks=(block, gap))
        assert "🔎" in text
        assert "5m OB 3131.00–3133.00" in text
        assert "5m FVG 3134.00–3136.00" in text

    def test_only_one_mark_renders_alone(self):
        block = Zone(bottom=3131.0, top=3133.0, is_demand=True,
                     pivot_index=1, timestamp=TS)
        text = format_zone_alert("ETHUSD", _scenario(), 2, marks=(block, None))
        assert "5m OB" in text and "5m FVG" not in text

    def test_the_base_message_is_unchanged_by_the_marks(self):
        base = format_zone_alert("ETHUSD", _scenario(), 2)
        block = Zone(bottom=3131.0, top=3133.0, is_demand=True,
                     pivot_index=1, timestamp=TS)
        with_marks = format_zone_alert(
            "ETHUSD", _scenario(), 2, marks=(block, None)
        )
        assert with_marks.startswith(base)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_m5_marks.py -v
```

Expected: FAIL — `ImportError: cannot import name 'm5_marks'`.

- [ ] **Step 3: Make `find_order_block`'s deeper-entry guard optional**

In `app/services/smc/structure.py`, change `find_order_block`'s `entry` and `stop_loss` parameters to `Optional[float] = None` and skip the guard when either is None:

```python
        if is_long:
            zone = Zone(bottom=c.low, top=c.body_high, is_demand=True,
                        pivot_index=i, timestamp=c.timestamp)
            ob_entry = zone.top
            deeper = entry is None or stop_loss is None or (
                stop_loss < ob_entry < entry
            )
        else:
            zone = Zone(bottom=c.body_low, top=c.high, is_demand=False,
                        pivot_index=i, timestamp=c.timestamp)
            ob_entry = zone.bottom
            deeper = entry is None or stop_loss is None or (
                entry < ob_entry < stop_loss
            )
        return zone if deeper else None
```

Extend the docstring: with no `entry`/`stop_loss` the function is a plain "last opposing candle in this window" finder, used for marking a zone the owner is watching before any setup exists (§2.2). **The engine's call is unchanged** — it still passes both, so the "strictly deeper than entry, strictly inside the stop" guard still applies there. Say that explicitly in the docstring.

- [ ] **Step 4: Implement `m5_marks`**

In `app/services/smc/structure.py`, after `find_order_block`:

```python
def m5_marks(
    m5: List[Candle],
    direction: Direction,
    zone_bottom: float,
    zone_top: float,
    min_size: float,
) -> Tuple[Optional[Zone], Optional[FVG]]:
    """The M5 order block and imbalance inside the current excursion into
    `[zone_bottom, zone_top]` — what the owner would actually buy from once
    price is in the zone (owner decision D5, spec §2.2).

    Marking only: no verdict, no suppression, no message of its own. Both
    halves are optional and independent — an excursion often shows one and
    not the other, and `(None, None)` simply means the zone alert prints no
    detail line.

    The window is `zone_touch_span`'s latest excursion, the same window the
    engine searches for the CHoCH and the FVG, so the marks name the same
    stretch of price action a setup would form in. Unlike the engine's use,
    this runs BEFORE any CHoCH exists — that is the point: the alert fires
    when price arrives, and the owner wants the levels then.

    Rule 4's session-scope and fill checks are deliberately NOT applied to
    the gap here. This is a label on a zone being watched, not a trade
    trigger; the M5 setup path still runs the full Rule 4 validation when
    the setup actually forms.
    """
```

Return `(None, None)` when `m5` is empty. Build the band as a `Zone` to hand to `zone_touch_span` (it takes a `Zone`; give it `is_demand=direction == Direction.LONG`, `pivot_index=0`, `timestamp=m5[0].timestamp`), and return `(None, None)` when the span is None. Inside the span `(start, end)`:
- order block: `find_order_block(m5, direction, before_index=end + 1, since_index=start, entry=None, stop_loss=None)`;
- imbalance: the most recent gap from `find_fvgs(m5, direction, from_index=start)` that is at least `min_size` and whose `measure_fill(m5, gap).closed_through` is False.

Import `FVG` and `Tuple` if `structure.py` does not already have them.

- [ ] **Step 5: Render the line**

In `app/services/smc/notifier.py`, `format_zone_alert` gains a keyword argument and one optional line:

```python
def format_zone_alert(pair, scenario, decimals: int, marks=None) -> str:
```

Keep the existing message text byte-identical — only change how it is assembled. If it is still a single f-string concatenation, restructure it into a `lines` list first, confirm the tests still pass, then append:

```python
    block, gap = marks if marks else (None, None)
    if block or gap:
        parts = []
        if block:
            parts.append(f"5m OB {block.bottom:.{d}f}–{block.top:.{d}f}")
        if gap:
            parts.append(f"5m FVG {gap.bottom:.{d}f}–{gap.top:.{d}f}")
        lines.append("🔎 " + " · ".join(parts))
```

Every value interpolated here is a float this function formatted itself, so no `escape_html` is needed on them; `pair` keeps the `escape_html` it already has.

- [ ] **Step 6: Compute the marks at the alert site**

In `smc_watcher.py`'s `_maybe_plan_zone_alert`, after the scenario is chosen and before the send:

```python
        marks = None
        try:
            instrument = get_instrument(key)
            profile = get_profile(
                self.state.pair_profile.get(key, settings.smc.default_profile)
            )
            marks = m5_marks(
                result.m5_candles,
                scenario.direction,
                scenario.zone_bottom,
                scenario.zone_top,
                instrument.min_fvg * profile.fvg_size_factor,
            )
        except Exception as e:  # marking must never cost the owner an alert
            logger.warning("M5 marks failed", pair=key, error=str(e))
```

then pass `marks=marks` to `format_zone_alert`.

The try/except is deliberate and mirrors the chart rule in `CLAUDE.md`: a decorative detail must never block an alert. Match how this file already imports `get_profile`, `get_instrument` and `settings` rather than adding a new import style.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_m5_marks.py tests/test_smc/test_zone_dedup.py tests/test_smc/test_zone_mute_button.py tests/test_smc/test_autoplan.py -v
```

Expected: PASS. Delivery 1's dedup and mute tests must still pass untouched.

- [ ] **Step 8: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: 4 pre-existing failures only, flake8 clean.

- [ ] **Step 9: Commit**

```bash
git add app/services/smc/structure.py app/services/smc/notifier.py smc_watcher.py tests/test_smc/test_m5_marks.py
git commit -m "feat: name the 5m order block and imbalance inside a touched zone"
```

---

### Task 4: H4/H1 trend agreement label and the ⭐ denial

**Files:**
- Modify: `app/services/smc/models.py` (`AnalysisResult.h1_trend`)
- Modify: `app/services/smc/engine.py` (record the H1 trend on every path; `trends_disagree`; pass it to `classify`)
- Modify: `app/services/smc/sniper.py` (`classify` gains the check)
- Modify: `app/services/smc/notifier.py` (the header of `format_result`)
- Test: `tests/test_smc/test_trend_agreement.py` (create)

**Interfaces:**
- Consumes: `structure.detect_trend(candles) -> Trend` (exists); `sniper.classify(room, sweep, pd, stale, min_room_r=MIN_ROOM_R) -> TierVerdict` (exists, extended here).
- Produces:
  - `AnalysisResult.h1_trend: Optional[Trend]`
  - `engine.trends_disagree(h4_trend, h1_trend) -> bool`
  - `sniper.classify(room, sweep, pd, stale, trend_disagrees: bool = False, min_room_r=MIN_ROOM_R)` — `"trend"` joins `missed` when `trend_disagrees` is True.

**The definition of disagreement, exactly:** both timeframes trend AND they point opposite ways. An H4 FLAT is **not** a disagreement — that is the H1-fallback case the owner approved on 2026-08-06, and it must keep earning the star exactly as it does today. An H1 FLAT under a trending H4 is likewise not a disagreement.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_trend_agreement.py`:

```python
"""D6: H4/H1 disagreement is labelled and denies the ⭐, never suppresses."""

from app.services.smc import sniper
from app.services.smc.engine import trends_disagree
from app.services.smc.models import Trend


class TestClassifyTrendGate:
    def _clean(self, **over):
        kwargs = dict(room=2.0, sweep="PDH", pd="ok", stale=False)
        kwargs.update(over)
        return sniper.classify(**kwargs)

    def test_star_when_trends_agree(self):
        assert self._clean(trend_disagrees=False).star is True

    def test_disagreement_denies_the_star(self):
        verdict = self._clean(trend_disagrees=True)
        assert verdict.star is False
        assert "trend" in verdict.missed

    def test_default_is_agreement(self):
        """Existing callers that pass four arguments keep today's behaviour."""
        assert sniper.classify(2.0, "PDH", "ok", False).star is True

    def test_disagreement_is_listed_alongside_other_misses(self):
        verdict = sniper.classify(
            room=0.1, sweep=None, pd="ok", stale=False, trend_disagrees=True
        )
        assert set(verdict.missed) >= {"room", "sweep", "trend"}


class TestTrendDisagreementDefinition:
    def test_h4_flat_is_not_disagreement(self):
        assert trends_disagree(Trend.FLAT, Trend.DOWN) is False

    def test_h1_flat_is_not_disagreement(self):
        assert trends_disagree(Trend.UP, Trend.FLAT) is False

    def test_opposite_trends_disagree(self):
        assert trends_disagree(Trend.UP, Trend.DOWN) is True
        assert trends_disagree(Trend.DOWN, Trend.UP) is True

    def test_same_trend_agrees(self):
        assert trends_disagree(Trend.UP, Trend.UP) is False

    def test_none_h1_is_not_disagreement(self):
        assert trends_disagree(Trend.UP, None) is False
```

Add one more test proving a disagreeing setup is still **sent** — detector mode. Put it next to the existing alert-routing tests in `tests/test_smc/test_multipair.py` or `tests/test_smc/test_notify_level.py`, whichever already has a working `_send_alert` harness; read both first and follow the one that fits. It must assert the message still goes out and only the ⭐ header is absent.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_trend_agreement.py -v
```

Expected: FAIL — `ImportError: cannot import name 'trends_disagree'`.

- [ ] **Step 3: Record the H1 trend**

In `models.py`, add to `AnalysisResult` next to `h4_trend`:

```python
    h1_trend: Optional[Trend] = None
```

In `engine.py`'s Rule 1, compute the H1 trend **once, unconditionally**, right after the H4 trend — today `detect_trend(h1)` runs only inside the H4-FLAT branch:

```python
        result.h4_trend = detect_trend(h4)
        result.h1_trend = detect_trend(h1)
```

Then reuse `result.h1_trend` in that FLAT branch instead of calling `detect_trend(h1)` a second time. The branch's behaviour must stay byte-identical — only the source of the value changes.

Add the module-level helper:

```python
def trends_disagree(h4_trend, h1_trend) -> bool:
    """True only when BOTH timeframes trend and they point opposite ways.

    An H4 FLAT is not a disagreement — it is the H1-fallback case the owner
    approved on 2026-08-06, and it must keep earning the star exactly as it
    does today. An H1 FLAT under a trending H4 is likewise no conflict:
    nothing is arguing.
    """
    trending = (Trend.UP, Trend.DOWN)
    return (
        h4_trend in trending and h1_trend in trending and h4_trend != h1_trend
    )
```

- [ ] **Step 4: Feed it to the tier**

In `engine.py`, where `tier = sniper.classify(room, sweep, pd, stale)` is called:

```python
        tier = sniper.classify(
            room, sweep, pd, stale,
            trend_disagrees=trends_disagree(result.h4_trend, result.h1_trend),
        )
```

In `sniper.py`, extend `classify`:

```python
def classify(
    room: Optional[float],
    sweep: Optional[str],
    pd: Optional[str],
    stale: bool,
    trend_disagrees: bool = False,
    min_room_r: float = MIN_ROOM_R,
) -> TierVerdict:
    """Star iff room is unmeasurable-or-wide-enough, a pool was swept, pd is
    ok-or-unmeasurable, the setup is not stale, and H4/H1 do not point
    opposite ways (owner decision D6, 2026-08-16).

    `trend_disagrees` defaults to False so a caller that does not measure it
    is treated as "nothing is arguing" rather than silently losing the star.
    Detector mode is unchanged: a disagreeing setup is still announced, just
    not as a ⭐.
    """
    checks = (
        ("room", room is None or room >= min_room_r),
        ("sweep", sweep is not None),
        ("pd", pd in ("ok", None)),
        ("stale", not stale),
        ("trend", not trend_disagrees),
    )
```

`trend_disagrees` goes **before** `min_room_r` so the existing four-positional calls keep working; confirm no caller passes `min_room_r` positionally before you move it.

Update the module docstring's enumerated star conditions to include the trend gate, in the same voice as the existing entries.

- [ ] **Step 5: Label it in the alert header**

In `notifier.py`'s `format_result`, add a line under the existing header and **before** the ⭐ line, so the tier stays visually last:

```python
    if result.h4_trend is not None and result.h1_trend is not None:
        agree = f"H4 {result.h4_trend.value} · H1 {result.h1_trend.value}"
        if trends_disagree(result.h4_trend, result.h1_trend):
            agree += " ⚠️ counter-hourly"
        lines.append(agree)
```

Import `trends_disagree` from `engine`. **Check the import graph first:** if `engine` imports `notifier` anywhere, do not force it — move `trends_disagree` into `structure.py` next to `detect_trend` and import it from there in both places. State which you did in your report.

Use `Trend`'s own `.value` for the words so the labels cannot drift from the enum.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_trend_agreement.py tests/test_smc/test_sniper.py -v
```

Expected: PASS, with every existing sniper test unchanged.

- [ ] **Step 7: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: 4 pre-existing failures only, flake8 clean.

- [ ] **Step 8: Commit**

```bash
git add app/services/smc/models.py app/services/smc/engine.py app/services/smc/sniper.py app/services/smc/notifier.py tests/test_smc/test_trend_agreement.py
git commit -m "feat: label H4/H1 trend disagreement and deny it the star"
```

---

### Task 5: Charts name the zone kind

**Files:**
- Modify: `app/services/smc/plan.py` (`PlanScenario.kind`)
- Modify: `app/services/smc/chart.py` (`render_plan_chart`; audit `render_setup_chart`)
- Test: `tests/test_smc/test_visuals.py` (extend — do NOT touch `TestPrettyStats`, a known pre-existing failure)

**Interfaces:**
- Consumes: `Zone.kind` (Task 1), `AnalysisResult.h1_zone` (exists).
- Produces: `PlanScenario.kind: str = "OB"`. Both renderers keep their signatures.

- [ ] **Step 1: Write the failing tests**

Read `tests/test_smc/test_visuals.py` first and match how it builds results and asserts on PNG bytes. Then add a class in that established style:

```python
class TestZoneKindOnCharts:
    def test_setup_chart_renders_with_an_fvg_zone(self):
        """A zone of kind FVG renders exactly like an OB one — the kind is a
        label, not a geometry change."""

    def test_plan_chart_renders_with_both_zone_kinds(self):
        """Two scenarios, one OB and one FVG, both draw."""
```

Fill both in with real assertions in the file's established style — a non-empty PNG whose length is plausible, and the same structural assertions the neighbouring tests make. Do not write a test that asserts nothing.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_visuals.py -k ZoneKind -v
```

Expected: FAIL.

- [ ] **Step 3: Carry the kind onto the plan scenario**

`PlanScenario` has no kind today. Add `kind: str = "OB"` to the dataclass in `plan.py` and set it from `zone.kind` where `_scenario` constructs the `PlanScenario(...)`.

Then in `render_plan_chart`, add the kind to the entry label already drawn per scenario:

```python
        _level(ax, s.entry, zone_color, f"{tag} Entry {s.entry:.{d}f} ({s.kind})", x_right, ylim)
```

**Do not** add `kind` to `planbook.plan_fingerprint`: it hashes `(direction, zone_bottom, zone_top, speculative)`, and a zone that changes kind while keeping identical bounds is the same trading idea at the same price — adding it would trigger a summary edit for a cosmetic change. Note in your report that you considered it.

- [ ] **Step 4: Audit the setup chart**

`render_setup_chart` already draws the H1 zone band, the FVG box and the order block. Read the whole function. If that already satisfies §2.4's "the 5m OB and 5m FVG boxes", change nothing and say so in your report — do not add a duplicate rectangle. If either is genuinely missing, add it in the style of the existing boxes.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_visuals.py -v
```

Expected: PASS except the known pre-existing `TestPrettyStats::test_stats_contains_bars_and_sparkline`.

- [ ] **Step 6: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: 4 pre-existing failures only, flake8 clean.

- [ ] **Step 7: Commit**

```bash
git add app/services/smc/plan.py app/services/smc/chart.py tests/test_smc/test_visuals.py
git commit -m "feat: charts name the zone kind"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none — documentation only. **Do not open a pull request in this task**; the controller does that after the whole-branch review.

- [ ] **Step 1: Update the architecture tree**

The `structure.py` entry lists fractal pivots, H4 trend, H1 zones, `zone_ladder`, M5 CHoCH, `sweep_extreme` and `find_order_block`. Add `find_h1_fvg_zone` / `find_zone_of_interest` (order block first, untouched imbalance as the fallback) and `m5_marks`. Keep the tree's alignment and its `│` continuation bars exactly as the surrounding entries use them.

The `sniper.py` entry lists the star conditions — add the H4/H1 trend gate.

- [ ] **Step 2: Update the strategy summary**

The file opens by describing the rule chain as "H1 zone → M5 CHoCH + FVG". Widen "H1 zone" to say the zone of interest is an H1 order block or, when none qualifies, an untouched H1 imbalance (owner decision D4). Keep the sentence's shape and length — this is a précis, not a spec.

- [ ] **Step 3: Add a conventions bullet**

One bullet in the Conventions section, in the voice of its neighbours, covering: `Zone.kind` (`"OB"`/`"FVG"`); D4's precedence and why (an order block is a filled order's footprint, an imbalance only an untraded gap); D10's freshness rule and why it is stricter than Rule 4's `fill < 50%`; and that `m5_marks` is label-only — no verdict, no suppression, no message of its own.

Extend the existing sniper bullet with the trend gate: a completed setup earns ⭐ only when H4 and H1 do not point opposite ways, and a disagreeing setup is still sent (detector mode).

- [ ] **Step 4: Verify the docs match the code**

Read the functions you documented — `find_zone_of_interest`, `find_h1_fvg_zone`, `m5_marks`, `sniper.classify`, `trends_disagree` — and confirm every claim. If a doc and the code disagree, report it rather than papering over it.

- [ ] **Step 5: Run the full suite and lint**

```bash
pytest tests/ -v && flake8 app/ tests/ smc_watcher.py
```

Record the exact summary line for the PR body.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: zone of interest (OB or untouched FVG), m5 marks, trend gate"
```

---

## Self-Review

**Spec coverage.** §2.1 → Tasks 1 and 2 (`Zone.kind`, `find_h1_fvg_zone`, `find_zone_of_interest`, engine Rule 2, `plan._scenario`, the runner-up in the ladder). §2.2 → Task 3 (`find_order_block`'s optional guard, `m5_marks`, the 🔎 line, the watcher call site). §2.3 → Task 4 (`AnalysisResult.h1_trend`, `trends_disagree`, `classify`'s trend gate, the header label). §2.4 → Task 5. §2.5 → the test list in each task: H1 FVG detection and freshness (Task 1), OB-wins-over-FVG (Task 2), FVG used when no OB (Task 2), M5 OB found before a CHoCH exists (Task 3), the engine's deeper-entry guard unchanged (Task 3 Step 3 keeps the engine call passing both arguments; `test_engine.py`'s existing order-block tests are the regression), disagreement denies ⭐ but still sends (Task 4), chart smoke tests (Task 5). §2.6 → the file lists, plus `CLAUDE.md` in Task 6.

**Type consistency.** `min_size: float` is the third positional parameter of `find_h1_fvg_zone` and the keyword `min_size` of `find_zone_of_interest` and `m5_marks`; its value is always `instrument.min_fvg * profile.fvg_size_factor`, computed at the call site. `Zone.kind` is a plain `str`, `"OB"` or `"FVG"`, defaulted so no existing `Zone(...)` construction breaks. `PlanScenario.kind` mirrors it with the same default. `m5_marks` returns `Tuple[Optional[Zone], Optional[FVG]]` in that order, and `format_zone_alert`'s `marks` parameter takes exactly that tuple. `trends_disagree(h4_trend, h1_trend)` takes two `Optional[Trend]` and returns `bool`; `classify`'s new parameter is the keyword `trend_disagrees: bool = False`, inserted before `min_room_r` — every existing call passes the first four positionally, so the default keeps them working.

**Deliberate deviations from the spec text**, recorded here so the reviewer judges them rather than discovering them: (1) `find_h1_fvg_zone` takes `min_size: float` instead of the spec's illustrative `instrument`, to keep `structure.py` free of the instrument and profile registries; (2) `m5_marks` lives in `structure.py` rather than a new module, because the spec left the location open and the function is two-thirds structural.

**Out of scope:** Delivery 3 (range detection, boundary alerts, the black dashed lines) — its own plan. The four pre-existing test failures — a separate task, already flagged to the owner.
