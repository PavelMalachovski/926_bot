# Detector mode — announce the setup, do not prescribe the trade

Date: 2026-08-06. Owner decision, taken in chat 2026-08-05/06.

## Why

The watcher currently decides whether a trade is worth taking and stays silent
when the arithmetic does not work. Over a year of ETHUSD, 540 M5 closes on the
conservative profile died at `rr_low` — the structure was complete, the
imbalance was there, and the bot said nothing because the nearest unswept
liquidity did not give 1:1.

The owner does not want that decision made for him:

> Мне главное, чтобы он оповестил о готовом сетапе, ликвидности. Просто чтобы
> я видел, что можно ставить лимитный ордер и стоп за снятую ликвидность.

So the bot becomes a **detector**. It announces that a full Triple Sync +
Imbalance has formed in an H1 zone and shows the three levels the owner needs
in order to act: where the limit order goes, where the stop goes, and what
liquidity lies ahead. He chooses the entry, the stop and the target himself.

This does not relax any strategy rule. Rules −1 through 11 are unchanged in
what they *detect*. What changes is which of them are allowed to *suppress a
message*.

## 1. Gates become labels

Three checks currently return `Verdict.SKIP` after a setup has fully formed.
All three stop suppressing and start annotating. Nothing is deleted — the
computation stays, the thresholds stay, only their consequence changes.

| Check | Today | In detector mode |
|---|---|---|
| `rr < min_rr` | SKIP, no message | send, labelled `⚠️ RR 1:0.6 — below your 1:1` |
| `gap > max_entry_gap_r × risk` | SKIP, no message | send, labelled `⚠️ price has run 1.2R past the imbalance` |
| no unswept liquidity beyond entry | SKIP, no message | send, labelled `⚠️ no unswept liquidity ahead` |

`SMCSettings.min_rr` and `SMCSettings.max_entry_gap_r` keep their names and
their env variables. Their descriptions change from "skip below this" to
"warn below this". Setting either to `0` / a large value simply removes the
corresponding warning.

Everything **before** the setup is complete still suppresses, exactly as now:
off-session, market closed, news blackout, no H4 direction, no H1 zone, price
not in the zone, zone invalidated, no M5 CHoCH, no valid FVG, no confirmed
geometry (`risk <= 0`, `reward <= 0`). Those are not opinions about a trade —
they mean there is no setup to announce.

## 2. The alert

The message answers three questions in the order the owner asks them: what is
the zone, where does the limit go, where does the stop go.

```
🚨 SETUP READY — ETHUSD  ·  LONG  ·  H4 uptrend

📍 H1 Demand zone      3131.00 – 3138.00
⚡ M5 imbalance (FVG)   3138.00 – 3140.50   ← limit order
🛑 Swept liquidity      3126.00             ← stop behind the wick
🎯 Unswept liquidity    3221.00             ← target  (H1 swing high, EQH x2)

⚠️ price has run 1.2R past the imbalance

   ref · RR imbalance edge → target: 1:4.8
   ref · FVG 2.50 wide, 12% filled  ·  session: New York
```

Rules for the layout:

- **Four actionable lines**, in the order the owner works: zone, limit, stop,
  target. He trades all four — the take-profit is always the liquidity, not a
  suggestion he might ignore, so it belongs here and not under `ref`.
- The stop line says "behind the wick" because the swept level is the extreme
  of the excursion, which is the wick that ran the stops — that is what
  `structure.sweep_extreme` returns and what the owner actually uses.
- Everything under `ref ·` is measured context, not instruction. RR is a
  consequence of the four levels above, not an input to them.
- **The header names where the direction came from.** With a real H4 trend it
  reads `H4 uptrend` / `H4 downtrend`. On the aggressive profile, when the H4
  trend is FLAT and the direction came from an unreclaimed H4 CHoCH, it reads
  `⚠️ H4 flat — direction from CHoCH (first leg, not with-trend)`.

  This exists because of a stated owner constraint: *"я всегда работаю по
  тренду, контр тренд не надо."* The conservative profile already satisfies it
  — it requires HH+HL or LH+LL. The aggressive profile does not, and the owner
  chose (2026-08-06) to leave the profile as it is rather than change it. The
  header line is the guard: if he ever switches a pair to aggressive, the
  first-leg entries announce themselves instead of arriving silently.
- Warnings sit between the two blocks, so they are impossible to miss but do
  not push the levels down.
- `entry_is_market` (price currently inside the FVG) adds a line
  `▶️ price is inside the imbalance right now` above the warnings — it is
  information about urgency, not a different verdict.
- Every dynamic value passes through `notifier.escape_html`. The `⚠️` lines
  contain measured numbers and therefore are dynamic.

`Verdict.APPROVED_LIMIT` / `APPROVED_MARKET` remain as the two approved
verdicts; the distinction still drives the `▶️` line and the chart. No new
verdict is introduced.

## 3. Volume control

This is the one thing that can make the feature worse than useless. Removing
the RR gate admits every completed setup, and the alert dedup key currently
includes the entry price (`smc_watcher._setup_fingerprint`), so each new FVG
inside the same zone re-alerts.

