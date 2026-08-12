# Sniper Redesign — Phase 1: Replay Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate the sniper redesign (OB entries, hybrid 2R+runner exit, ⭐ tier = room + sweep + P/D) on the cached year of candles for ETHUSD, USDJPY, USDCAD — producing a report the owner reads before any bot code changes.

**Architecture:** Standalone analysis scripts in `C:\temp\926_bot_data\scripts\` (prefix `sn_`), importing the repo's real rule code from `C:\Git\926_bot` unmodified, following the established `year_fx_run.py` / `year_run.py` harness pattern (PIT slicing, chunked runs, post-hoc analysis). New rule logic (sweep detector, P/D filter, hybrid exit) lives in small pure modules with pytest tests on synthetic candles. Nothing in the repository is modified except this plan/spec and the final report pointer.

**Tech Stack:** Python 3 (repo venv), pytest, pickle candle caches, `app.services.smc.*` imported read-only.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-12-sniper-redesign-design.md` (owner-approved 2026-08-12).
- **Candle pickles are irreplaceable** (TwelveData key rotated). Open read-only; never overwrite any file in `C:\temp\926_bot_data\candles\`.
- **The repository is NOT modified in Phase 1.** No edits to `app/` or `scripts/` in the repo. Counterfactual behaviour is produced in the harness only.
- **Data:** `C:\temp\926_bot_data\candles\eth_year.pkl` (ETHUSD), `fx_USDJPY.pkl`, `fx_USDCAD.pkl` — each a dict `{"m5": [...], "h1": [...], "h4": [...]}` of `app.services.smc.models.Candle`.
- **PIT discipline (identical to prior year runs):** H4/H1 sliced by candle **close** time (`open + tf <= cutoff`); slices capped at production depth **H4 300 / H1 400 / M5 400**; `PIT_FLOOR = 15` (from `scripts.funnel`); 60-candle M5 warm-up; off-session closes skipped; forex `require_weekday=True`, crypto every day.
- **Profile:** `CONSERVATIVE` only. Engine: `min_rr=1.0`, `max_entry_gap_r=0.75`, per-instrument params from `get_instrument(pair)`.
- **Approval semantics (owner decision 2026-08-12, superseding the original sanity targets as-written):** the current engine is detector-mode (commit `0f463bf`, 2026-08-06) — Rule 5.1/Rule 7 warn instead of SKIP, so raw approved counts exceed the published pre-detector baselines by construction. The run records the detector-mode superset with each approval's `warnings` list; **harness sanity** = the warnings-empty subset must reproduce the published numbers exactly (59d ETH capped: 6 approved closes; year ETH: 420; USDJPY: 80); **study population** = approvals with no "run past" (Rule 5.1) warning — the RR/no-liquidity warnings stay in-population because the redesign replaces Rule 7. Post-hoc gate equivalence was verified exact in both prior year reports.
- **Outcome semantics are conservative:** fill = wick touch; TP and SL in one candle = SL; TP1 and SL in one candle = SL; runner-TP and BE in one candle = BE; pending expires at `session_end_utc` (Rule 10); `created_at` = close of the signal candle (signal candle can neither fill nor resolve itself).
- **Headline counting = idea counting:** one alert per `(direction, round(entry_used, 8), Prague date)` for the whole year, taken at its first approved close. Rising-edge and fingerprint reported as cross-checks only.
- **Out-of-sample split:** train = closes before **2026-02-05 00:00 UTC**, test = closes at/after. The runner multiple (3.0/3.5/4.0) and any threshold decision are picked on train and confirmed on test.
- Python for all scripts starts with the `sys.path` preamble used by `year_fx_run.py` (`REPO = r"C:\Git\926_bot"`, scratch dir first).
- Run tests with the repo's venv python from `C:\temp\926_bot_data\scripts\`: `python -m pytest sn_test_rules.py -v` etc.

---

### Task 1: `sn_rules.py` — sweep condition + premium/discount

**Files:**
- Create: `C:\temp\926_bot_data\scripts\sn_rules.py`
- Create: `C:\temp\926_bot_data\scripts\sn_test_rules.py`

**Interfaces:**
- Consumes: `app.services.smc.models.Candle`, `Direction`; `app.services.smc.structure.find_pivots`; `app.services.smc.liquidity.find_liquidity`.
- Produces (used by Task 3):
  - `sweep_label(m5, h1_pit, direction, touch_idx, choch_idx, tolerance, ts_utc) -> Optional[str]` — name of the pool the touch-excursion swept (`"PDH"`, `"PDL"`, `"AsiaH"`, `"AsiaL"`, `"EQH(n)"`, `"EQL(n)"`, `"swingH"`, `"swingL"`) or `None`.
  - `dealing_range(h1_pit) -> Optional[Tuple[float, float]]` — `(low, high)` of the last confirmed H1 swing low/high pivots, `None` if fewer than one of each or `low >= high`.
  - `pd_state(direction, entry, rng) -> Optional[str]` — `"ok"` if LONG in lower half / SHORT in upper half of `rng`, else `"bad"`; `None` when `rng is None`.

- [ ] **Step 1: Write the failing tests**

```python
# sn_test_rules.py
import os
import sys
from datetime import datetime, timedelta, timezone

SCRATCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRATCH)
sys.path.insert(0, r"C:\Git\926_bot")

from app.services.smc.models import Candle, Direction  # noqa: E402
from sn_rules import dealing_range, pd_state, sweep_label  # noqa: E402

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
        m5=m5, h1_pit=[], direction=Direction.LONG,
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
        m5=m5, h1_pit=[], direction=Direction.LONG,
        touch_idx=len(prev) + 1, choch_idx=len(prev) + 4,
        tolerance=0.05, ts_utc=base + timedelta(minutes=20),
    )
    assert label is None
```

- [ ] **Step 2: Run tests, verify they fail with `ModuleNotFoundError: sn_rules`**

Run: `python -m pytest sn_test_rules.py -v` (cwd `C:\temp\926_bot_data\scripts`)

- [ ] **Step 3: Implement `sn_rules.py`**

```python
"""Sniper redesign rule primitives: liquidity-sweep label and premium/discount.

Pure functions over Candle lists. No repo modification; imports the repo's
pivot/liquidity code read-only.
"""

import os
import sys
from datetime import timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

SCRATCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRATCH)
sys.path.insert(0, r"C:\Git\926_bot")

from app.services.smc.liquidity import find_liquidity  # noqa: E402
from app.services.smc.models import Candle, Direction  # noqa: E402
from app.services.smc.structure import find_pivots  # noqa: E402

PRAGUE = ZoneInfo("Europe/Prague")


def _prague_date(ts_utc):
    return ts_utc.astimezone(PRAGUE).date()


def _day_extremes(m5: List[Candle], day) -> Optional[Tuple[float, float]]:
    """(low, high) over the Prague calendar day `day`, None if no candles."""
    rows = [c for c in m5 if _prague_date(c.timestamp) == day]
    if not rows:
        return None
    return min(c.low for c in rows), max(c.high for c in rows)


def _asia_extremes(m5: List[Candle], day) -> Optional[Tuple[float, float]]:
    """(low, high) of 00:00-08:00 Prague on `day` (pre-session accumulation)."""
    rows = [
        c for c in m5
        if _prague_date(c.timestamp) == day
        and c.timestamp.astimezone(PRAGUE).hour < 8
    ]
    if not rows:
        return None
    return min(c.low for c in rows), max(c.high for c in rows)


