# Quiet zone alerts, OB+FVG zones of interest, and range trading

Owner decisions of 2026-08-16. Three deliveries, three PRs, in this order.
Delivery 1 ships alone and first — it removes noise the owner is living with
today. Deliveries 2 and 3 follow once 1 is on Railway and quiet.

## Problem

**Noise.** On 2026-08-13 USDCAD sent the identical plan-zone alert four times
(16:20, 16:35, 16:55, 17:25 Prague) for the same zone 1.39448–1.39584, and
ETHUSD twice for 1875.49–1883.18. The owner wants one alert per zone, plus a
button to silence a pair when he has seen enough.

Root cause is in `_maybe_plan_zone_alert` (`smc_watcher.py:1240`). An episode
survives only while the *latest closed M5 candle still overlaps the zone*:

```python
same_episode = (
    p_date == today
    and last.low <= p_high and last.high >= p_low   # <- the hole
    and current is not None
    and current.zone_bottom == p_low
    and current.zone_top == p_high
    and current.direction.value == p_dir
)
```

One candle that does not touch the zone pops the mark. Price hovering on a
zone edge therefore re-arms the alert on every exit and re-entry. The
identical message text confirms this was exit/re-entry on one stable zone,
not a zone replacement.

**Missing strategy coverage.** Two gaps between what the owner trades and
what the bot detects:

- Zones of interest are order blocks *and* imbalances. The bot only ever
  treats an H1 order block as a zone; an FVG is checked later, on M5, inside
  that zone. An H1 FVG standing alone is invisible.
- When H4 and H1 are both flat the bot goes silent. The owner trades that
  case: he marks the range, waits for a boundary, and targets the opposite
  boundary.

## Owner decisions (2026-08-16)

| # | Decision |
|---|---|
| D1 | Zone-alert unit of silence is the **session block**, not the day |
| D2 | Same direction + **any overlap** = the same zone (reverses the 2026-08-11 rule) |
| D3 | Mute button silences **zone alerts of that pair only**; 🚨 setups and Rule 0.4 always pass |
| D4 | H1 zone of interest = OB **or** FVG; **OB always wins** when both are valid |
| D5 | Inside an H1 zone, mark M5 OB and M5 FVG — as detail in the one alert, never a second alert |
| D6 | H4/H1 trend disagreement does not suppress; it is labelled and costs the ⭐ |
| D7 | Range boundaries come from clustered confirmed H1 pivots with ≥2 touches each |
| D8 | Range boundary alert first, full 🚨 only after M5 CHoCH + FVG |
| D9 | A wick through a boundary that closes back inside is liquidity taken — range survives, setup earns ⭐ |
| D10 | An H1 FVG counts as a fresh zone of interest only while price has **not entered it at all** — the exact mirror of an untested order block (owner decision 2026-08-18) |
| D11 | The range is in play only when **both** H4 and H1 read FLAT — the H1-trend fallback of 2026-08-06 keeps precedence over it (owner decision 2026-08-18) |
| D12 | When a range is found, it **replaces** the plan's speculative both-way breakout brackets rather than joining them (owner decision 2026-08-18) |
| D13 | A boundary sweep earns the ⭐ only when it happened in the **current excursion** to that boundary — the same scope every other sweep check in the bot uses (owner decision 2026-08-18) |
| D14 | In range mode there is **no hybrid exit** — one target, the opposite boundary, full size (owner decision 2026-08-18) |
| D15 | A close beyond a range boundary invalidates it only if the break **still holds** at the end of the excursion; pierce-and-reclaim is a sweep, not a breakout (owner decision 2026-08-18) |
| D16 | A pierce that closes beyond a boundary and is reclaimed counts as a **sweep** for the ⭐, not only a wick-only pierce — the follow-through of D15 (owner decision 2026-08-18) |

---

# Delivery 1 — Quiet

## 1.1 Session block

`sessions.py` owns the trading day and must keep owning it. Add:

```python
def session_block(utc_dt: datetime) -> Optional[str]:
    """Stable identity of the session block containing utc_dt, e.g.
    "2026-08-16/Frankfurt-London". None outside trading hours."""
```

Derived from `WINDOWS` — never a hardcoded 14:00. The Prague date prefix
makes the identity unique across days.

