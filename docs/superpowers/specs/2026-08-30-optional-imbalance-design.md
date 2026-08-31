# D22 — the imbalance labels a setup, it no longer defines one

**Date:** 2026-08-30 · **Status:** approved by owner (chat, 2026-08-30)
**Owner's words:** «сила трёх + имбаланс … давай для сигнала уберем
имбаланс. пусть он не будет важным, но будет в сообщении.»

The strategy keeps its three syncs — H4 (or H1) trend → H1 zone of interest
→ M5 CHoCH — and drops the fourth requirement that used to ride with the
third. The imbalance stays visible in every message; it just stops deciding
whether the owner hears about the setup at all.

## 1. What changed

**Rule 4 is no longer a gate.** `select_valid_fvg` still runs first and its
answer still wins when it has one. When it returns nothing, the setup is
announced instead of becoming a WATCH.

**Rule 5 grows an entry ladder** (the three open questions were delegated to
the assistant in the same chat; these are the shipped answers):

1. the proximal edge of the **valid imbalance** — unchanged, so every
   pre-D22 signal keeps byte-for-byte identical levels;
2. else the **M5 order block** of the same touch→CHoCH excursion — the last
   opposing candle before the impulse, which is where price reacts from when
   the impulse left no gap behind it;
3. else **price itself** — a market entry on the CHoCH.

`TradeSetup.entry_source` ("fvg" / "ob" / "market") records the rung. It is
what names the ladder's RR column, picks the `entry_is_market` band, and
decides whether a *deeper* order block is worth advertising (only rung 1 can
have one below it — on rung 2 the block IS the entry).

**A gap that failed Rule 4 never becomes an entry.** Too small, over half
filled, closed through, or from a session that is over: all of those mean
price has already traded through that level, so entering at its edge would
be entering where the market has been. It rides along on `rejected_fvg` /
`rejected_fvg_problems` so the alert can show the band and name the flaw
("✗ too small"), and the chart draws it hatched rather than solid.

**The imbalance becomes the ⭐'s sixth condition** (`sniper.classify`).
A CHoCH with no gap behind it is announced, journal-recorded and traded like
any other setup — it simply cannot star. That also makes the question
answerable: after a month, `/stats` separates what the setups without a gap
actually earned.

**Rule 6 is untouched.** The stop was always measured from the swept extreme
of the excursion, never from the imbalance.

**One new geometry check.** An FVG entry sat on the trading side of its stop
by construction; the new rungs are new geometry, so an entry on the wrong
side of its own stop is now an explicit SKIP next to the existing
`risk <= 0` — malformed, not a judgment call.

## 2. The escape hatch

`SMC_REQUIRE_IMBALANCE=true` restores the pre-D22 gate with no deploy and no
code change, exactly the way `SMC_PD_BASIS=h1` restores the pre-D17 reading.
Default `false`.

## 3. What this costs

Honest ledger, recorded before any live data exists:

- **More signals, thinner on average.** The gap was a real filter; removing
  it necessarily admits setups that would have been filtered. The ⭐ tier is
  where the old bar now lives, and `/notify star` is the one-command way
  back to roughly the old signal count.
- **Rung 3 is the weakest.** A market entry at the CHoCH close has no
  pullback, the widest risk of the three, and therefore the worst RR of the
  three. It exists so the bot never goes silent on a setup it can see, not
  because it is a good price.
- **Untested against history.** The year replay predates this branch and the
  cached candles are off-repo (see the 2026-08-28 audit, F3), so nothing here
  has been replayed. The ⭐ split is what keeps that measurable going forward.

## 4. Explicitly unchanged

Detector mode; the two alert tiers and `/notify`; the H1 zone of interest
(D4) and the range mode (D11-D16); Rule 6 and Rule 7; sessions, news
blackouts and discipline; the journal's lifecycle and semantics; the
per-instrument minimum imbalance in `instruments.py` (still what
`select_valid_fvg` measures against, still profile-scaled).


---

# D23/D24 — the same session's follow-ups (2026-08-31)

The owner described his actual daily loop (07:45 news → 08:00 plan the day
→ wait for the setup → hourly re-check of that plan) and asked how far the
bot already was. Answer: most of it existed; two things did not.

## D24 — plan corrections must reach Telegram

The auto-plan summary was recomputed every five minutes and silently edited
in place on a material change. True, but invisible: nothing told the owner
his plan had moved. The silent edit stays (that message must always show the
truth) and the pairs that actually moved now also get one `🔁 Plan updated`
message naming what moved, throttled to once an hour per pair.

`planbook.plan_snapshot` stores the fingerprint's own facts in a diffable
shape; `describe_plan_changes` turns two snapshots into lines a human reads
("LONG zone moved 2400.00–2410.00 → 2415.00–2425.00", "Now waiting: …").

## D23 — show the counter-H4 setup

From the ETH case the owner brought (2026-08-31): 5m and 1h were clearly
turning up and he was waiting to go long, while H4 read LH+LL and Rule 1 was
looking for a short. The bot could not say anything about the trade he was
actually watching, because the H4 direction was the only one it evaluated.

Now, when H4 and H1 trend opposite ways, a second `evaluate` pass runs with
`force_direction` set to the H1 side, on the candles the first pass already
fetched. Both setups are announced. The counter one is labelled "⚠️ against
H4" and is denied the ⭐ by the existing D6 rule, so the tier still means
what it meant. Nothing the owner already receives is replaced.

**The cost, stated plainly:** counter-trend entries are the ones that lose
when the higher timeframe wins. That is exactly why they can never star,
why only a COMPLETED counter setup is announced, and why `/notify star`
remains the one-command way to see none of them.

## Not built (owner did not select them)

Making the 08:00/14:00 plan loud and unfolded, moving the slots off
07:55/13:55, and an hourly "plan still alive" check-in. All three are small
and remain available.