def sweep_label(
    m5: List[Candle],
    h1_pit: List[Candle],
    direction: Direction,
    touch_idx: int,
    choch_idx: int,
    tolerance: float,
    ts_utc,
) -> Optional[str]:
    """Name of the pool swept by the touch..choch excursion, or None.

    A LONG's excursion sweeps LOW-side pools (wick below the level); a
    SHORT's sweeps HIGH-side pools. Priority: PDH/PDL > Asia > EQ pools
    (equal_count >= 2) > single unswept swing. Levels are taken as-of the
    touch (m5[:touch_idx] / the PIT H1 slice), so the excursion itself
    cannot create the pool it sweeps.
    """
    window = m5[touch_idx:choch_idx + 1]
    if not window:
        return None
    is_long = direction == Direction.LONG
    extreme = min(c.low for c in window) if is_long else max(c.high for c in window)

    def crossed(level: float) -> bool:
        return extreme < level if is_long else extreme > level

    today = _prague_date(ts_utc)
    prev_days = sorted(
        {d for c in m5 if (d := _prague_date(c.timestamp)) < today}, reverse=True
    )
    if prev_days:
        pd = _day_extremes(m5, prev_days[0])
        if pd is not None:
            pdl, pdh = pd
            if is_long and crossed(pdl):
                return "PDL"
            if not is_long and crossed(pdh):
                return "PDH"
    asia = _asia_extremes(m5, today)
    if asia is not None:
        al, ah = asia
        if is_long and crossed(al):
            return "AsiaL"
        if not is_long and crossed(ah):
            return "AsiaH"

    levels = (
        find_liquidity(m5[:touch_idx], "M5", tolerance)
        + find_liquidity(h1_pit, "H1", tolerance)
    )
    side = [lv for lv in levels if lv.is_high != is_long and crossed(lv.price)]
    if not side:
        return None
    pools = [lv for lv in side if lv.equal_count >= 2]
    if pools:
        n = max(lv.equal_count for lv in pools)
        return (f"EQL({n})" if is_long else f"EQH({n})")
    return "swingL" if is_long else "swingH"


def dealing_range(h1_pit: List[Candle]) -> Optional[Tuple[float, float]]:
    """(low, high) of the last confirmed H1 swing low and swing high."""
    pivots = find_pivots(h1_pit)
    highs = [p for p in pivots if p.is_high]
    lows = [p for p in pivots if not p.is_high]
    if not highs or not lows:
        return None
    low, high = lows[-1].price, highs[-1].price
    if low >= high:
        return None
    return low, high


def pd_state(
    direction: Direction, entry: float, rng: Optional[Tuple[float, float]]
) -> Optional[str]:
    """'ok' when the entry is on the right side of equilibrium, 'bad' when
    not, None when no dealing range exists (condition can't be judged)."""
    if rng is None:
        return None
    mid = (rng[0] + rng[1]) / 2.0
    if direction == Direction.LONG:
        return "ok" if entry <= mid else "bad"
    return "ok" if entry >= mid else "bad"
```

- [ ] **Step 4: Run tests, verify all pass**

Run: `python -m pytest sn_test_rules.py -v` — expected: 5 passed.

- [ ] **Step 5: Sanity-fix pass** — if `test_sweep_label_pdl_for_long` fails on pivot confirmation details, adjust the synthetic candle series (wick asymmetry, wing counts), NOT the rule logic, unless the logic genuinely misreads the definition. Re-run until green.

---

### Task 2: `sn_exit.py` — hybrid exit simulator (TP1 2R half + BE + runner)

**Files:**
- Create: `C:\temp\926_bot_data\scripts\sn_exit.py`
- Create: `C:\temp\926_bot_data\scripts\sn_test_exit.py`

**Interfaces:**
- Consumes: `app.services.smc.models.Candle`, `Direction`.
- Produces (used by Task 4):
  - `evaluate_hybrid(signal: dict, candles: list, runner_r: float, now) -> dict` — steps closed candles; mutates/returns the signal with `status` in `{"pending","open","open_runner","sl","tp1_be","tp1_runner","expired","timeout"}` and, when resolved, `r` (float, in R):
    - `sl` → `r = -1.0` (full stop before TP1)
    - `tp1_be` → `r = +1.0` (half banked 2R, half back at entry)
    - `tp1_runner` → `r = 0.5 * 2.0 + 0.5 * runner_r`
    - `expired` → `r = 0.0`; `timeout` (open > 7 days, mirrors journal `OPEN_TIMEOUT`) → mark-to-nothing `r = 0.0` and flagged, reported separately.
  - Signal input keys: `direction` (`"long"`/`"short"`), `entry`, `stop_loss` (floats), `created_at`, `expires_at` (ISO strings), optional `checked_until`.

- [ ] **Step 1: Write the failing tests**

```python
# sn_test_exit.py
import os
import sys
from datetime import datetime, timedelta, timezone

SCRATCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRATCH)
sys.path.insert(0, r"C:\Git\926_bot")

from app.services.smc.models import Candle  # noqa: E402
from sn_exit import evaluate_hybrid  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 3, 3, 9, 0, tzinfo=UTC)


def c(i, o, h, low, cl):
    return Candle(timestamp=T0 + timedelta(minutes=5 * i),
                  open=o, high=h, low=low, close=cl)


def sig(entry=100.0, sl=99.0):
    return {
        "direction": "long", "entry": entry, "stop_loss": sl,
        "created_at": T0.isoformat(),
        "expires_at": (T0 + timedelta(hours=8)).isoformat(),
        "status": "pending",
    }


def run(candles, runner_r=3.5, now=None):
    now = now or (candles[-1].timestamp + timedelta(minutes=5))
    return evaluate_hybrid(sig(), candles, runner_r, now)


def test_full_stop_before_tp1_is_minus_one():
    # fill at 100, drop through 99 before touching 102 (TP1 = entry + 2R)
    out = run([c(1, 100.5, 100.6, 100.0, 100.2),   # fill (touch entry)
               c(2, 100.2, 100.4, 98.9, 99.0)])    # SL
    assert out["status"] == "sl" and out["r"] == -1.0


def test_tp1_then_be_is_plus_one():
    out = run([c(1, 100.5, 100.6, 100.0, 100.2),   # fill
               c(2, 100.2, 102.1, 100.1, 102.0),   # TP1 at 102 (2R)
               c(3, 102.0, 102.5, 99.9, 100.0)])   # back to entry -> BE
    assert out["status"] == "tp1_be" and out["r"] == 1.0


def test_tp1_then_runner_hits_full_r():
    out = run([c(1, 100.5, 100.6, 100.0, 100.2),   # fill
               c(2, 100.2, 102.1, 100.1, 102.0),   # TP1
               c(3, 102.0, 103.6, 101.9, 103.5)],  # runner 3.5R = 103.5
              runner_r=3.5)
    assert out["status"] == "tp1_runner"
    assert abs(out["r"] - (0.5 * 2.0 + 0.5 * 3.5)) < 1e-9


def test_same_candle_tp1_and_sl_counts_sl():
    out = run([c(1, 100.5, 100.6, 100.0, 100.2),
               c(2, 100.2, 102.5, 98.9, 99.5)])    # both TP1 and SL touched
    assert out["status"] == "sl" and out["r"] == -1.0


def test_same_candle_runner_and_be_counts_be():
    out = run([c(1, 100.5, 100.6, 100.0, 100.2),
               c(2, 100.2, 102.1, 100.1, 102.0),   # TP1
               c(3, 102.0, 103.6, 99.9, 101.0)])   # runner AND entry touched
    assert out["status"] == "tp1_be" and out["r"] == 1.0


def test_pending_expires_at_session_end():
    candles = [c(1, 101.0, 101.5, 100.6, 101.2)]   # never touches entry
    now = T0 + timedelta(hours=9)                  # past expires_at
    out = evaluate_hybrid(sig(), candles, 3.5, now)
    assert out["status"] == "expired" and out["r"] == 0.0


def test_signal_candle_cannot_fill_itself():
    # candle ending exactly at created_at must be ignored
    pre = Candle(timestamp=T0 - timedelta(minutes=5),
                 open=100.2, high=100.4, low=99.5, close=100.0)
    out = evaluate_hybrid(sig(), [pre], 3.5, T0 + timedelta(minutes=5))
    assert out["status"] == "pending"