## 1.2 Dedup state

`WatcherState.zone_pinged` changes shape:

| | before | after |
|---|---|---|
| value | `[bottom, top, direction, prague_date]` | `List[[bottom, top, direction, block_id]]` |
| meaning | the one zone pinged in this episode | every zone pinged, one entry per alert |

On load, entries whose `block_id` is not the current block are dropped, as
are values of any legacy shape (bool, or the flat 4-element list). The key
self-heals; no DB migration.

`set_profile` keeps clearing the key (`.pop`) — unchanged.

## 1.3 New alert decision

`_maybe_plan_zone_alert` becomes:

1. `zone_ping` disabled, no result, no session, or no M5 candles → return.
2. Pair muted (§1.4) and mute not expired → return.
3. `result.verdict in APPROVED` → return (the 🚨 alert covers this touch).
4. `scenario = planbook.scenario_for_touch(key, last.low, last.high)`; None → return.
5. `_cooldown_left(key)` → return.
6. **Already fired?** For each recorded entry of the current block: same
   direction and `entry_bottom <= scenario.zone_top and scenario.zone_bottom
   <= entry_top` → return.
7. Send, and only on success append `[zone_bottom, zone_top, direction,
   block_id]` and save.

The "price still inside the zone" condition is gone. Nothing re-arms an
alert inside a block — not an exit, not a drifting boundary, not a plan
recompute.

**Why any-overlap and not exact bounds.** The plan is recomputed every five
minutes; a new H1 pivot shifts a zone by a fraction of a pip and the exact
comparison reads it as a different zone. Any overlap is coarse in the safe
direction: a genuinely new zone in the same direction that does not touch
the old one still alerts.

## 1.4 Mute button

`format_zone_alert` gains a keyboard:

```python
def zone_alert_keyboard(pair: str, until_hhmm: str) -> dict:
    # [[{"text": f"🔕 Mute {pair} till {until_hhmm}",
    #    "callback_data": f"zmute_{pair}"}]]
```

- Deadline is computed at press time from `WINDOWS`: inside a block that is
  not the last of the day → that block's end (14:00); inside the last block
  of the day, or outside trading hours entirely → the next trading day's
  open (08:00). Stored as an ISO UTC string in
  `WatcherState.zone_muted: Dict[str, str]` (kv key `zone_muted`).
- The button label shows the deadline that *would* apply at send time; the
  authoritative deadline is computed on press.
- On press the keyboard is replaced with a single inert
  `🔕 Muted till HH:MM` button (`callback_data: "noop"`), matching how
  Took/Skipped already collapse.
- The callback lives in `telegram_bot._handle_callback` next to `aplan_`,
  behind a new `on_zone_mute` hook the watcher wires up.

Mute is checked **only** in `_maybe_plan_zone_alert` and (Delivery 3) in the
range boundary alert. Setup alerts, Rule 0.4 warnings, the 07:45 digest and
the 07:55/13:55 plan snapshots ignore it entirely.

## 1.5 Commands

- `/status` lists active mutes: `🔕 USDCAD till 14:00`.
- `/unmute` clears every mute and confirms which pairs were freed.

## 1.6 Tests (`tests/test_smc/test_zone_dedup.py`)

Regression on the observed production failure first:

- price enters the zone, leaves it, re-enters within the block → exactly one send;
- zone bounds shift by one pip between cycles, overlap holds → one send;
- the block rolls over at 14:00 → the same zone may alert once more;
- a same-direction zone with no overlap → a second send;
- muted pair → no send; deadline passed → send;
- mute does not suppress a 🚨 setup alert or a Rule 0.4 warning;
- `session_block` returns distinct ids for 13:59 and 14:01 Prague, and None at 07:00;
- legacy `zone_pinged` values (bool, flat list) are dropped on load.

## 1.7 Files

`app/services/smc/sessions.py`, `state.py`, `notifier.py`, `telegram_bot.py`,
`smc_watcher.py`, `CLAUDE.md`, new `tests/test_smc/test_zone_dedup.py`.

---

# Delivery 2 — OB and imbalance as zones of interest

## 2.1 H1 FVG as a zone candidate

