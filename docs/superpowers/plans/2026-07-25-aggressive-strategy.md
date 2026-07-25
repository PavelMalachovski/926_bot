# Aggressive Strategy Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the zone-touch bug that hid the M5 CHoCH trigger, add an opt-in per-pair "aggressive" strategy profile, an RR-filtered pre-market plan, and an offline funnel-calibration script.

**Architecture:** A frozen `StrategyProfile` dataclass carries the four relaxations (H4-CHoCH entry, scaled min FVG, first-retest zones, day-wide FVG scope). `TripleSyncEngine` reads the profile at exactly four decision points; everything else (entry/SL/RR/sizing/discipline) is profile-independent. The profile per pair lives in the existing SQLite kv state and is switched via a new `/strategy` command. The pre-market plan walks candidate targets outward until RR ≥ min_rr.

**Tech Stack:** Python 3.11, dataclasses, pytest (`asyncio_mode=auto`), structlog, httpx, SQLite. No pandas anywhere. matplotlib (Agg) only in `chart.py`.

## Global Constraints

- **All bot-facing text is English**; code comments/spec discussion may be English. Owner conversation is Russian but that does not appear in code.
- **Every dynamic string in a Telegram message goes through `notifier.escape_html`.** Only `<b>` tags allowed.
- **Quiet mode stays default**: do NOT add any new unsolicited Telegram messages. No `/funnel` command. The funnel lives in an offline script only.
- **Per-instrument parameters live only in `instruments.py`.** The aggressive profile scales them with a *multiplier*, never a new absolute threshold.
- **The conservative profile must be bit-for-bit today's behaviour**, except where the zone-touch fix legitimately changes a verdict.
- **Strategy rules are law**: min RR 1:2, sessions 08–20 Prague (forex Mon–Fri), news blackout −60/+15, Rule 0.2 (two taken stops = day closed), Rule 10, risk 2% (no half-lot) are untouched by the profile.
- **Tests are network-free.** Build candles via `tests/test_smc/helpers.py`.
- Run `pytest tests/ -v` and `flake8 app/ tests/ smc_watcher.py` before every commit.
- Branch: `feat/aggressive-strategy` (already created from `origin/master`). PR to `master`.

---

## File Structure

- Create `app/services/smc/profiles.py` — `StrategyProfile` dataclass + `PROFILES` registry + `get_profile`.
- Create `scripts/funnel.py` — offline calibration replay (not imported by the worker).
- Modify `app/services/smc/structure.py` — `zone_touch_span` (replaces `zone_touch_index`), `h4_choch_direction`, `find_target_zones`, `find_h1_zone(max_touches=)`, `_mark_zone_state` touch count.
- Modify `app/services/smc/models.py` — `Zone.touches`, `AnalysisResult.profile_key`.
- Modify `app/services/smc/engine.py` — accept `profile`, read it at 4 points, set `result.profile_key`.
- Modify `app/services/smc/plan.py` — `build_plan(min_rr, profile)`, walk targets, no-scenario reason.
- Modify `app/services/smc/notifier.py` — profile tag in `format_result`, RR-walk copy in `format_plan`, drop the "below 1:2" marker.
- Modify `app/services/smc/state.py` — `pair_profile` dict + `set_profile`.
- Modify `app/services/smc/telegram_bot.py` — `/strategy` command, keyboard, callbacks.
- Modify `app/services/smc/db.py` — `profile_key` signal column + migration.
- Modify `app/services/smc/journal.py` — persist `profile_key` in `record`.
- Modify `smc_watcher.py` — `_build_engine(key)` reads the pair's profile; `_send_pair_plan` passes profile + min_rr; `/strategy` wiring.
- Modify `app/core/config.py` — `SMC_DEFAULT_PROFILE` (default `conservative`).
- Docs: `README.md`, `CLAUDE.md`.

---

## Task 1: Fix the zone-touch span bug (both profiles)

The single highest-value change. `zone_touch_index` returns the *last* candle in the zone, so `find_choch` scans a one-candle window and the M5 CHoCH trigger is invisible while price sits in the zone. Replace it with the *first* index of the last contiguous excursion.

**Files:**
- Modify: `app/services/smc/structure.py` (replace `zone_touch_index` at lines 203-214)
- Modify: `app/services/smc/engine.py` (call site around lines 156, 174, 192, 208, 228)
- Test: `tests/test_smc/test_structure.py`, `tests/test_smc/test_engine.py`

**Interfaces:**
- Produces: `zone_touch_span(candles: List[Candle], zone: Zone) -> Optional[Tuple[int, int]]` returning `(start, end)` inclusive indices of the last contiguous run of candles that entered the zone, or `None`.
- Removes: `zone_touch_index` (single internal call site).

- [ ] **Step 1: Write the failing test for the span**

Add to `tests/test_smc/test_structure.py`:

```python
from app.services.smc.structure import zone_touch_span
from app.services.smc.models import Zone
from datetime import datetime, timezone
from tests.test_smc.helpers import make_candles


def _zone(bottom, top, is_demand=True):
    return Zone(bottom=bottom, top=top, is_demand=is_demand, pivot_index=0,
                timestamp=datetime(2026, 7, 6, tzinfo=timezone.utc))


def test_zone_touch_span_returns_first_and_last_of_last_excursion():
    # closes: above zone, then 8 candles inside 3130-3140, then back above
    closes = [3150, 3145, 3135, 3134, 3133, 3136, 3138, 3137, 3135, 3134, 3160]
    candles = make_candles(closes)
    span = zone_touch_span(candles, _zone(3130, 3140))
    assert span is not None
    start, end = span
    # the excursion starts at the first candle whose range enters the zone
    assert start < end
    # all candles in [start, end] intersect the zone; start-1 does not sit fully inside
    assert candles[start].low <= 3140 and candles[start].high >= 3130


def test_zone_touch_span_none_when_never_touched():
    candles = make_candles([3200, 3205, 3210, 3208])
    assert zone_touch_span(candles, _zone(3130, 3140)) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_smc/test_structure.py -k zone_touch_span -v`
Expected: FAIL with `ImportError: cannot import name 'zone_touch_span'`.

- [ ] **Step 3: Implement `zone_touch_span`, remove `zone_touch_index`**

Replace lines 203-214 of `app/services/smc/structure.py`:

```python
def zone_touch_span(
    candles: List[Candle], zone: Zone
) -> Optional[Tuple[int, int]]:
    """Inclusive (start, end) indices of the LAST contiguous excursion into
    the zone, or None if price never entered.

    Intended for M5 candles against an H1 zone. `start` is the origin for the
    CHoCH/FVG search (the bug this replaces returned only the last candle, so
    the M5 CHoCH inside the zone was invisible while price sat in the zone).
    """
    start = end = None
    for i, c in enumerate(candles):
        in_zone = c.low <= zone.top and c.high >= zone.bottom
        if in_zone:
            if start is None or (end is not None and i > end + 1):
                start = i  # begin a new run
            end = i
    if start is None:
        return None
    return start, end
```

Ensure `Tuple` is imported (line 3 already has `from typing import List, Optional, Tuple`).

- [ ] **Step 4: Run the span tests**

Run: `pytest tests/test_smc/test_structure.py -k zone_touch_span -v`
Expected: PASS (both).

- [ ] **Step 5: Update the engine call site**

In `app/services/smc/engine.py`, replace the `zone_touch_index` import (line 27 area) and its use. Change the import line from `zone_touch_index,` to `zone_touch_span,`. Then replace the touch block (around line 156):

```python
        # Rule 3 phase 1/2 — has price pulled back into the zone?
        span = zone_touch_span(m5, zone)
        if span is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                f"Price has not reached the H1 {'Demand' if zone.is_demand else 'Supply'} "
                f"zone ({zone.bottom:.2f}–{zone.top:.2f}) yet — pullback phase"
            )
            result.watch_notes.append(
                f"Set an alert at {zone.top if zone.is_demand else zone.bottom:.2f} — "
                "on zone touch, check M5 for a CHoCH + FVG"
            )
            result.watch_notes.append(
                f"Invalidation: H1 body close "
                f"{'below ' + format(zone.bottom, '.2f') if zone.is_demand else 'above ' + format(zone.top, '.2f')}"
            )
            return result
        touch = span[0]
```

`touch` keeps its downstream meaning (`m5[touch:]` validity scan, `find_choch(m5, direction, touch)`, `select_valid_fvg(..., max(touch, choch - 2), ...)`) — now anchored at the *start* of the excursion, so the CHoCH search window spans the whole time price was in the zone.

- [ ] **Step 6: Add an engine regression test**

Add to `tests/test_smc/test_engine.py` (this is the core bug reproduction — CHoCH forms while price is still inside the zone):

```python
    def test_choch_inside_zone_is_seen_while_price_still_in_zone(self):
        # m5_long_trigger ends with the CHoCH candle (index 16) and price
        # still hovering in/around the zone at the tail. Before the span fix
        # the search window was a single candle and this returned WATCH.
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.verdict in (
            Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET
        )
```

- [ ] **Step 7: Run the full SMC suite (conservative invariant check)**

Run: `pytest tests/test_smc/ -v`
Expected: PASS. If any previously-passing test now changes verdict, inspect it: a change is only acceptable if it is the touch-span fix legitimately surfacing a CHoCH that was wrongly hidden. Document any such change in the commit message. If a test breaks for another reason, the span logic is wrong — fix it, do not edit the test to match.

- [ ] **Step 8: Lint and commit**

```bash
flake8 app/services/smc/structure.py app/services/smc/engine.py tests/test_smc/
git add app/services/smc/structure.py app/services/smc/engine.py tests/test_smc/
git commit -m "Fix: M5 CHoCH inside the H1 zone was invisible (zone-touch span)

zone_touch_index returned the last candle in the zone, so find_choch
scanned a one-candle window while price sat in the zone. Replace with
zone_touch_span returning the start of the last contiguous excursion.
Fixes the likely cause of the zero-alert week; applies to both profiles.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: Offline funnel-calibration script

Built early (before the aggressive profile) so it can measure the funnel both before and after, and so it is ready to produce the `fvg_size_factor` numbers at the end. Not imported by the worker.

**Files:**
- Create: `scripts/funnel.py`
- Test: `tests/test_smc/test_funnel.py`

**Interfaces:**
- Consumes: `TripleSyncEngine.evaluate`, `AnalysisResult`, `get_instrument`, `active_session`.
- Produces: `replay_funnel(instrument, h4, h1, m5, profile, min_rr) -> Dict[str, int]` — a counter of funnel outcomes keyed by stage (`"off_session"`, `"h4_flat"`, `"no_zone"`, `"no_touch"`, `"no_choch"`, `"fvg_small"`, `"rr_low"`, `"approved"`), replaying `evaluate` at every M5 close.

- [ ] **Step 1: Write the failing test**

Create `tests/test_smc/test_funnel.py`:

```python
from scripts.funnel import replay_funnel, classify_result
from app.services.smc.models import AnalysisResult, Verdict, Trend
from app.services.smc.profiles import get_profile
from app.services.smc.instruments import get_instrument
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES, H4_UPTREND_CLOSES, m5_long_trigger, make_candles,
)


def test_classify_approved():
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.APPROVED_LIMIT,
                       checked_at=None)
    assert classify_result(r) == "approved"


def test_classify_flat_h4():
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=None)
    r.h4_trend = Trend.FLAT
    r.reasons = ["H4 is flat or CHoCH against the trend — no direction"]
    assert classify_result(r) == "h4_flat"


def test_replay_counts_at_least_one_approved_on_the_trigger_fixture():
    counts = replay_funnel(
        get_instrument("ETHUSD"),
        make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        m5_long_trigger(),
        profile=get_profile("conservative"),
        min_rr=2.0,
    )
    assert counts["approved"] >= 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_smc/test_funnel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.funnel'` (or ImportError). Note: this task depends on Task 3's `profiles.py`; if running strictly in order, implement Task 3 first or stub `get_profile`. See sequencing note at the end — Task 3 may be interleaved.

- [ ] **Step 3: Implement the replay core**

Create `scripts/funnel.py`:

```python
"""Offline funnel calibration for the SMC strategy (NOT used by the worker).

Replays TripleSyncEngine.evaluate over historical candles for both profiles
and prints where setups die in the rule funnel and the size distribution of
near-miss FVGs. Use the numbers to choose StrategyProfile.fvg_size_factor.

Usage:
    python -m scripts.funnel --pair ETHUSD --days 45
    python -m scripts.funnel --all --days 45
"""

import argparse
import asyncio
from collections import Counter
from typing import Dict, List

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import INSTRUMENTS, Instrument, get_instrument
from app.services.smc.models import AnalysisResult, Candle, Trend, Verdict
from app.services.smc.profiles import PROFILES, StrategyProfile, get_profile
from app.services.smc.sessions import active_session


def classify_result(result: AnalysisResult) -> str:
    """Map an evaluate() outcome to a funnel stage label."""
    if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
        return "approved"
    if result.verdict == Verdict.OFF_SESSION:
        return "off_session"
    reason = (result.reasons[0] if result.reasons else "").lower()
    if result.h4_trend == Trend.FLAT and "no direction" in reason:
        return "h4_flat"
    if "no valid untested" in reason or "no untested opposite" in reason:
        return "no_zone"
    if "has not reached" in reason or "pullback phase" in reason:
        return "no_touch"
    if "has not printed a choch" in reason:
        return "no_choch"
    if "no valid fvg" in reason:
        return "fvg_small"
    if "rr 1:" in reason:
        return "rr_low"
    return "other"


def replay_funnel(
    instrument: Instrument,
    h4: List[Candle],
    h1: List[Candle],
    m5: List[Candle],
    profile: StrategyProfile,
    min_rr: float,
    warmup: int = 60,
) -> Dict[str, int]:
    """Replay evaluate() at each M5 close (after `warmup` candles). Off-session
    candles are skipped. Returns a Counter of funnel-stage labels."""
    engine = TripleSyncEngine(
        instrument=instrument, min_rr=min_rr, enforce_sessions=True,
        profile=profile, fetcher=None,
    )
    counts: Counter = Counter()
    for i in range(max(warmup, 2), len(m5) + 1):
        window = m5[:i]
        now = window[-1].timestamp
        if active_session(now, require_weekday=instrument.source == "forex") is None:
            counts["off_session"] += 1
            continue
        result = AnalysisResult(
            symbol=instrument.key, verdict=Verdict.SKIP, checked_at=now,
            price_decimals=instrument.price_decimals,
        )
        result.session_name = active_session(now)
        result.price = window[-1].close
        result = engine.evaluate(h4=h4, h1=h1, m5=window, result=result)
        counts[classify_result(result)] += 1
    return dict(counts)


async def _run(pairs: List[str], days: int) -> None:
    from smc_watcher import _build_fetcher  # reuse the worker's source resolution

    for key in pairs:
        instrument = get_instrument(key)
        fetcher = _build_fetcher(instrument)
        data = await fetcher.fetch_all_timeframes()
        print(f"\n=== {key} — last {len(data['m5'])} M5 candles ===")
        for profile in PROFILES.values():
            counts = replay_funnel(
                instrument, data["h4"], data["h1"], data["m5"], profile, min_rr=2.0
            )
            in_session = sum(v for k, v in counts.items() if k != "off_session")
            print(f"  {profile.label}: {counts}")
            print(f"    in-session checks: {in_session}, approved: {counts.get('approved', 0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC funnel calibration")
    parser.add_argument("--pair", default="ETHUSD")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--days", type=int, default=45)
    args = parser.parse_args()
    pairs = list(INSTRUMENTS) if args.all else [args.pair]
    asyncio.run(_run(pairs, args.days))


if __name__ == "__main__":
    main()
```

Note: `fetch_all_timeframes` pulls the fetchers' default history (≈400 M5, 300 H4). `--days` is advisory for now (the free feeds cap history); a future iteration can page. This is enough to compare profiles.

- [ ] **Step 4: Run the funnel tests**

Run: `pytest tests/test_smc/test_funnel.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
flake8 scripts/funnel.py tests/test_smc/test_funnel.py
git add scripts/funnel.py tests/test_smc/test_funnel.py
git commit -m "Add offline funnel calibration script (scripts/funnel.py)

Replays evaluate() over historical candles per profile and reports where
setups die in the rule funnel. Not imported by the worker.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `StrategyProfile` registry

**Files:**
- Create: `app/services/smc/profiles.py`
- Test: `tests/test_smc/test_profiles.py`

**Interfaces:**
- Produces:
  - `StrategyProfile` frozen dataclass with fields `key: str`, `label: str`, `allow_h4_choch_entry: bool`, `fvg_size_factor: float`, `max_zone_touches: int`, `fvg_day_scope: bool`.
  - `PROFILES: Dict[str, StrategyProfile]` with keys `"conservative"` and `"aggressive"`.
  - `get_profile(key: str) -> StrategyProfile` (falls back to conservative on unknown key).
  - `CONSERVATIVE`, `AGGRESSIVE` module constants.

- [ ] **Step 1: Write the failing test**

Create `tests/test_smc/test_profiles.py`:

```python
from app.services.smc.profiles import (
    PROFILES, CONSERVATIVE, AGGRESSIVE, get_profile,
)


def test_conservative_is_todays_behaviour():
    assert CONSERVATIVE.fvg_size_factor == 1.0
    assert CONSERVATIVE.max_zone_touches == 0
    assert CONSERVATIVE.fvg_day_scope is False
    assert CONSERVATIVE.allow_h4_choch_entry is False


def test_aggressive_relaxes_all_four():
    assert AGGRESSIVE.allow_h4_choch_entry is True
    assert AGGRESSIVE.max_zone_touches >= 1
    assert AGGRESSIVE.fvg_day_scope is True
    assert AGGRESSIVE.fvg_size_factor < 1.0