```

- [ ] **Step 2: Run tests, verify they fail with `ModuleNotFoundError: sn_exit`**

Run: `python -m pytest sn_test_exit.py -v`

- [ ] **Step 3: Implement `sn_exit.py`**

```python
"""Hybrid exit simulator: TP1 at 2R on half, stop to BE, runner to a fixed
multiple. Same stepping/watermark discipline as journal.evaluate_signal,
extended with the partial-close state machine. Conservative ambiguity: any
candle that touches both the favourable and adverse level resolves adverse
(TP1+SL -> SL; runner+BE -> BE)."""

from datetime import datetime, timedelta, timezone
from typing import List

TP1_R = 2.0
OPEN_TIMEOUT = timedelta(days=7)  # mirrors journal.OPEN_TIMEOUT
M5D = timedelta(minutes=5)


def _parse(ts: str) -> datetime:
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def evaluate_hybrid(signal: dict, candles: List, runner_r: float, now) -> dict:
    if signal.get("status") not in ("pending", "open", "open_runner"):
        return signal
    is_long = signal["direction"] == "long"
    entry, sl = signal["entry"], signal["stop_loss"]
    risk = abs(entry - sl)
    tp1 = entry + TP1_R * risk if is_long else entry - TP1_R * risk
    runner = entry + runner_r * risk if is_long else entry - runner_r * risk
    watermark = _parse(signal.get("checked_until") or signal["created_at"])

    for candle in candles:
        if candle.timestamp + M5D <= watermark:
            continue
        if signal["status"] == "pending":
            touched = candle.low <= entry if is_long else candle.high >= entry
            if not touched:
                continue
            signal["status"] = "open"
            signal["filled_at"] = candle.timestamp.isoformat()
        if signal["status"] == "open":
            hit_sl = candle.low <= sl if is_long else candle.high >= sl
            hit_tp1 = candle.high >= tp1 if is_long else candle.low <= tp1
            if hit_sl:                      # adverse first, always
                signal["status"], signal["r"] = "sl", -1.0
            elif hit_tp1:
                signal["status"] = "open_runner"
                signal["tp1_at"] = candle.timestamp.isoformat()
                # BE and runner are NOT evaluated on the TP1 candle itself:
                # the candle that tags 2R necessarily also spans the entry
                # region it just left; judging BE on it would be double
                # jeopardy on intra-candle path we cannot see. Conservative
                # enough: the very next candle judges both.
                continue
        if signal["status"] == "open_runner":
            hit_be = candle.low <= entry if is_long else candle.high >= entry
            hit_run = candle.high >= runner if is_long else candle.low <= runner
            if hit_be:                      # adverse first, always
                signal["status"], signal["r"] = "tp1_be", 0.5 * TP1_R
            elif hit_run:
                signal["status"] = "tp1_runner"
                signal["r"] = 0.5 * TP1_R + 0.5 * runner_r
        if signal["status"] in ("sl", "tp1_be", "tp1_runner"):
            signal["resolved_at"] = candle.timestamp.isoformat()
            break

    if candles:
        signal["checked_until"] = (candles[-1].timestamp + M5D).isoformat()

    if signal["status"] == "pending":
        expires = signal.get("expires_at")
        if expires and now > _parse(expires):
            signal["status"], signal["r"] = "expired", 0.0
            signal["resolved_at"] = now.isoformat()
    elif signal["status"] in ("open", "open_runner"):
        if now - _parse(signal["created_at"]) > OPEN_TIMEOUT:
            signal["status"], signal["r"] = "timeout", 0.0
            signal["timeout_from"] = "runner" if "tp1_at" in signal else "open"
            signal["resolved_at"] = now.isoformat()
    return signal
