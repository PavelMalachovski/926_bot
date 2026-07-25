# Aggressive strategy profile, per-pair switch, RR-filtered plan

Date: 2026-07-25
Base: `origin/master` @ 4940777
Branch: `feat/aggressive-strategy`

## Problem

A full week of live running produced **zero forex alerts**. A line-by-line audit of
the rule funnel found five distinct causes, only one of which is a strategy rule:

1. **`zone_touch_index` returns the LAST touching candle** ([structure.py:203](../../../app/services/smc/structure.py)).
   While price sits inside the H1 zone, `touch` equals the final candle index, so
   `find_choch` scans `range(touch, len(candles))` — a single candle. The core
   trigger of the strategy (an M5 CHoCH *inside* the zone) is invisible until
   price has already left the zone, by which time the impulse FVG is usually
   >50% filled and rejected by Rule 4. **This is an implementation bug, not a rule.**
2. **min FVG = 5 pips on M5 forex** ([instruments.py:38](../../../app/services/smc/instruments.py)).
   A 5-pip *gap* between candle 1 and candle 3 on EURUSD/USDJPY is a news-scale
   impulse; the average M5 range there is 3–6 pips. Compounding irony: such
   impulses cluster on red news, which the −60/+15 blackout excludes.
3. **H4 FLAT → immediate SKIP** ([engine.py:126](../../../app/services/smc/engine.py)).
   `detect_trend` demands confirmed HH+HL / LH+LL and `_break_still_holds`
   downgrades to FLAT on any unreclaimed wick break. In a ranging month FLAT is
   the normal state for forex.
4. **A zone touched once is dead forever** ([structure.py:131](../../../app/services/smc/structure.py)).
   `find_h1_zone` accepts `not tested` only; in chop, zones get pierced constantly.
5. **FVG scope is the session block** ([sessions.py:71](../../../app/services/smc/sessions.py)).
   State resets at 14:00 Prague — a London FVG never carries into New York.

Causes 3–5 are deliberate strategy rules; the owner wants them relaxed behind an
opt-in profile, not removed. Cause 1 is a bug and is fixed unconditionally.
Cause 2 is a threshold to be **calibrated on historical data**, not guessed.

Two secondary defects, confirmed with the owner:

- `/plan` takes the *nearest* untested opposite zone as TP regardless of RR,
  producing scenarios like RR 1:0.1 (observed live on ETHUSD 2026-07-25).
- Switching a pair's profile without clearing its dedup fingerprint would mute
  the first alert of the new profile — the switch would appear broken.

## Decisions (owner, this session)

| Question | Decision |
|---|---|
| What "aggressive" relaxes | All of: H4 CHoCH entry, lower min FVG, first-retest zones, day-wide FVG scope |
| Where the touch-span bug is fixed | **Both** profiles (over-strict implementation, not a rule) |
| Switch granularity | **Per pair**, plus an "all pairs" button |
| Plan RR | Walk targets outward until RR ≥ 1:2; if none qualifies, show no scenario and say why |
| Threshold source | **Measure on 30–60 days of history first**, then choose |
| Untouchable in aggressive | Sessions 08–20 Prague, news blackout −60/+15, min RR 1:2, Rule 0.2, Rule 10, risk 2% (no half-lot) |
| Funnel reporting in Telegram | **Not built** — quiet mode stays; the funnel lives in an offline script |

## Design

### 1. `StrategyProfile` (new: `app/services/smc/profiles.py`)

```python
@dataclass(frozen=True)
class StrategyProfile:
    key: str                    # "conservative" | "aggressive"
    label: str                  # "🛡 Conservative" | "⚡ Aggressive"
    allow_h4_choch_entry: bool  # take direction from an H4 CHoCH when trend is FLAT
    fvg_size_factor: float      # multiplier on Instrument.min_fvg (1.0 = unchanged)
    max_zone_touches: int       # 0 = untested only, 1 = allow one prior retest
    fvg_day_scope: bool         # True = FVG valid all Prague day, False = session block

PROFILES: Dict[str, StrategyProfile]  # "conservative" is the default everywhere
```

`fvg_size_factor` is a **multiplier**, never an absolute threshold: the CLAUDE.md
invariant "per-instrument parameters live only in `instruments.py`" stays intact.
`AGGRESSIVE.fvg_size_factor` stays a placeholder until the calibration step (§6)
produces numbers.

`conservative` = `fvg_size_factor=1.0, max_zone_touches=0, fvg_day_scope=False,
allow_h4_choch_entry=False` — bit-for-bit today's behaviour.

### 2. Structure primitives (`structure.py`)

**`zone_touch_span(candles, zone) -> Optional[tuple[int, int]]`** replaces
`zone_touch_index`. Returns `(start, end)` of the **last contiguous excursion**
into the zone. The engine uses `start` as the search origin for CHoCH and FVG,
and `end` to test whether price is still inside. `zone_touch_index` is removed
(single call site) rather than kept as a deprecated alias.

**`h4_choch_direction(candles) -> Optional[Direction]`** — the last confirmed
pivot whose body was broken and *not* reclaimed, reusing the existing
`_break_still_holds`. Consulted only when `detect_trend` returns FLAT, so the
aggressive profile never argues with a live trend; it only speaks where the
conservative one is silent.