def test_get_profile_falls_back_to_conservative():
    assert get_profile("nonsense") is CONSERVATIVE
    assert get_profile("aggressive") is AGGRESSIVE
    assert set(PROFILES) == {"conservative", "aggressive"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_smc/test_profiles.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `profiles.py`**

```python
"""Strategy profiles: conservative (default) vs aggressive (opt-in).

A profile scales the strategy's strictness without changing any rule. It is
read by the engine at exactly four points (direction, FVG size, zone
selection, FVG scope). Per-instrument thresholds still live in instruments.py;
the profile only multiplies them (fvg_size_factor).

fvg_size_factor for AGGRESSIVE is a placeholder until scripts/funnel.py
produces the near-miss size distribution — do not treat 0.4 as final.
"""

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class StrategyProfile:
    key: str
    label: str
    allow_h4_choch_entry: bool  # take direction from an H4 CHoCH when trend is FLAT
    fvg_size_factor: float      # multiplier on Instrument.min_fvg (1.0 = unchanged)
    max_zone_touches: int       # 0 = untested only, 1 = allow one prior retest
    fvg_day_scope: bool         # True = FVG valid all Prague day, False = session block


CONSERVATIVE = StrategyProfile(
    key="conservative",
    label="🛡 Conservative",
    allow_h4_choch_entry=False,
    fvg_size_factor=1.0,
    max_zone_touches=0,
    fvg_day_scope=False,
)

# PLACEHOLDER fvg_size_factor — calibrate with scripts/funnel.py (Task 8).
AGGRESSIVE = StrategyProfile(
    key="aggressive",
    label="⚡ Aggressive",
    allow_h4_choch_entry=True,
    fvg_size_factor=0.4,
    max_zone_touches=1,
    fvg_day_scope=True,
)

PROFILES: Dict[str, StrategyProfile] = {
    CONSERVATIVE.key: CONSERVATIVE,
    AGGRESSIVE.key: AGGRESSIVE,
}


def get_profile(key: str) -> StrategyProfile:
    """Look up a profile by key; unknown keys fall back to conservative."""
    return PROFILES.get((key or "").lower(), CONSERVATIVE)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_smc/test_profiles.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
flake8 app/services/smc/profiles.py tests/test_smc/test_profiles.py
git add app/services/smc/profiles.py tests/test_smc/test_profiles.py
git commit -m "Add StrategyProfile registry (conservative default, aggressive opt-in)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: Structure primitives for the aggressive profile

**Files:**
- Modify: `app/services/smc/models.py` (add `Zone.touches`)
- Modify: `app/services/smc/structure.py` (`_mark_zone_state` count, `find_h1_zone(max_touches)`, `find_target_zones`, `h4_choch_direction`)
- Test: `tests/test_smc/test_structure.py`

**Interfaces:**
- Produces:
  - `Zone.touches: int` (default 0), set by `_mark_zone_state` to the number of completed excursions.
  - `find_h1_zone(candles, direction, max_touches: int = 0) -> Optional[Zone]`.
  - `find_target_zones(candles, direction, entry) -> List[Zone]` sorted nearest-first.
  - `find_target_zone(candles, direction, entry) -> Optional[Zone]` — thin wrapper `next(iter(find_target_zones(...)), None)`.
  - `h4_choch_direction(candles) -> Optional[Direction]`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_smc/test_structure.py`:

```python
from app.services.smc.structure import (
    find_h1_zone, find_target_zones, h4_choch_direction,
)
from app.services.smc.models import Direction
from tests.test_smc.helpers import H1_PULLBACK_CLOSES, H4_UPTREND_CLOSES


def test_find_h1_zone_untested_only_by_default():
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    zone = find_h1_zone(h1, Direction.LONG)  # max_touches=0
    assert zone is not None
    assert zone.touches == 0


def test_find_target_zones_sorted_nearest_first():
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    zones = find_target_zones(h1, Direction.LONG, entry=3138.0)
    tops = [z.bottom for z in zones]
    assert tops == sorted(tops)  # nearest (smallest bottom above entry) first


def test_h4_choch_direction_none_on_clean_uptrend():
    # a confirmed HH+HL uptrend has no unreclaimed break -> no CHoCH signal
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    assert h4_choch_direction(h4) is None


def test_h4_choch_direction_short_after_break_of_last_hl():
    # uptrend then a decisive body close below the last higher-low, held
    closes = H4_UPTREND_CLOSES + [3200, 3100, 3050, 3000, 2990]
    h4 = make_candles(closes, step_minutes=240)
    assert h4_choch_direction(h4) == Direction.SHORT
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_smc/test_structure.py -k "find_target_zones or h4_choch or find_h1_zone_untested" -v`
Expected: FAIL (`find_target_zones`/`h4_choch_direction` not defined; `touches` attribute missing).

- [ ] **Step 3: Add `Zone.touches`**

In `app/services/smc/models.py`, in the `Zone` dataclass (after `tested: bool = False`, line ~71):

```python
    tested: bool = False
    touches: int = 0
    invalidated: bool = False
```

- [ ] **Step 4: Count touches in `_mark_zone_state`**

In `app/services/smc/structure.py`, modify `_mark_zone_state` to count completed excursions:

```python
def _mark_zone_state(candles: List[Candle], zone: Zone) -> Zone:
    """Mark whether the zone was tested/invalidated after forming, and count
    completed touches (entered-and-left excursions)."""
    start = zone.pivot_index + PIVOT_WING + 1
    touches = 0
    in_zone_prev = False
    for c in candles[start:]:
        if zone.is_demand:
            if c.close < zone.bottom and c.body_low < zone.bottom:
                zone.invalidated = True
                return zone
        else:
            if c.close > zone.top and c.body_high > zone.top:
                zone.invalidated = True
                return zone
        in_zone = c.low <= zone.top and c.high >= zone.bottom
        if in_zone_prev and not in_zone:
            touches += 1
        in_zone_prev = in_zone
    zone.touches = touches
    zone.tested = touches > 0
    return zone
```

- [ ] **Step 5: `find_h1_zone` honours `max_touches`**

Replace `find_h1_zone`:

```python
def find_h1_zone(
    candles: List[Candle], direction: Direction, max_touches: int = 0
) -> Optional[Zone]:
    """Rule 2: latest valid H1 Demand (long)/Supply (short) zone with at most
    `max_touches` completed retests (0 = untested only, conservative)."""
    pivots = find_pivots(candles)
    want_high = direction == Direction.SHORT
    candidates = [p for p in pivots if p.is_high == want_high]
    for pivot in reversed(candidates):
        zone = _mark_zone_state(candles, build_zone(candles, pivot))
        if not zone.invalidated and zone.touches <= max_touches:
            return zone
    return None
```

- [ ] **Step 6: `find_target_zones` + thin `find_target_zone`**

Replace `find_target_zone` (lines 143-160) with:

```python
def find_target_zones(
    candles: List[Candle], direction: Direction, entry: float
) -> List[Zone]:
    """Rule 7: all untested opposite zones beyond entry, nearest first."""
    pivots = find_pivots(candles)
    want_high = direction == Direction.LONG
    out: List[Zone] = []
    for pivot in (p for p in pivots if p.is_high == want_high):
        zone = _mark_zone_state(candles, build_zone(candles, pivot))
        if zone.invalidated or zone.tested:
            continue
        if direction == Direction.LONG and zone.bottom > entry:
            out.append(zone)
        elif direction == Direction.SHORT and zone.top < entry:
            out.append(zone)
    if direction == Direction.LONG:
        out.sort(key=lambda z: z.bottom)          # nearest above entry first
    else:
        out.sort(key=lambda z: z.top, reverse=True)  # nearest below entry first
    return out


def find_target_zone(
    candles: List[Candle], direction: Direction, entry: float
) -> Optional[Zone]:
    """Nearest untested opposite zone beyond entry (back-compat wrapper)."""
    return next(iter(find_target_zones(candles, direction, entry)), None)
```

- [ ] **Step 7: Add `h4_choch_direction`**

Add near `detect_trend` in `structure.py`:

```python
def h4_choch_direction(candles: List[Candle]) -> Optional[Direction]:
    """Aggressive entry: direction implied by an unreclaimed H4 CHoCH.

    Consulted only when detect_trend() is FLAT. Returns SHORT if the last
    confirmed higher-low was broken down and not reclaimed, LONG if the last
    confirmed lower-high was broken up and not reclaimed, else None.
    """
    pivots = find_pivots(candles)
    highs = [p for p in pivots if p.is_high]
    lows = [p for p in pivots if not p.is_high]
    if lows and _break_still_holds(candles, lows[-1], below=True):
        return Direction.SHORT
    if highs and _break_still_holds(candles, highs[-1], below=False):
        return Direction.LONG
    return None
```

Ensure `Direction` is imported in `structure.py` (line 5 already imports it).

- [ ] **Step 8: Run the new tests + full suite**

Run: `pytest tests/test_smc/test_structure.py -v && pytest tests/test_smc/ -v`
Expected: PASS. `find_target_zone` wrapper keeps existing engine/plan tests green.

- [ ] **Step 9: Lint and commit**

```bash
flake8 app/services/smc/structure.py app/services/smc/models.py tests/test_smc/
git add app/services/smc/structure.py app/services/smc/models.py tests/test_smc/
git commit -m "Add structure primitives for profiles (zone touches, target list, H4 CHoCH)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: Wire the profile into the engine

**Files:**
- Modify: `app/services/smc/models.py` (`AnalysisResult.profile_key`)
- Modify: `app/services/smc/engine.py` (accept `profile`, read at 4 points, set `result.profile_key`)
- Test: `tests/test_smc/test_engine.py`

**Interfaces:**
- Consumes: `StrategyProfile`, `get_profile`, `h4_choch_direction`, `find_h1_zone(max_touches=)`.
- Produces: `TripleSyncEngine(..., profile: StrategyProfile = CONSERVATIVE)`; `AnalysisResult.profile_key: str = "conservative"`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_smc/test_engine.py`:

```python
from app.services.smc.profiles import AGGRESSIVE, CONSERVATIVE


def _agg_engine(**kwargs):
    defaults = dict(min_fvg_size=2.0, sl_buffer=2.0, min_rr=2.0, profile=AGGRESSIVE)
    defaults.update(kwargs)
    return TripleSyncEngine(**defaults)


class TestProfiles:
    def test_result_carries_profile_key(self):
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.profile_key == "conservative"

    def test_aggressive_takes_direction_from_h4_choch_when_flat(self):
        # H4 flat for detect_trend, but an unreclaimed break-down exists
        closes = [3000, 3050, 3000, 3050, 3000, 3050, 3000, 2900, 2850, 2840]
        h4 = make_candles(closes, step_minutes=240)
        # conservative: FLAT -> SKIP
        cons = _engine().evaluate(
            h4=h4, h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(), result=_fresh_result(),
        )
        assert cons.verdict == Verdict.SKIP
        # aggressive: consults h4_choch_direction (no crash, direction resolved)
        agg = _agg_engine().evaluate(
            h4=h4, h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(), result=_fresh_result(),
        )
        assert agg.profile_key == "aggressive"
        # with a downward CHoCH the aggressive path no longer SKIPs on "no direction"
        assert "no direction" not in " ".join(agg.reasons).lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_smc/test_engine.py -k Profiles -v`
Expected: FAIL (`profile` kwarg unknown / `profile_key` missing).

- [ ] **Step 3: Add `AnalysisResult.profile_key`**

In `app/services/smc/models.py`, in `AnalysisResult` (near `price_decimals`, line ~125):

```python
    price_decimals: int = 2
    profile_key: str = "conservative"
```

- [ ] **Step 4: Accept the profile in the engine constructor**

In `app/services/smc/engine.py`:
- Import: add `from app.services.smc.profiles import StrategyProfile, CONSERVATIVE` and `h4_choch_direction` to the `structure` import.
- Constructor: add `profile: StrategyProfile = CONSERVATIVE,` to `__init__` params and `self.profile = profile`.

- [ ] **Step 5: Read the profile at the four decision points**

In `evaluate`:

(a) After `result.profile_key = self.profile.key` at the top of `evaluate` (first line of the method body).

(b) Rule 1 — direction, replace the FLAT handling (lines 126-137):

```python
        result.h4_trend = detect_trend(h4)
        direction = None
        if result.h4_trend == Trend.UP:
            direction = Direction.LONG
        elif result.h4_trend == Trend.DOWN:
            direction = Direction.SHORT
        elif self.profile.allow_h4_choch_entry:
            direction = h4_choch_direction(h4)  # aggressive: catch the first leg
        if direction is None:
            result.verdict = Verdict.SKIP
            result.reasons.append("H4 is flat or CHoCH against the trend — no direction")
            result.watch_notes.append(
                "Wait for a clear HH+HL or LH+LL structure on H4 "
                "(2 closed bodies beyond the extreme)"
            )
            return result
```

(c) Rule 2 — zone selection (line 140):

```python
        zone = find_h1_zone(h1, direction, max_touches=self.profile.max_zone_touches)
```

(d) Rule 4 — FVG size and scope (lines 207-213):

```python
        same_day = self.profile.fvg_day_scope or self.instrument.source == "crypto"
        min_fvg = self.min_fvg_size * self.profile.fvg_size_factor
        fvg = select_valid_fvg(
            m5, direction, max(touch, choch - 2), min_fvg, same_day_scope=same_day,
        )
```

And in `_fvg_rejection_detail` and `_fvg_size_label`, use `self.min_fvg_size * self.profile.fvg_size_factor` when reporting the required size so diagnostics match. Simplest: add `self._effective_min_fvg = self.min_fvg_size * self.profile.fvg_size_factor` in `__init__` and use it in all three places (evaluate, `_fvg_size_label`, `_fvg_rejection_detail`).

- [ ] **Step 6: Run the profile tests + full suite**

Run: `pytest tests/test_smc/test_engine.py -v && pytest tests/test_smc/ -v`
Expected: PASS, conservative verdicts unchanged.

- [ ] **Step 7: Lint and commit**

```bash
flake8 app/services/smc/engine.py app/services/smc/models.py tests/test_smc/
git add app/services/smc/engine.py app/services/smc/models.py tests/test_smc/
git commit -m "Wire StrategyProfile into the engine (4 decision points, profile_key)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: Persist the profile and switch it per pair (`/strategy`)

**Files:**
- Modify: `app/services/smc/db.py` (`profile_key` signal column + migration)
- Modify: `app/services/smc/journal.py` (`record` writes `profile_key`)
- Modify: `app/services/smc/state.py` (`pair_profile`, `set_profile`)
- Modify: `app/services/smc/telegram_bot.py` (`/strategy`, keyboard, callbacks)
- Modify: `smc_watcher.py` (`_build_engine(key)` reads profile; `/strategy` help text)
- Modify: `app/core/config.py` (`SMC_DEFAULT_PROFILE`)
- Test: `tests/test_smc/test_db.py`, `tests/test_smc/test_state_profile.py` (new)

**Interfaces:**
- Consumes: `get_profile`, `PROFILES`.
- Produces:
  - `WatcherState.pair_profile: Dict[str, str]`; `WatcherState.set_profile(key: str, profile_key: str) -> None` (clears `last_setup[key]` and `zone_pinged[key]`, saves).
  - `db.py`: `"profile_key"` appended to `SIGNAL_COLUMNS`, in `CREATE TABLE`, and in the migration tuple.
  - `TelegramCommandBot` gains `on_set_profile: Callable[[str, str], None]` and `pair_profiles: Callable[[], Dict[str, str]]`.

- [ ] **Step 1: Write the failing DB migration test**

Add to `tests/test_smc/test_db.py`:

```python
def test_signal_profile_key_column(tmp_path):
    from app.services.smc.db import Database, SIGNAL_COLUMNS
    assert "profile_key" in SIGNAL_COLUMNS
    db = Database(str(tmp_path / "t.db"))
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(signals)")}
    assert "profile_key" in cols
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_smc/test_db.py -k profile_key -v`
Expected: FAIL.

- [ ] **Step 3: Add the column + migration**

In `app/services/smc/db.py`:
- Append `"profile_key",  # conservative | aggressive` to `SIGNAL_COLUMNS`.
- Add `profile_key TEXT` to the `CREATE TABLE signals` body (after `alert_text TEXT`).
- Add `("profile_key", "TEXT"),` to the migration tuple (lines 135-139).

- [ ] **Step 4: Persist it in the journal**

In `app/services/smc/journal.py` `record`, add to the `signal` dict (after `"alert_text": None,`):

```python
            "profile_key": getattr(result, "profile_key", "conservative"),
```

- [ ] **Step 5: Run the DB test**

Run: `pytest tests/test_smc/test_db.py -v`
Expected: PASS.

- [ ] **Step 6: Write the state test**

Create `tests/test_smc/test_state_profile.py`:

```python
from app.services.smc.db import Database
from app.services.smc.state import WatcherState


def test_set_profile_persists_and_clears_dedup(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    state = WatcherState(db)
    state.last_setup["ETHUSD"] = "some-fingerprint"
    state.zone_pinged["ETHUSD"] = True
    state.save()

    state.set_profile("ETHUSD", "aggressive")
    assert state.pair_profile["ETHUSD"] == "aggressive"
    assert "ETHUSD" not in state.last_setup
    assert state.zone_pinged.get("ETHUSD", False) is False

    # survives a reload
    state2 = WatcherState(Database(str(tmp_path / "s.db")))
    assert state2.pair_profile["ETHUSD"] == "aggressive"
```

- [ ] **Step 7: Run to verify it fails**

Run: `pytest tests/test_smc/test_state_profile.py -v`
Expected: FAIL (`pair_profile`/`set_profile` missing).

- [ ] **Step 8: Implement state**

In `app/services/smc/state.py`:
- In `__init__` after `zone_pinged`:

```python
        self.pair_profile: Dict[str, str] = db.kv_get("pair_profile") or {}
```

- In `save`, add:

```python
        self.db.kv_set("pair_profile", self.pair_profile)
```

- Add method:

```python
    def set_profile(self, key: str, profile_key: str) -> None:
        """Set a pair's strategy profile and clear its dedup so the new
        profile's first alert is not suppressed by a stale fingerprint."""
        key = key.upper()
        self.pair_profile[key] = profile_key
        self.last_setup.pop(key, None)
        self.zone_pinged.pop(key, None)
        self.save()
```

- [ ] **Step 9: Run the state test**

Run: `pytest tests/test_smc/test_state_profile.py -v`
Expected: PASS.

- [ ] **Step 10: Config default**

In `app/core/config.py` `SMCSettings`, add:

```python
    default_profile: str = Field(
        default="conservative",
        description="Default strategy profile for a pair with no explicit "
        "choice: conservative | aggressive",
    )
```

- [ ] **Step 11: `_build_engine` reads the pair's profile**

In `smc_watcher.py`, change `_build_engine(instrument)` to `_build_engine(instrument, profile_key)` — but the cleaner change (fewer call sites break) is to look the profile up inside `Watcher` where `state` is available. Make `_build_engine` take an optional profile:

```python
def _build_engine(instrument: Instrument, profile=None) -> TripleSyncEngine:
    from app.services.smc.profiles import CONSERVATIVE
    fetcher = _build_fetcher(instrument)
    smc = settings.smc
    return TripleSyncEngine(
        instrument=instrument,
        min_rr=smc.min_rr,
        risk_pct=smc.risk_pct,
        deposit=smc.deposit,
        enforce_sessions=smc.enforce_sessions,
        profile=profile or CONSERVATIVE,
        fetcher=fetcher,
    )
```

Then at the three call sites inside `Watcher` (`check_pair`, `_send_pair_plan`, `_live_status`), resolve the profile:

```python
        from app.services.smc.profiles import get_profile
        profile = get_profile(
            self.state.pair_profile.get(key, settings.smc.default_profile)
        )
        engine = _build_engine(instrument, profile)
```

(In `_send_pair_plan`/`_live_status` the local variable is `key`/`instrument.key`.)

- [ ] **Step 12: Add the `/strategy` command to the bot**

In `app/services/smc/telegram_bot.py`:
- Constructor: add params `on_set_profile: Optional[Callable[[str, str], None]] = None` and `pair_profiles: Optional[Callable[[], Dict[str, str]]] = None`; store them.
- `HELP_TEXT`: add `"/strategy — switch a pair between 🛡 Conservative and ⚡ Aggressive\n"`.
- `_setup_bot_profile` commands list: add `{"command": "strategy", "description": "Conservative / Aggressive per pair"}`.
- `_handle_command`: add

```python
        elif command == "/strategy":
            if not self.on_set_profile:
                await self.send("Strategy switch is not available.")
            else:
                await self.send(
                    "Strategy per pair (tap to toggle):",
                    reply_markup=self._strategy_keyboard(),
                )
```

- Add keyboard builder:

```python
    def _strategy_keyboard(self) -> Dict:
        from app.services.smc.profiles import get_profile, AGGRESSIVE
        profiles = self.pair_profiles() if self.pair_profiles else {}
        rows = []
        for key in INSTRUMENTS:
            current = get_profile(profiles.get(key, "conservative"))
            rows.append([{
                "text": f"{current.label} {key}",
                "callback_data": f"strat_{key}",
            }])
        rows.append([
            {"text": "🛡 All conservative", "callback_data": "stratall_conservative"},
            {"text": "⚡ All aggressive", "callback_data": "stratall_aggressive"},
        ])
        return {"inline_keyboard": rows}
```

- `_handle_callback`: add handling before the final `answerCallbackQuery`:

```python
        if data.startswith("strat_") and self.on_set_profile:
            key = data[len("strat_"):]
            profiles = self.pair_profiles() if self.pair_profiles else {}
            current = profiles.get(key, "conservative")
            new = "aggressive" if current == "conservative" else "conservative"
            self.on_set_profile(key, new)
            answer["text"] = f"{key}: {new}"
            message = callback.get("message", {})
            if message:
                await self._api(
                    "editMessageReplyMarkup",
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    reply_markup=self._strategy_keyboard(),
                )
            await self._api("answerCallbackQuery", **answer)
            return
        if data.startswith("stratall_") and self.on_set_profile:
            profile_key = data[len("stratall_"):]
            for key in INSTRUMENTS:
                self.on_set_profile(key, profile_key)
            answer["text"] = f"All pairs: {profile_key}"
            message = callback.get("message", {})
            if message:
                await self._api(
                    "editMessageReplyMarkup",
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    reply_markup=self._strategy_keyboard(),
                )
            await self._api("answerCallbackQuery", **answer)
            return
```

- [ ] **Step 13: Wire the bot in `smc_watcher.py`**

In `Watcher.__init__`, the `TelegramCommandBot(...)` call — add:

```python
            on_set_profile=self.state.set_profile,
            pair_profiles=lambda: dict(self.state.pair_profile),
```

- [ ] **Step 14: Show the profile in `/status`**

In `smc_watcher.py` `status_text`, after the `Pairs:` line add:

```python
        from app.services.smc.profiles import get_profile
        profiles_line = ", ".join(
            f"{k} {get_profile(self.state.pair_profile.get(k, settings.smc.default_profile)).label}"
            for k in self.state.pairs
        )
        if profiles_line:
            lines.append(f"Profiles: {profiles_line}")
```

- [ ] **Step 15: Full suite + lint**

Run: `pytest tests/ -v && flake8 app/ tests/ smc_watcher.py`
Expected: PASS.

- [ ] **Step 16: Commit**

```bash
git add app/services/smc/db.py app/services/smc/journal.py app/services/smc/state.py app/services/smc/telegram_bot.py app/core/config.py smc_watcher.py tests/test_smc/
git commit -m "Add per-pair /strategy switch (persisted, clears dedup on change)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: RR-filtered, profile-aware pre-market plan

**Files:**
- Modify: `app/services/smc/plan.py` (`build_plan(min_rr, profile)`, walk targets, no-scenario reason)
- Modify: `app/services/smc/notifier.py` (`format_plan` drops the "below 1:2" marker; add profile tag to `format_result`)
- Modify: `smc_watcher.py` (`_send_pair_plan` passes profile + min_rr)
- Test: `tests/test_smc/test_plan.py`

**Interfaces:**
- Consumes: `find_target_zones`, `find_h1_zone(max_touches=)`, `h4_choch_direction`, `StrategyProfile`, `get_profile`.
- Produces: `build_plan(instrument, h4, h1, m5, min_rr: float, profile: StrategyProfile = CONSERVATIVE, market_closed=False) -> PairPlan`; `PairPlan.note` explains a skipped low-RR scenario.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_smc/test_plan.py`:

```python
from app.services.smc.plan import build_plan
from app.services.smc.profiles import CONSERVATIVE, AGGRESSIVE
from app.services.smc.instruments import get_instrument


def test_plan_skips_scenario_below_min_rr_with_reason():
    inst = get_instrument("ETHUSD")
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    m5 = make_candles([3140, 3138, 3136])
    plan = build_plan(inst, h4, h1, m5, min_rr=99.0, profile=CONSERVATIVE)
    assert plan.scenarios == []
    assert plan.note is not None
    assert "1:" in plan.note  # states the achievable RR


def test_plan_scenario_meets_min_rr_when_target_exists():
    inst = get_instrument("ETHUSD")
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    m5 = make_candles([3140, 3138, 3136])
    plan = build_plan(inst, h4, h1, m5, min_rr=1.5, profile=CONSERVATIVE)
    for s in plan.scenarios:
        assert s.rr >= 1.5
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_smc/test_plan.py -k "min_rr" -v`
Expected: FAIL (`build_plan` has no `min_rr`/`profile` params).

- [ ] **Step 3: Rework `_scenario` to walk targets**

In `app/services/smc/plan.py`, change imports to `find_h1_zone, find_target_zones, h4_choch_direction, detect_trend` and rework `_scenario`:

```python
def _scenario(
    instrument, h1, h4, direction, price, speculative, min_rr, profile,
):
    """Project a conditional setup; walk targets outward until RR >= min_rr."""
    zone = find_h1_zone(h1, direction, max_touches=profile.max_zone_touches)
    if zone is None:
        return None, None
    if direction == Direction.LONG:
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

    targets = find_target_zones(h1, direction, entry) + find_target_zones(
        h4, direction, entry
    )
    best_rr = 0.0
    for target in targets:
        tp = target.bottom if direction == Direction.LONG else target.top
        rr = abs(tp - entry) / risk
        best_rr = max(best_rr, rr)
        if rr >= min_rr:
            d = instrument.price_decimals
            return PlanScenario(
                direction=direction, entry=round(entry, d),
                stop_loss=round(stop, d), take_profit=round(tp, d),
                rr=round(rr, 2), zone_bottom=round(zone.bottom, d),
                zone_top=round(zone.top, d), speculative=speculative,
            ), None
    reason = (
        f"Zone {'Demand' if direction == Direction.LONG else 'Supply'} "
        f"{zone.bottom:.{instrument.price_decimals}f}–"
        f"{zone.top:.{instrument.price_decimals}f} is live, but the nearest "
        f"target gives 1:{best_rr:.1f} — waiting for other structure"
    ) if best_rr > 0 else None
    return None, reason
```

- [ ] **Step 4: Rework `build_plan`**

```python
def build_plan(
    instrument, h4, h1, m5, min_rr, profile=None, market_closed=False,
):
    """Build the pre-market plan; only scenarios with RR >= min_rr are shown."""
    from app.services.smc.profiles import CONSERVATIVE
    profile = profile or CONSERVATIVE
    price = round(m5[-1].close, instrument.price_decimals) if m5 else 0.0
    trend = detect_trend(h4)
    plan = PairPlan(
        pair=instrument.key, price=price,
        price_decimals=instrument.price_decimals, h4_trend=trend,
        market_closed=market_closed,
    )
    if market_closed:
        plan.note = "Market closed (weekend) — no plan"
        return plan

    directions = []
    if trend == Trend.UP:
        directions = [(Direction.LONG, False)]
    elif trend == Trend.DOWN:
        directions = [(Direction.SHORT, False)]
    elif profile.allow_h4_choch_entry:
        choch = h4_choch_direction(h4)
        if choch is not None:
            directions = [(choch, False)]  # aggressive: first-leg direction
    if not directions and trend == Trend.FLAT:
        directions = [(Direction.LONG, True), (Direction.SHORT, True)]

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
            "No clean zone with RR >= 1:2 yet — wait for structure to form"
        )
    return plan
```

- [ ] **Step 5: Drop the dead "below 1:2" marker**

In `app/services/smc/notifier.py` `format_plan`, remove the `rr_note` lines (172-174) — with the filter every shown scenario is ≥ min_rr. Replace with:

```python
        lines.append(f"   📐 RR ~1:{s.rr:.1f} (approx)")
```

- [ ] **Step 6: Add the profile tag to the alert**

In `notifier.py` `format_result`, after the `<b>{symbol}</b> — Triple Sync + Imbalance` line, append the profile tag when aggressive:

```python
    lines.append(f"<b>{result.symbol}</b> — Triple Sync + Imbalance")
    if getattr(result, "profile_key", "conservative") == "aggressive":
        lines.append("⚡ <b>Aggressive profile</b> — first-leg entry, lower-probability")
```

- [ ] **Step 7: Update `_send_pair_plan` in `smc_watcher.py`**

```python
        profile = get_profile(
            self.state.pair_profile.get(key, settings.smc.default_profile)
        )
        plan = build_plan(
            instrument, data["h4"], data["h1"], data["m5"],
            min_rr=settings.smc.min_rr, profile=profile, market_closed=stale,
        )
```

(`get_profile` import already added in Task 6.)

- [ ] **Step 8: Run plan tests + full suite**

Run: `pytest tests/test_smc/test_plan.py -v && pytest tests/ -v`
Expected: PASS. Existing plan tests that call `build_plan` without `min_rr` must be updated to pass `min_rr=2.0` — update them in this step (they are test callers, not the rule).

- [ ] **Step 9: Lint and commit**

```bash
flake8 app/services/smc/plan.py app/services/smc/notifier.py smc_watcher.py tests/test_smc/
git add app/services/smc/plan.py app/services/smc/notifier.py smc_watcher.py tests/test_smc/
git commit -m "Plan: walk targets to RR>=1:2, profile-aware, drop dead marker

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: Calibrate, document, and open the PR

**Files:**
- Modify: `app/services/smc/profiles.py` (final `fvg_size_factor`)
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Run the funnel on live data**

```bash
python -m scripts.funnel --all --days 45
```

(Needs `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` only if a fetcher import pulls settings; a dummy token works. If Twelve Data key is set it uses that, else Yahoo.) Capture the printed funnel per pair per profile.

- [ ] **Step 2: Present numbers to the owner, choose `fvg_size_factor`**

Report to the owner (in Russian): setups/week per profile per pair, funnel death stages, and the near-miss FVG size spread. **The owner picks the factor.** Update `AGGRESSIVE.fvg_size_factor` in `profiles.py` to the agreed value and remove the PLACEHOLDER comment.

- [ ] **Step 3: Update docs**

- `README.md`: replace the "Roadmap — Aggressive breakout mode (not yet implemented)" section with a "Strategy profiles" section documenting `/strategy`, the two profiles, and `SMC_DEFAULT_PROFILE`. Add `SMC_DEFAULT_PROFILE` to the config table. Note `scripts/funnel.py` under project layout.
- `CLAUDE.md`: add a bullet under "Conventions and gotchas": the engine reads the profile at four points only; `fvg_size_factor` is a multiplier so per-instrument thresholds stay in `instruments.py`; changing a profile clears the pair's dedup keys.

- [ ] **Step 4: Final full run**

Run: `pytest tests/ -v && flake8 app/ tests/ smc_watcher.py`
Expected: PASS.

- [ ] **Step 5: Commit and open the PR**

```bash
git add app/services/smc/profiles.py README.md CLAUDE.md
git commit -m "Calibrate aggressive fvg_size_factor; document strategy profiles

Co-Authored-By: Claude <noreply@anthropic.com>"
git push -u origin feat/aggressive-strategy
gh pr create --base master --title "Aggressive strategy profile + per-pair switch + RR-filtered plan" --body "..."
```

PR body follows `.github/pull_request_template.md`. Summarise: the zone-touch bug fix (likely cause of the zero-alert week), the opt-in aggressive profile, `/strategy`, the RR-filtered plan, and the funnel numbers behind the chosen `fvg_size_factor`.

---

## Self-Review notes

- **Spec §1 profiles** → Task 3. **§2 primitives** → Tasks 1, 4. **§3 engine** → Task 5. **§4 switch** → Task 6. **§5 plan** → Task 7. **§6 calibration** → Tasks 2, 8. **Testing** → each task. **Sequencing** → task order (funnel built in Task 2, calibrated in Task 8).
- **Type consistency:** `zone_touch_span` (Tuple), `find_h1_zone(max_touches=)`, `find_target_zones` + `find_target_zone` wrapper, `h4_choch_direction`, `StrategyProfile` fields, `profile_key`, `set_profile`, `on_set_profile`/`pair_profiles` bot params — all used consistently across tasks.
- **Dependency note:** Task 2's test imports `app.services.smc.profiles`. If executing strictly in number order, either implement Task 3 before Task 2, or run Task 2's non-profile tests first. Recommended order for a subagent: **1 → 3 → 2 → 4 → 5 → 6 → 7 → 8** (profiles before funnel). This keeps each task's tests green when it lands.