```

- [ ] **Step 4: Run tests, verify all 7 pass**

Run: `python -m pytest sn_test_exit.py -v`

- [ ] **Step 5: Review the TP1-candle skip decision** — the `continue` after TP1 means BE/runner judgment starts on the NEXT candle. Confirm the tests encode this (test 3 and 5 use separate candles). If the owner later wants same-candle judgment, it is one deleted `continue` — note this in the report's methodology section.

---

### Task 3: `sn_run.py` — year replay with sniper fields, plus baseline sanity

**Files:**
- Create: `C:\temp\926_bot_data\scripts\sn_run.py`
- Reference (read, do not modify): `year_fx_run.py`, `year_run.py`, `scripts/funnel.py` (repo), `year_fx_prep.py`

**Interfaces:**
- Consumes: Task 1 (`sweep_label`, `dealing_range`, `pd_state`); repo engine (`TripleSyncEngine`, `CONSERVATIVE`), `find_liquidity`, `nearest_liquidity`, `session_end_utc`, `active_session`, `PIT_FLOOR`, `classify_result`.
- Produces (used by Task 4): per-pair JSONL of approval records `sn_<PAIR>_c<k>.jsonl` + checkpoint JSON with funnel counters. Each approval record:

```json
{"i": 0, "ts": "...", "created_at": "...", "expires_at": "...",
 "session": "...", "direction": "long",
 "entry_fvg": 0.0, "entry_used": 0.0, "entry_kind": "ob|fvg",
 "stop_loss": 0.0, "risk": 0.0, "price": 0.0, "gap_r": 0.0,
 "sweep": "PDL|...|null", "pd": "ok|bad|null",
 "room_r": 0.0, "rr_engine": 0.0}
```

- [ ] **Step 1: Write `sn_run.py`, derived from `year_fx_run.py`**

Copy `year_fx_run.py` to `sn_run.py` and change ONLY the following (keep chunking, checkpointing, PIT `bisect` slicing, warm-up, session logic, caps identical):

1. Drop the `nom5`/`old`/`curNG` variants and the `DROP_M5` machinery — one engine per pair: `NewEngine(instrument=get_instrument(pair), profile=CONSERVATIVE, min_rr=1.0, max_entry_gap_r=0.75, enforce_sessions=True)`. Keep the `sweep_extreme` / `nearest_liquidity` recorders (`REC`) for the touch/choch indices and levels.
2. Record touch/choch for the sweep call: extend `sweep_extreme_rec` to also store `REC["touch"], REC["choch"] = touch, choch` (it receives them as arguments).
3. Data loading: accept `--pair ETHUSD|USDJPY|USDCAD`; load `eth_year.pkl` for ETHUSD (tuple/dict as `year_prep.py` produced — reuse its loader), `fx_<PAIR>.pkl` for forex. `require_weekday = instrument.source == "forex"`.
4. On every `Verdict.APPROVED_*` close, build the extended record:

```python
s = result.setup
entry_fvg = s.entry
if s.order_block is not None:
    entry_used = s.order_block.top if s.direction == Direction.LONG \
        else s.order_block.bottom
    entry_kind = "ob"
else:
    entry_used, entry_kind = entry_fvg, "fvg"
risk = abs(entry_used - s.stop_loss)
tol = inst.min_fvg
h1_pit, h4_pit = h1_slice, h4_slice          # the slices already built
sw = sweep_label(m5_slice, h1_pit, s.direction,
                 REC["touch"], REC["choch"], tol, now)
pd = pd_state(s.direction, entry_used, dealing_range(h1_pit))
ht_levels = (find_liquidity(h1_pit, "H1", tol)
             + find_liquidity(h4_pit, "H4", tol))