`fvg.py` already detects and validates imbalances; point it at H1 candles.
Add to `structure.py` (or a thin wrapper next to `find_h1_zone`):

```python
def find_h1_zone(candles, direction, max_touches=0) -> Optional[Zone]
    # unchanged: the order block
def find_h1_fvg_zone(candles, direction, instrument) -> Optional[Zone]
    # the freshest unfilled H1 FVG on the trade's side, as a Zone
def find_zone_of_interest(candles, direction, instrument, max_touches=0)
    # D4: order block if valid, else the FVG
```

Freshness for an FVG mirrors `touches == 0` for an OB exactly (D10, owner
decision 2026-08-18): the gap must be **untouched** — no candle since it
formed has traded into it at all, so `measure_fill` reports zero
penetration. A partially filled gap is not a zone the bot is still waiting
for; price already arrived. This is deliberately stricter than Rule 4's
`fill < 50%` test for the M5 entry imbalance, which judges a gap price is
trading in right now rather than one being waited on. Minimum size is the
per-instrument `min_fvg` scaled by the profile factor, as everywhere else;
never a new hardcoded threshold.

`plan._scenario` and `engine` Rule 2 both switch to `find_zone_of_interest`.
The zone's kind (`OB` / `FVG`) travels on the `Zone` dataclass so messages
and charts can name it. The runner-up zone, when both exist, joins the
existing zone ladder.

## 2.2 M5 detail inside the zone

When price is inside the H1 zone, the alert adds one line naming what the
owner would actually buy from:

```
🔎 5m OB 159.070–159.140 · 5m FVG 159.137–159.174
```

`find_order_block` currently requires the CHoCH index as its upper bound.
For the marking use it is called with the zone-touch span alone, so a block
is visible while the CHoCH is still pending. Its use inside the engine —
the deeper-entry check with its "strictly deeper than entry, strictly inside
the stop" guard — is untouched.

## 2.3 Trend disagreement label (D6)

The alert header gains `H4 UP · H1 UP` or `H4 UP · H1 DOWN ⚠️ counter-hourly`.
`sniper.classify` gains disagreement as a ⭐-denying condition, alongside
room, sweep, premium/discount and staleness. Detector mode is unchanged:
the alert is still sent.

## 2.4 Charts

- Plan chart (H1): both zone candidates drawn and labelled, OB and FVG in
  distinct colours.
- Alert chart (M5): the 5m OB and 5m FVG boxes.
- `chart.py` stays matplotlib-only, no pandas — rendering must never block
  an alert.

## 2.5 Tests

H1 FVG detection and freshness; OB-wins-over-FVG selection; FVG used when no
valid OB exists; M5 OB found before a CHoCH exists; the engine's deeper-entry
guard unchanged; disagreement denies ⭐ but still sends; chart smoke tests.

## 2.6 Files

`fvg.py`, `structure.py`, `models.py` (`Zone.kind`), `plan.py`, `engine.py`,
`sniper.py`, `notifier.py`, `chart.py`, `CLAUDE.md`, plus tests.

---

# Delivery 3 — Range (боковик)

## 3.1 Detection — new module `app/services/smc/range.py`

```python
@dataclass
class Range:
    top: float
    bottom: float
    touches_top: int
    touches_bottom: int
    broken: bool

def detect_range(candles, instrument, window: int = 120) -> Optional[Range]
```

Over the last `window` closed H1 candles, using the same confirmed fractal-5
pivots as everywhere else:

1. Cluster pivot highs within `instrument.min_fvg` of each other; the
   largest cluster's mean is `top`. Same for lows → `bottom`.
   The tolerance is the raw per-instrument `min_fvg`, matching sweep
   detection — never the profile-scaled value.
2. Require `touches_top >= 2` and `touches_bottom >= 2`.
3. Require `top - bottom >= 3 * min_fvg`, else it is chop, not a range.
4. `broken` when an H1 **body** has closed beyond either boundary after the
   cluster formed **and the break still holds** (D15, owner decision
   2026-08-18). A pierce that closes back inside does not break it (D9) —
   it is liquidity taken, and it is recorded as such. The same
   reclaim-aware test governs Rule 3's invalidation of a RANGE zone on M5:
   without it the stop-hunt D9 blesses would kill the setup instead of
   starring it, because the pierce is usually an M5 body close. This
   mirrors `structure._break_still_holds`, which already spares the H4
   trend from a reclaimed fakeout.