**Dedup moves from the entry to the zone.** The fingerprint becomes

```
{symbol}:{direction}:{zone.bottom}-{zone.top}:{session}:{day}
```

One announcement per zone per session block. A second imbalance forming in the
same zone during the same session is the same trading idea — the owner has
already placed his order or decided not to.

`SMC_ZONE_PING` (the "price reached the zone" heads-up, currently off) stays
off and stays independent. Detector mode fires on the completed setup, not on
the touch.

The measured alert rates that justify this choice, and the fallback if the
rate is still too high, are recorded in §8 — that measurement gates the
implementation.

## 4. Journal and statistics

The journal keeps recording every announced setup and tracking the reference
entry/SL/TP, because that is the only outcome signal the bot has. But those
levels are now the bot's reference, not the owner's actual trade — he sets his
own. `/stats` must therefore say so in one line, e.g.

> Tracked against the bot's reference levels, not your actual orders.

Real trade performance already comes from the MT4-screenshot journal
(`/journal`), which is the correct source for it and is unaffected.

Rule 10 is unchanged: a pending order expires with its session, and the
journal marks it expired. The owner confirmed he wants that kept.

Discipline (Rule 10 re-entry bans, Rule 0.2 daily stop) still suppresses
alerts and is still driven by the ✅/❌ buttons only. Detector mode does not
touch it.

## 5. Funnel

`rr_low`, `entry_stale` and `no_liquidity` stop being terminal stages — the
setups they described now reach `approved`. `scripts/funnel.py::classify_result`
keeps the labels for diagnostics but they move to a second counter, so the
funnel can still answer "how many announced setups carried a warning" without
implying they were rejected.

## 6. Pre-market plan

`/plan` is a projection made before price reaches the zone and is unaffected by
this change. It keeps its own `min_rr` filter: a plan is a shortlist the owner
reviews in the morning, not an alert he must act on. This asymmetry is
deliberate — do not "fix" it later.

## 7. What does not change

Session windows, news blackouts, discipline, Rule 10 order life, the H4/H1/M5
detection chain, the sweep-extreme stop, liquidity detection, strategy
profiles and their four decision points, chart rendering, `/plan`.

## 8. Measurement that gates this work

Before implementing, measure the announced-setup rate per week per pair per
profile over the cached year, for: both thresholds off (detector), RR only,
gap only, and both on (today). Method: one engine pass per pair/profile with
both thresholds disabled, recording `(approved, rr, gap_r)` per in-session M5
close, then recomputing the rising-edge distinct-setup count per threshold
pair offline — both thresholds reject without altering entry/SL/TP, so ten
passes suffice instead of forty.

**Replay at production history depth, not archive depth.** `data.py:67-69`
fetches 300 H4 (~50 days), 400 H1 (~17 days) and 400 M5 (~33 h). A replay fed
the full cached year at every step inflates both halves of the alert decision:
`detect_trend` resolves a trend more often on more pivots, and
`find_liquidity` accumulates far more unswept levels than the bot can ever
see. Measured directly — an uncapped 90-day ETHUSD replay produced 18
conservative setups where a 59-day funnel run, whose H4 history happened to be
~354 bars and therefore close to production, produced 3. Cap every
higher-timeframe slice to the production limits or the rate is fiction.

The post-hoc threshold shortcut itself is sound and was verified rather than
assumed: a genuinely gated engine over the same 90-day window produced 18/35
distinct setups against the shortcut's 19/38, the difference being the warmup
start offset.

Reference point: on ETHUSD over 59 days with both gates on, conservative
produced 3 distinct setups and aggressive 16.

**Acceptance:** if zone-keyed dedup keeps the conservative rate under roughly
one announcement per pair per day, implement as specified. If it does not, the
fallback is to keep the zone key but additionally suppress a repeat
announcement for the same zone across sessions on the same day, and to report
the resulting rate before proceeding. Do not implement on an unmeasured rate.

## 9. Testing

- a setup whose RR is below `min_rr` is announced, with the warning present
- a setup whose price has run past `max_entry_gap_r × risk` is announced, with
  the warning present and the measured distance in it
- a setup with no unswept liquidity ahead is announced, with that warning, and
  its reference target lines are absent rather than showing a bogus level
- a setup with none of the three conditions is announced with no warnings
- an incomplete setup (no CHoCH, no FVG, price outside the zone) is still
  silent — the pre-setup suppressions are untouched
- two different imbalances in the same zone in the same session produce one
  announcement; the same zone in the next session block produces a second
- every rendered alert passes the existing `_assert_valid_telegram_html` check,
  including one carrying all three warnings at once

Tests stay network-free and build candles via `tests/test_smc/helpers.py`.

## 10. Out of scope

- Changing what counts as a valid setup. The detection chain is law.
- Removing or weakening discipline rules.
- Any change to `/plan`'s own filtering.
- Win-rate optimisation. Measured separately and settled: no subgroup filter
  survived validation, and the take-profit that maximises expectancy (≈2.0–2.4R)
  lowers win rate to 43–49%. That analysis is advice to the owner as a trader,
  not a bot setting, and detector mode makes it moot for the bot.