obstacle = real_nearest_liquidity(ht_levels, s.direction, entry_used)
room_r = (abs(obstacle.price - entry_used) / risk) if (obstacle and risk) else None
```

   `find_order_block` already guarantees `entry_used` strictly inside (entry, stop), so `risk > 0` whenever the engine approved.
5. Keep the funnel stage counters (`classify_result`) so the report can show where closes die.

- [ ] **Step 2: Harness sanity — reproduce the published year baseline BEFORE reading any new number**

Run `sn_run.py` with the new fields DISABLED from affecting anything (they are recorded, not filtering — this is automatic) and compare approved-close counts against the published year runs:

- ETHUSD conservative, gate 0.75 real: **420 approved closes** (eth-year-replay.md §2.3).
- USDJPY conservative, real 0.75 engine: **80 approvals** (forex-year-replay.md §1.1).
- USDCAD conservative `cur` alerts: **9 rising-edge alerts** (forex-year-replay.md §5.4, check after Task 4's counting).

Expected: exact match on ETHUSD and USDJPY approved-close counts. Any mismatch is a STOP-the-line bug in the copy — diff against `year_fx_run.py` until found. Record the comparison in the checkpoint JSON.

- [ ] **Step 3: Run the year, all three pairs, chunked**

```bash
cd C:\temp\926_bot_data\scripts
python sn_run.py --pair ETHUSD  --nchunks 12 --chunk 0..11   # 12 jobs
python sn_run.py --pair USDJPY  --nchunks 3  --chunk 0..2
python sn_run.py --pair USDCAD  --nchunks 3  --chunk 0..2
```

Run chunk jobs in parallel (6 at a time max, as `year_fx_driver.py` did). Each chunk checkpoints every 2000 evaluated closes. Do NOT pipe output through `tail` (known 3-hour trap — see RESUME.md).

- [ ] **Step 4: Verify chunk completeness** — merged evaluated-close count per pair must equal the published counts (~52 227 ETHUSD, ~36 960 per forex pair). Missing chunks re-run individually.

---

### Task 4: `sn_analyze.py` — outcomes, tiers, runner sweep, baselines, train/test

**Files:**
- Create: `C:\temp\926_bot_data\scripts\sn_analyze.py`
- Reference: `year_fx_analyze.py`, `year_exit.py` (counting + outcome patterns)

**Interfaces:**
- Consumes: Task 3 JSONL records; Task 2 `evaluate_hybrid`; repo `journal.evaluate_signal` (baseline exit); candle pickles for outcome stepping.
- Produces: `sn_analysis.json` — every table the report needs.

- [ ] **Step 1: Implement counting and tier classification**

- Idea counting (headline): first approved close per `(direction, round(entry_used, 8), Prague date)`; rising-edge and fingerprint as cross-checks (port both from `year_fx_analyze.py` unchanged).
- Tier per idea-alert: `star = (room_r is None or room_r >= 2.5) and sweep is not None and pd == "ok"`. Also record the miss-list (which of the three failed) for every non-star alert.

- [ ] **Step 2: Outcomes**

For every idea-alert, step M5 candles from `created_at` with:

- **New design:** `evaluate_hybrid` at `runner_r` ∈ {3.0, 3.5, 4.0}, entry = `entry_used`, stop = `stop_loss`, expiry = `expires_at` (session end). Fill rate, status mix, per-trade R, totals — split by tier (⭐ / non-⭐) and by entry_kind (ob/fvg).
- **Baseline A (shipped rule):** `journal.evaluate_signal` with the engine's own `entry_fvg` / `stop_loss` / `take_profit` from the record (add `take_profit`/`rr_engine` to the Task 3 record if missing — it is `s.take_profit`).
- **Baseline B (flat 2R full position, FVG entry):** `evaluate_hybrid` degenerate check is NOT valid here; implement as `journal.evaluate_signal` with `take_profit = entry_fvg ± 2.0 * risk_fvg`. This is the ETH-year's best-known exit, the honest yardstick.

- [ ] **Step 3: Train/test split**

Everything in Step 2 computed twice: train (< 2026-02-05 UTC) and test (>=). The runner multiple is chosen on train (idea-counted avg R, with a stability sanity check: the chosen multiple must not be last on test). Report both halves side by side; never sum them into one headline.

- [ ] **Step 4: Per-condition kill analysis**

For each ⭐ condition alone (room / sweep / P/D): how many idea-alerts it removes, and the counterfactual R (hybrid exit, chosen runner) of the removed set — "what the filter killed". Same for the OB entry: R and fill-rate of `entry_kind == "ob"` alerts under OB entry vs the same alerts under FVG entry.

- [ ] **Step 5: Run the analysis, spot-check three alerts by hand**

Run: `python sn_analyze.py --pairs ETHUSD USDJPY USDCAD --out sn_analysis.json`
Pick one ⭐ and two non-⭐ alerts from the output; manually trace their candles (print the M5 window) and confirm fill/TP1/runner/BE/SL transitions match the recorded statuses. Any disagreement is a bug; fix before the report.

---

### Task 5: The report + green-light evaluation

**Files:**
- Create: `C:\temp\926_bot_data\reports\sniper-replay.md`
- Modify: `C:\Git\926_bot\docs\superpowers\specs\2026-08-12-sniper-redesign-design.md` (append a one-line pointer to the report under §5)

**Interfaces:**
- Consumes: `sn_analysis.json`, checkpoint funnel counters.

- [ ] **Step 1: Write the report** — follow the house style of `eth-year-replay.md` (verdict first, methodology with what-was-verified, honest caveats). Required sections, per spec §5.3:

1. Verdict first: does the ⭐ tier green-light? (idea-counted R positive on BOTH halves, frequency ≥ ~1 per 2 weeks pooled).
2. Harness sanity (the Task 3 Step 2 reproduction table).
3. ⭐ frequency/week (pooled and per pair), tier mix, miss-list histogram.
4. Fill rate: new design vs baseline A, and the OB-vs-FVG entry delta.
5. Runner sweep table 3.0/3.5/4.0 — train picks, test confirms.
6. New design vs baseline A (shipped) vs baseline B (flat 2R) — idea-counted, per half.
7. Per-condition kills: what each ⭐ condition costs, including which of the year's giant winners (USDJPY 2026-07-31, 2025-09-29) land in which tier.
8. Honest caveats: one regime, no fees/slippage, news/discipline not modelled, TP1-candle skip decision, room-check uses H1/H4 pools only.

- [ ] **Step 2: Green-light check** — apply spec §5.4 mechanically. If ⭐ frequency < 1 per 2 weeks pooled or R non-positive on either half: the report's final section lists which single condition relaxation (per Task 4 Step 4 data) recovers the most R per added alert, as the owner's decision menu. Do NOT pick for him.

- [ ] **Step 3: Commit the spec pointer**

```bash
cd C:\Git\926_bot
git add docs/superpowers/specs/2026-08-12-sniper-redesign-design.md
git commit -m "spec: link sniper replay report (phase 1 complete)"
```

- [ ] **Step 4: Deliver the report to the owner (Russian summary in chat, report file attached), wait for his verdict on runner multiple + any relaxation.**

---

## Phase 2 — bot implementation (separate plan, gated)

Phase 2 (engine OB entry, journal partial-close columns + migration, two-tier alerting in `smc_watcher.py`/`notifier.py`, roster change to ETHUSD+USDJPY+USDCAD) is deliberately **not planned here**: its parameters (runner multiple, relaxed/kept conditions, tier thresholds) are outputs of Task 5 and an owner decision. Per spec §Implementation order, after the owner reviews the replay report, write `docs/superpowers/plans/<date>-sniper-bot-implementation.md` via the writing-plans skill with those decisions baked in. Planning it now would hardcode guesses the replay exists to replace.

## Self-review notes

- Spec coverage: §1 roster → Phase 2 (gated, intentional); §2 OB entry → Tasks 3–4; §3 hybrid exit → Tasks 2, 4; §4 tiers → Tasks 1, 3, 4; §5 validation → Tasks 3–5 (out-of-sample in Task 4 Step 3); §7 caveats → Task 5 Step 1.8.
- Type consistency: `sweep_label`/`dealing_range`/`pd_state` signatures match between Tasks 1 and 3; `evaluate_hybrid(signal, candles, runner_r, now)` matches between Tasks 2 and 4; approval-record keys listed once in Task 3 and consumed by name in Task 4.
- No repo source is modified anywhere in Phase 1; the only repo writes are the spec pointer and this plan.