**`find_target_zones(candles, direction, entry) -> List[Zone]`** — all untested
opposite zones beyond entry, sorted by proximity. `find_target_zone` becomes a
thin `next(iter(...), None)` wrapper so existing call sites and tests keep working.

**`find_h1_zone(candles, direction, max_touches=0)`** — `_mark_zone_state` already
computes a touched-and-left flag; extend it to a **count** and accept zones with
`touches <= max_touches`. Invalidation (body close through the far edge) still
kills a zone at any touch count.

### 3. Engine (`engine.py`)

`TripleSyncEngine(..., profile=PROFILES["conservative"])`. The profile is read at
exactly four points:

| Rule | Conservative | Aggressive |
|---|---|---|
| 1 — direction | `detect_trend`; FLAT → SKIP | FLAT → try `h4_choch_direction` |
| 2 — H1 zone | untested only | one prior retest allowed |
| 4 — FVG size | `instrument.min_fvg` | `× fvg_size_factor` |
| 4 — FVG scope | session block | whole Prague day |

`AnalysisResult` gains `profile_key: str`. The alert header and `/plan` carry the
profile tag; `journal.record` persists it (new column via the `db.py` migration
list) so `/stats` can separate the two regimes.

Rules 5–8 (entry, SL, RR ≥ 1:2, sizing) and every Rule 0/9/10 gate are untouched
by the profile.

### 4. Per-pair switch (`/strategy`)

`WatcherState.pair_profile: Dict[str, str]` on the existing SQLite kv, default
`conservative` for every pair — deploying changes nothing until a button is pressed.

Keyboard, modelled on `_pairs_keyboard()`:

```
🛡 ETHUSD — Conservative
⚡ USDJPY — Aggressive
🛡 EURUSD — Conservative
────────────────────────
🛡 All conservative    ⚡ All aggressive
```

On any profile change for a pair, **clear `last_setup[pair]` and
`zone_pinged[pair]`** — a stale fingerprint from the previous profile would
suppress the new profile's first alert and make the switch look broken.

`/status` lists each pair's profile. `_build_engine` reads
`state.pair_profile.get(key, "conservative")`.

### 5. Plan rework (`plan.py`, `notifier.format_plan`)

- `_scenario` walks `find_target_zones(h1) + find_target_zones(h4)` outward and
  takes the first target with `RR >= min_rr`; the H4 structural extreme is the
  final fallback.
- No qualifying target → **no scenario**. The plan states the reason instead:
  `Zone Supply 1860.34–1864.69 is live, but the nearest target gives 1:0.1 —
  waiting for other structure.`
- `min_rr` becomes a required argument of `build_plan` (currently the plan does
  not know about it at all).
- The scenario is built with the pair's profile: aggressive allows one-retest
  zones and projects an H4-CHoCH scenario instead of the FLAT both-way brackets.
- The `⚠️ below 1:2 — likely SKIP` marker is deleted — unreachable under the filter.

### 6. Offline calibration (`scripts/funnel.py`)

Not part of the worker; run by hand. Fetches H4/H1/M5 for 30–60 days, caches the
candles on disk (the Twelve Data free tier is 800 req/day), then replays
`evaluate()` at every M5 close inside session hours for both profiles and all five
pairs. `evaluate()` is already pure, so the replay needs no network and no mocks.

Output:

- funnel deaths per rule (H4 FLAT / no zone / no touch / no CHoCH / FVG too small / RR < 2)
- setups per week per profile per pair
- **size distribution of near-miss FVGs** — the basis for choosing `fvg_size_factor`

The owner picks the threshold from these numbers. A factor that yields 40 setups a
week is spam, and this step surfaces that before deployment rather than after.

## Testing

- `zone_touch_span`: price inside the zone for 8 candles with a CHoCH on the 4th —
  currently missed, must be detected after the fix (regression test for cause 1).
- `h4_choch_direction`: break-and-hold detected; break-and-reclaim returns None.
- Engine: both profiles over the same synthetic candles — aggressive finds a setup
  where conservative returns WATCH/SKIP, for each of the four relaxations.
- **Conservative invariant**: existing `tests/test_smc` fixtures produce identical
  verdicts, except where the touch-span fix legitimately changes them.
- Plan: RR filter walks to a farther target; no-qualifying-target renders the
  explanation line and no scenario.
- `/strategy`: toggle persists, "all pairs" works, dedup keys cleared on change.
- All tests stay network-free (`pytest.ini`, `asyncio_mode=auto`).

## Out of scope

Deliberately excluded: a `/funnel` Telegram command, auto-switching profiles by
market state, half risk in aggressive mode, extended session windows, and any
change to news blackout, correlation limits or the discipline rules.

## Sequencing

1. Fix `zone_touch_span` + regression test (both profiles) — the single highest-value change.
2. `scripts/funnel.py`, measure, choose `fvg_size_factor` with the owner.
3. `profiles.py` + engine wiring + structure primitives.
4. `/strategy` switch and `/status` display.
5. Plan RR filter and profile-aware scenarios.
6. README / CLAUDE.md updates; PR to `master`.

Step 2 gates step 3's threshold, but steps 3–5 can be built with the placeholder
value and calibrated at the end if the data pull proves slow.
