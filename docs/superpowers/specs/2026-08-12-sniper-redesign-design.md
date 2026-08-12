# Sniper redesign — Triple Sync + Imbalance, quality-over-quantity

**Date:** 2026-08-12 · **Status:** approved by owner (chat, 2026-08-12)
**Goal:** fewer, fatter setups — order-block entries, hybrid 2R + runner exit,
a loud ⭐ "sniper" alert tier gated by sweep + premium/discount, everything
validated on the cached year of candles before any live change.

Grounding data: `C:\temp\926_bot_data\reports\` — `eth-year-replay.md`,
`forex-year-replay.md`, `gate-sweep-report.md`. Key findings this design
rests on:

- Paired same-entry exit test (ETH year): flat 2.0R beats the liquidity TP in
  all 8 cells (+0.37R vs +0.24R per trade). The entries are good; the nearest-
  liquidity exit is the drag (median RR 1.3, pool sits 1:3–1:4 of stop width).
- EURUSD loses under every tested rule over the year (−29…−47R, t to −3.3,
  both profiles). GBPUSD negative on aggressive. USDJPY positive under every
  liquidity arm; USDCAD moderate plus.
- Aggressive profile loses on forex (−35.76R / 327 trades) and is worse than
  conservative on ETH. Conservative everywhere.
- The year's R is carried by a handful of huge winners (USDJPY 2026-07-31
  +29.76R at RR 7.44; 2025-09-29 +21.84R; GBPUSD 2025-10-13 +19.60R) — the
  "rare but fat" distribution the owner wants is what the data already shows.
- 1:3–1:4 fixed targets are OUTSIDE the tested range (1.0/2.0/2.5R tested;
  2.0R best). Break-even WR at 1:3 is 25%; observed WR on far targets 31–36%.
  Plausible, unproven — hence the replay gate.

## 1. Roster and profile

- `SMC_PAIRS` → **ETHUSD, USDJPY, USDCAD**. EURUSD and GBPUSD removed.
- Profile: **conservative** on all three. No aggressive anywhere.

## 2. Entry (Rule 5 change)

- Limit order at the **M5 order block** when `find_order_block` locates one in
  the zone-touch→FVG window (today it is the "deeper second entry" info line;
  it becomes the primary entry).
- Fallback: FVG entry exactly as today when no OB is found.
- Stop unchanged: Rule 6, behind the swept extreme + per-instrument buffer.
- Deeper entry + same stop reference + same target ⇒ mechanically higher RR.
  Cost: lower fill rate (already ~30–40% of alerts expire unfilled); the
  replay must report the fill-rate delta.

## 3. Exit (Rule 7 change) — hybrid

- **TP1 = 2.0R for half the position** (the data-backed core).
- After TP1 fills: **stop to break-even** on the remainder.
- **Runner: second half to a fixed multiple, default 3.5R.** The replay sweeps
  3.0 / 3.5 / 4.0 and the shipped default is chosen from that table.
- Worst case after TP1 = **+1.0R** (half banked 2R, half stopped at BE).
  A full −1R loss is only possible before TP1.
- Journal gains partial-close semantics:
  `pending → open → tp1 → (runner_tp | be)` plus the existing `sl`/`expired`.
  Final trade R = 0.5 × 2.0 + 0.5 × (runner result in R).
  Same-candle ambiguity stays conservative (TP and SL in one candle = SL;
  TP1 and SL in one candle = SL before TP1).

## 4. Two alert tiers

**⭐ Sniper** — full alert (pin, chart PNG, Took/Skipped buttons). ALL three
conditions required:

1. **Projected RR ≥ 2.5**, measured from the OB entry to the runner target.
2. **Liquidity sweep at the touch**: the zone tap's wick took out a concrete
   pool — PDH/PDL, Asia-session high/low, EQH/EQL, or an unswept swing from
   `liquidity.py` — at or immediately before the touch. Sweep detection stays
   wick-based with the raw per-instrument `min_fvg` tolerance.
3. **Premium/Discount**: dealing range = last confirmed H1 swing low↔high
   (fractal-5, 2-closed-candle confirmation). Longs only when the entry sits
   in the lower half; shorts only in the upper half.

**Regular setup** — short, quiet message: no pin, no chart, and a one-line
note of which ⭐ conditions were missed. Detector mode survives: nothing that
completes is hidden; attention is spent only on the fat ones.

Info lines in both tiers (NOT filters): zone displacement quality, strict-H4
trend flag (direction came from H4 itself vs the H1 fallback).

## 5. Validation — replay gate

Nothing ships to the live bot until it has run on the cached year
(`C:\temp\926_bot_data\candles\` — irreplaceable, the TwelveData key is
rotated; treat as read-only input).

1. Extend the existing `year_*` replay harness: OB entry, hybrid exit with
   partial close, sweep condition, P/D filter.
2. Run the full year on ETHUSD + USDJPY + USDCAD, **idea-counting** (one
   alert per (direction, entry, day)) as the headline scheme.
3. Report: ⭐ frequency per week, fill rate vs current, WR, total/avg R vs the
   shipped `cur075` baseline, runner sweep 3.0/3.5/4.0, and how many
   historical winners each ⭐ condition kills.
4. Green-light criterion: ⭐ tier positive on idea-counted R with a livable
   frequency (guide: ≥ ~1 setup per 2 weeks across the three-pair pool).
   Below that, come back with numbers and decide which condition to relax.
5. Discipline: parameter choices (runner multiple, any thresholds) are picked
   on the first half of the year and confirmed on the second half —
   out-of-sample, per the standing criticism in the year reports.

## 6. Explicitly unchanged

Sessions/blocks, news blackouts, discipline (Rule 10 / Rule 0.2, taken-marks
only), alert dedup, Rule 9/0.4 warnings, plan-centric zone alerts, quiet
mode, closed-candles-only fetchers, per-instrument params in
`instruments.py`, HTML escaping rules, chart-never-blocks-alert.

## 7. Honest caveats (recorded in the spec on purpose)

- Sweep and P/D conditions have never been replayed on our history; they may
  cut good trades. The replay quantifies that before anything ships.
- OB entries will lower the fill rate; the replay quantifies by how much.
- One year ≈ one market regime; the replay gate protects against obvious
  mistakes, not against the future.
- Maximum-profit parameter mining on this same year is overfitting and is
  explicitly out of scope; any optimisation runs train-on-H1/test-on-H2.

## Implementation order

1. Replay harness extension + year run + report (analysis only, no bot code).
2. Owner reviews the numbers; runner multiple and any relaxations decided.
3. Bot implementation behind the replay's verdict: engine (entry/exit),
   journal partial-close semantics, two-tier alerting, roster change.
