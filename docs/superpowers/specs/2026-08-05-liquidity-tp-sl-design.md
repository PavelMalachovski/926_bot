# Liquidity-based take-profit and sweep-based stop — design

Date: 2026-08-05. Approved by the owner in chat.

## Why

The bot currently sends a setup only when a fixed 1:2.5 take-profit fits
(`SMC_TP_RR`, engine.py Rule 7, PR #45 of 2026-07-30). The RR in an alert is
never computed — it is assigned. The owner wants the opposite: a structural
take-profit at the nearest **unswept liquidity**, a stop behind the **swept**
liquidity, and any setup whose real RR reaches at least 1:1.

Owner decisions taken in chat on 2026-08-05:

- TP = nearest unswept liquidity level; RR computed from it, threshold ≥ 1:1.
- Liquidity = unswept swing high/low **and** equal highs/lows (EQH/EQL).
- Liquidity scanned on M5, H1 and H4 — nearest level wins.
- EQH/EQL tolerance is per-instrument, equal to `Instrument.min_fvg`.
- SL = extreme of the sweep excursion, not the last fractal pivot.
- Below the RR threshold the setup is skipped (no alert).
- Limit vs market alerts stay exactly as they are.

The owner was shown the 2026-07-30 replay (ETHUSD, 59 days): structural
zone-chain TP produced −9.3R while a fixed 2R cap produced +14.0R, and the
"untested opposite zone beyond TP" requirement was worth about +13R on its
own. He chose the structural rule with full knowledge of those numbers. The
strategy spec is law; this document records the decision, not a debate.

## 1. New module: `app/services/smc/liquidity.py`

The project has no concept of liquidity — only "untested zone" (an order
block built from a pivot candle). This module introduces it.

```python
@dataclass(frozen=True)
class LiquidityLevel:
    price: float
    is_high: bool
    timeframe: str    # "M5" | "H1" | "H4"
    equal_count: int  # 1 = single swing, 2+ = EQH/EQL cluster
    timestamp: datetime
```

### Detection

1. Swings come from the existing `structure.find_pivots` (fractal-5, two
   closed confirmation candles). No new swing logic is written.
2. **Sweep test is wick-based, not body-based, and carries the same
   tolerance as clustering.** A pivot high is swept when any later candle
   prints `high > pivot.price + tolerance`; a pivot low is swept when any
   later candle prints `low < pivot.price - tolerance`. Two reasons:
   liquidity is taken by a wick, not a body close (so this deliberately
   differs from zone invalidation); and a bare `>` would make EQH/EQL
   impossible to detect — a second high 20 cents above the first would
   "sweep" it and leave a lone level instead of a pool. A poke smaller than
   the instrument's minimum FVG has not taken the pool.
3. **EQH/EQL clustering.** Unswept pivots of the same kind whose prices sit
   within `tolerance` of each other form one cluster. `tolerance` is the raw
   `Instrument.min_fvg` (ETH $2, forex 5 pips) — no new per-instrument
   constant is introduced. It is deliberately **not** the profile-scaled
   `_effective_min_fvg`: liquidity is a property of the chart, not of how
   aggressively the owner is trading it.
4. **Cluster price is the near side** — the edge price will meet first. For a
   cluster of highs that is the lowest high in the cluster; for a cluster of
   lows, the highest low. `equal_count` records the cluster size and is shown
   in the alert.

### Public API

```python
def find_liquidity(
    candles: List[Candle], timeframe: str, tolerance: float
) -> List[LiquidityLevel]: ...

def nearest_liquidity(
    levels: List[LiquidityLevel], direction: Direction, entry: float
) -> Optional[LiquidityLevel]: ...
```

`nearest_liquidity` keeps only levels strictly beyond `entry` in the trade
direction (highs above entry for a long, lows below for a short) and returns
the closest one. Ties between timeframes are broken by the larger
`equal_count`, then by the higher timeframe — a cluster is the better target.

## 2. Rule 6 — stop behind the sweep extreme

`engine.py` currently anchors the stop to `last_protective_pivot` — the last
confirmed M5 fractal pivot at or before the CHoCH. When another, shallower
fractal forms after the sweep, the stop lands above the real swept low.

New rule: the stop is anchored to the **extreme of the zone excursion** — for
a long, `min(low)` over `m5[touch : choch + 1]` where `touch` is the start of
the last contiguous excursion into the H1 zone (`zone_touch_span`) and
`choch` is the CHoCH candle index. Then `SL = extreme − sl_buffer` (long) or
`extreme + sl_buffer` (short).

Consequences, accepted:

- Stops get wider, so RR values get lower.
- The "no confirmed M5 pivot → SKIP" branch disappears: an excursion always
  has an extreme. `last_protective_pivot` loses its only caller and is
  removed together with its tests.

## 3. Rule 7 — take-profit at the nearest unswept liquidity

```
levels = find_liquidity(m5, "M5", tol)
       + find_liquidity(h1, "H1", tol)
       + find_liquidity(h4, "H4", tol)
target = nearest_liquidity(levels, direction, entry)

target is None  -> SKIP "No unswept liquidity beyond the entry"
TP  = target.price - sl_buffer   (long)   /   + sl_buffer   (short)
rr  = abs(TP - entry) / risk
rr < min_rr     -> SKIP "RR 1:0.6 < minimum 1:1 to the nearest liquidity
                         (H1 swing high 3002.00)"
```

The take-profit sits one `sl_buffer` **short of** the level so the trade is
out before the sweep itself.

`TradeSetup` gains a `target: Optional[LiquidityLevel]` field so the alert can
name the objective.

### Removed

- `SMCSettings.tp_rr` and the `tp_rr` engine argument.
- The `has_room` check (an untested opposite zone at/beyond the fixed TP). It
  existed only to compensate for the arithmetic TP; the structural TP is its
  own room check.

### Restored

- `SMCSettings.min_rr`, **default 1.0**, env `SMC_MIN_RR`. `env.example` is
  updated. A stale `SMC_TP_RR` left in Railway is ignored (`extra="ignore"`),
  it will not crash the watcher, but it must be deleted from the service
  variables so nobody later believes it still does something.

## 4. Alert text

`format_result` shows the real RR and names the objective:

```
🛑 SL: 2951.40 | 🎯 TP: 3000.00
📐 RR: 1:1.4  →  H1 swing high 3002.00 (EQH ×2)
```

The `EQH ×N` suffix appears only when `equal_count > 1`. The whole objective
string is dynamic and goes through `escape_html`. Verdict selection
(`APPROVED_MARKET` when price is inside the FVG, otherwise `APPROVED_LIMIT`)
is untouched — the owner confirmed the current behaviour is what he wants.

Skips caused by low RR stay in the logs. Quiet mode is unchanged: Telegram
still receives found setups only.

## 5. Pre-market plan

`plan.py` projects targets with `find_target_zones` and its own `min_rr`
walk. Left as-is, `/plan` and the live alerts would name different objectives
for the same pair.

`_scenario` switches to the same liquidity search: `nearest_liquidity` over
H1+H4 (M5 is meaningless hours before the session — its swings will have been
swept by the time the zone is reached), TP one buffer short of the level,
scenario shown only when `rr >= min_rr`. The preliminary stop stays beyond the
H1 zone extreme per Шаблон B — the live alert still tightens it, and the
existing footnote in `format_plan` already says so.

## 6. Charts

`chart.py` keeps rendering the TP line unchanged — the price is what matters
and the alert text names the level. No new drawing code. Chart rendering must
stay non-blocking for alerts.

## 7. Funnel

`scripts/funnel.py` loses the fixed-TP stage and regains an `rr_low` stage.
`replay_funnel` takes `min_rr` again instead of `tp_rr`.

## Testing

New `tests/test_smc/test_liquidity.py`:

- a pivot high with a later wick above it is swept; a later *body* close
  below an unrelated level does not sweep it
- two highs within `min_fvg` cluster with `equal_count == 2`; two highs
  further apart stay separate
- a cluster's price is its near side (lowest high / highest low)
- `nearest_liquidity` ignores levels on the wrong side of entry and returns
  the closest of the remaining
- tie between an M5 single swing and an H1 cluster at the same price goes to
  the cluster

Engine regressions in `tests/test_smc/test_engine.py`:

- SL sits below the excursion extreme even when a shallower fractal formed
  after the sweep (this is the bug the change fixes — the test must fail on
  the old `last_protective_pivot` code)
- TP equals the nearest unswept liquidity minus the buffer
- a setup whose nearest liquidity gives RR < 1:1 is `SKIP` with the RR reason
- `setup.rr` is computed, not assigned — two different fixtures produce two
  different RR values

Plan tests updated to `min_rr` and liquidity targets. Existing `tp_rr` tests
across `test_engine.py`, `test_improvements.py`, `test_plan.py`,
`test_funnel.py`, `test_multipair.py`, `test_visuals.py`, `test_zone_ping.py`
are rewritten, not deleted — they are callers, not rules.

All tests stay network-free and build candles via `tests/test_smc/helpers.py`.

## Validation before the owner trusts it with money

After implementation, re-run `scripts/funnel.py` and the outcome replay on the
new rule and report the numbers to the owner. This is a separate step and does
not block the deploy — but the owner should see how the new rule scores on the
same ETHUSD window that produced −9.3R for the old structural TP, before
sizing up.

## Addendum, 2026-08-05 — Rule 5.1, the entry staleness gate

The outcome replay of the rule above (ETHUSD, 59 days ending 2026-08-05, a
+14.1% window; the shipped fixed-2.5R rule re-run on the same candles as a
control) found the planned-RR problem fixed — median 1:1.2 conservative /
1:1.9 aggressive against roughly 1:6 before, and 57–67% of filled trades
reaching their target — but **80–90% of the limit orders never filled**,
against 21–25% under the fixed rule.

The cause is structural, not a defect. The gate "is there RR ≥ 1:1 left to the
*nearest* unswept pool?" can only pass once the near pools have been swept —
which is to say, once price has already left the entry. The entry meanwhile
stays anchored to an FVG formed hours earlier. Median distance from market
price to the limit entry at alert time: **$19.60–23.40, against $1.50–5.30**
under the fixed rule.

Owner decision: keep the liquidity rule, remove the dead-limit mechanism.

### The gate

After Rule 6 has produced `risk`, and before Rule 7 spends anything on the
liquidity scan:

```
gap = price - entry   (long)   /   entry - price   (short)
gap > max_entry_gap_r * risk  ->  SKIP
```

`price` is the same close Rule 8 already uses for the market-entry test
(`result.price or m5[-1].close`). A negative or zero gap means price has not
run past the entry — for a long that includes every case where price is still
inside or below the FVG — so the gate is silent there and market entries are
never affected.

`SMCSettings.max_entry_gap_r`, env `SMC_MAX_ENTRY_GAP_R`. Every value is
meaningful: `0` admits market entries only, a large value disables the gate.
There is no off sentinel. The default is chosen by a replay sweep (see below),
not by intuition.

### What the gate does NOT apply to

`plan.py` keeps no such gate, and this asymmetry is deliberate — do not
"fix" it later. The pre-market plan projects a pullback that has not happened
yet: `_scenario` returns `None` unless the zone sits below current price for a
long, so its entry is *always* far below market by construction. That is a
plan, not a stale limit. The gate exists only for a live setup whose FVG has
already formed and which price has since abandoned.

### Choosing the default

Implement the gate with the threshold configurable, then sweep it over the
same 59-day ETHUSD window at 0.25 / 0.5 / 0.75 / 1.0 / disabled, for both
profiles, reporting alert count, fill rate, win rate and total R at each. The
default ships as whatever that sweep supports. Prior expectation from the
replay: the fixed rule's median gap was 0.13–0.47R with a 75–79% fill rate,
so something near 0.5R is the plausible region — but the sweep decides.

A tighter gate trades alert count for fill rate and cannot create edge on its
own. The honest framing is that it stops the bot from posting orders it has
no reason to believe will fill; whether the surviving trades are profitable is
a separate question this window is far too small to answer — every per-trade
mean measured, under either rule, sits within ~1.5 standard errors of zero.

## Out of scope

- Session windows, news blackouts, discipline rules, journal semantics.
- Strategy profiles: `conservative` / `aggressive` keep their four decision
  points untouched. Liquidity detection is profile-independent.
- Partial take-profits / TP1+TP2. The owner chose the single structural TP.