Range boundaries coincide with EQH/EQL pools by construction, so this is the
same liquidity hunt applied to a range.

## 3.2 When the range is in play

`detect_trend(h4) == FLAT` **and** `detect_trend(h1) == FLAT` and
`detect_range` returns an unbroken range (D11, owner decision 2026-08-18).

The original wording said H4 alone, and justified itself with "that is
exactly the state in which the bot is silent today". Those two disagree: on
a flat H4 the bot is *not* silent — Rule 1 falls back to the H1 trend (owner
decision 2026-08-06) and trades it. Requiring both timeframes flat makes the
justification true again: the range adds signal exactly where there is none
today, and no existing H1-trend setup is replaced by a boundary trade.

When the range is in play it also **replaces** the plan's speculative
both-way breakout brackets (D12). Concrete boundaries with targets in each
other are strictly better information about the same market state than "if
it breaks up → long, if down → short", and showing both would put two
different plans for one pair in one message.

## 3.3 Alerts

Boundary touch → one message, under Delivery 1's dedup and mute:

```
🔔 USDJPY: price is at the range HIGH 159.478
📋 Plan: SHORT — target the range LOW 158.687 | 🛑 SL 159.55
Watching M5 for a bearish CHoCH + FVG.
```

Then the normal 🚨 on M5 CHoCH into the range plus a valid FVG:

- direction: at the high → SHORT, at the low → LONG;
- entry: the M5 FVG edge, or the deeper M5 OB;
- SL: beyond the boundary + `instrument.sl_buffer`;
- TP: the opposite boundary − `sl_buffer`;
- **no hybrid exit** (D14, owner decision 2026-08-18): one target, the
  opposite boundary, full size. The 2R/runner split is meaningless here —
  risk is anchored beyond the boundary plus a buffer, so RR to the opposite
  boundary is routinely under 2 and TP1 would land *outside* the box the
  setup is aiming at;
- ⭐ when the boundary was swept and reclaimed **in this excursion** (D9,
  scoped by D13, widened by D16). A pierce counts whether it closed beyond
  the boundary or only wicked through it: D15 made the body-close raid
  tradeable, and the same raid is what the owner calls a sweep, so the two
  rules must agree about it.

  The stop follows from the same reading. It must sit beyond **every**
  candle of the raid, not merely beyond the leg price returned on: a raid
  interrupted by a candle trading wholly outside the boundary band splits
  the excursion in two, and anchoring on the later half puts the stop
  inside the liquidity it was meant to hide behind. A pierce from days earlier does not
  count: every other sweep check in the bot is excursion-scoped, and an
  unscoped flag would mark almost every boundary eventually, leaving the ⭐
  with nothing to distinguish.

## 3.4 Charts

Both boundaries as black dashed lines labelled `RANGE HIGH` / `RANGE LOW`,
on the H1 plan chart and the M5 alert chart.

## 3.5 Journal

Range setups are journal-recorded like any other, with `zone_kind="RANGE"`
so `/stats` can separate them from trend setups later.

## 3.6 Tests

Clustering with and without enough touches; the 3× `min_fvg` height floor;
body close beyond a boundary marks `broken`; wick-and-reclaim does not;
range ignored when H4 trends; boundary alert direction; SL/TP geometry;
⭐ on the reclaimed sweep; chart smoke test.

## 3.7 Files

New `range.py`; `engine.py`, `plan.py`, `planbook.py`, `notifier.py`,
`chart.py`, `journal.py`, `db.py` (the `zone_kind` column, added to
`SIGNAL_COLUMNS`, the `CREATE TABLE` and the migration list), `smc_watcher.py`,
`CLAUDE.md`, plus tests.

---

## Out of scope

- Changing the H4-first / H1-fallback direction rule (D6: label, do not gate).
- Any change to the 07:45 news digest or the 07:55/13:55 snapshot mechanics —
  they already hold the plan behind a button, which is what the owner asked
  for.
- Reviving the per-pair `/strategy` picker.
- Replay validation of Delivery 2 and 3 — worth doing, but a separate task
  with its own plan.
