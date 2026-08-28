# Demo autopilot — project audit and execution-layer design

**Date:** 2026-08-28 · **Status:** direction approved by owner (chat Q&A,
2026-08-28); implementation NOT started — this document is the audit verdict
plus the design it leads to.
**Goal:** answer three owner questions — can the bot trade autonomously on
the data it already has; what architecture changes would make it work
better; should it be split into several bots — and record the decisions
taken in the interactive Q&A.

## 0. Owner Q&A, 2026-08-28 (recorded decisions)

Asked interactively in chat; answers are owner decisions unless marked
"proposed".

- **Context answer (load-bearing):** the live losses come mostly from
  trades taken OUTSIDE the bot's signals ("торгую мимо сигналов"), not
  from the signal journal being deeply negative. The first job of any
  autonomy work is therefore *measurement and separation*: an untouched
  system track the owner's hands never touch, versus the owner's
  discretionary track, visible side by side.
- **D18 — target mode: full autopilot on a DEMO account.** The bot places
  and manages its own orders on a practice account, using exactly the
  levels it already computes. 4–8 weeks of forward statistics (real
  spread, real fills) before any live-money decision.
- **D19 — hard risk limits, enforced in code** (for any auto or semi-auto
  mode; these are gates, not hints): **0.5% risk per trade; hard day-stop
  after 2 realized stop-losses; weekly kill-switch at −6R; Rule 9.2
  correlation combinations become blocking for the executor** (they stay
  warnings for the human alerts). Owner picked the recommended preset.
- **D20 (proposed by the assistant, owner delegated the venue choice):**
  **OANDA v20 practice** for the forex pairs — real REST order API, free
  practice account, and its candle history also repairs the lost-replay-
  cache problem (§1 F3). **ETHUSD via an internal paper-broker** with an
  explicit cost model: Binance's futures testnet has fake liquidity, and
  micro-notional live futures is the later option, not the first step.

## 1. Audit verdict

### What stands (keep, do not churn)

The engineering is not the problem. The pure `evaluate()` engine, the
closed-candles-only fetchers, the synthetic-candle test suite (~11k lines
of tests), the owner-decision discipline (D4–D17), quiet mode, the
defensive DB layer — all of it is exactly the base an execution layer
needs. Nothing below proposes touching strategy rules: the rules are law,
and detector mode (2026-08-06) is unchanged — the owner keeps receiving
every alert exactly as today.

### Findings

- **F1 — transaction costs are modeled nowhere.** Not in the engine, not
  in the journal, not in the year replay. Typical M5-FVG stops run
  10–30 pips; a retail spread of 1–1.6 pips plus commission/swap is
  −0.05…−0.15R of drag per trade. The replay's star-tier edge was
  +0.19R/idea *pre-cost* — costs of that size eat 30–80% of it. A live
  account underperforming the replay is the *expected* outcome of this
  gap, before any other explanation is needed.
- **F2 — the fill model is optimistic.** Journal fill = candle touch; no
  queue, no slippage. The conservative same-candle-SL rule compensates in
  the other direction, but nobody has measured the net. Only a real
  (demo) broker track can.
- **F3 — the validation base is off-repo and unrecoverable.** The year
  candle cache and the replay harness (`sn_exit.py`, `year_*`) live on
  the owner's PC (`C:\temp\926_bot_data`); the TwelveData key was
  rotated, so the cache is called "irreplaceable" in the sniper spec.
  Today the project cannot re-validate a strategy change at all. An OANDA
  account provides years of M5/H1/H4 history for free and fixes this
  permanently once a downloader + the harness are ported into
  `scripts/replay/`.
- **F4 — the measured edge is thin and concentrated.** Star tier
  +0.193/+0.188 R/idea (train/test), ~2.7 stars/week across the pool,
  carried almost entirely by ETHUSD; USDJPY's star slice negative on both
  halves; USDCAD contributed no train stars. Any autopilot must report
  per-pair, per-tier **net** R, and capital decisions are per-slice, not
  per-bot.
- **F5 — journal rows don't carry the slicing context.** The engine knows
  `direction_source`, session block, PD state, `room_r`, the sweep label,
  the warnings, but none of it is persisted per signal, so `/stats`
  cannot answer "which slice earns money". `SIGNAL_COLUMNS` + the
  migration list make this a cheap, safe extension.
- **F6 — discipline counters are human-shaped.** Rule 10 / Rule 0.2 count
  only `taken` marks (alert buttons) — correct for the owner, unusable
  for an executor. The executor needs its own counters over *realized
  broker fills* (D19), independent of any button.
- **F7 — the news blackout hangs on one third-party mirror**
  (`nfs.faireconomy.media`). Fine for advisory alerts; an autopilot needs
  a fail-closed placement policy: no fresh calendar → no new orders while
  a blackout window *could* be in effect.
- **F8 — single process + SQLite is the right size.** Explicitly not a
  finding to fix. See §5.

## 2. Can it trade autonomously on the data it has? — Yes, with caveats

**Yes, technically.** The strategy is defined on closed M5/H1/H4 candles;
entries are resting limit orders at structural levels; SL/TP are computed
at signal time. That shape is exactly what a 5-minute closed-candle
polling feed supports *by construction*: the latency-critical part (SL/TP
around a live position) is delegated to broker-side bracket orders, which
is non-negotiable in this design — a bot crash must never leave an
unprotected position. What the current data does NOT provide — ticks,
depth — the strategy does not consume.

What is genuinely missing is not data but three code layers: an
**executor** (broker adapter + order state machine), a **hard risk
engine** (D19), and **reconciliation/watchdog** ops. Moving forex data to
OANDA (already an implemented fetcher) additionally provides real bid/ask
at decision time — the first honest spread numbers for F1.

**The caveat that matters:** autonomy is not edge. The evidence today is
a thin, concentrated, pre-cost edge measured on one year — and the live
losses are dominated by off-signal trading anyway. Automating execution
makes the system's results *honest and disciplined*; whether they are
*positive net of costs* is exactly what the 4–8-week demo run (D18)
exists to find out, cheaply, before any live money is risked on it.

## 3. Design — execution layer

New module `app/services/exec/`, same process, driven from the existing
cycle. No second engine run, no strategy fork: the executor consumes the
same `AnalysisResult` the alert path uses, at the point in `run_cycle`
where a setup has passed dedup + discipline and is journal-recorded.

```
app/services/exec/
├── broker.py        Broker interface: place_bracket(setup, size, expiry,
│                    client_tag) / amend_sl / cancel / positions /
│                    transactions — all idempotent by client_tag
├── oanda_broker.py  OANDA v20 practice (then live): limit order with GTD
│                    expiry = session_end_utc (Rule 10 for free), SL/TP
│                    attached server-side
├── paper_broker.py  internal simulator for ETHUSD: live Binance prices +
│                    per-instrument spread/fee/slippage model; also usable
│                    in tests
├── risk.py          D19 gates, checked before every placement: 0.5% per
│                    trade, ≤2 realized SL/day, −6R/week kill, Rule 9.2
│                    correlation block, max concurrent positions, news
│                    fail-closed (F7), global /auto off kill switch
└── reconciler.py    on start + every cycle: DB orders vs broker truth;
                     crash-safe resume; orphan detection
```

Key decisions:

- **Order mapping.** Hybrid exit (trend setups) = two half-size orders
  sharing the SL; on the TP1 fill the remainder's SL moves to break-even
  — a 1:1 mirror of `journal.evaluate_signal`. Range setups (D14): one
  full-size order, single TP at the opposite boundary.
- **Sizing.** From D19's 0.5% and the practice balance, via the existing
  Rule 8 lot math. `SMC_RISK_PCT` stays a *message* hint; the executor
  has its own capped parameter.
- **Tier policy.** `SMC_AUTO_TIER=star` is the capital default (F4), with
  a flag to also place regular-tier at minimum size purely as fill/cost
  telemetry — the star flow alone (~2.7/week) collects statistics slowly.
- **Journal vs orders.** The journal stays the *model* track (reference
  levels, touch-fill). A new `orders` table (keyed by signal id, storing
  venue, broker ids, state, fill prices, spread at placement, realized
  net R) is the *demo* track. Their divergence is a first-class report,
  not a bug: it measures F1/F2 directly.
- **Telegram decoupling.** Today a failed alert send discards the journal
  row. Under autopilot, recording (and order placement) must not depend
  on Telegram delivery: split "record" from "deliver" so a Telegram
  outage cannot orphan or suppress a live order. Executor events (placed,
  filled, TP1, BE, closed, blocked-by-risk) are wanted messages — they
  join the quiet-mode whitelist.
- **Commands.** `/auto on|off|status` (kv-backed kill switch + state),
  plus a weekly auto-report: net R per pair/tier, fill rate, measured
  spread, model-vs-demo divergence, risk-engine events.
- **Ops.** Watchdog (no completed cycle in N minutes → Telegram alarm),
  daily one-line heartbeat, reconcile-on-restart, broker-side brackets
  always. Positions may outlive a session (runner legs) but never sit
  without a server-side stop; weekend gap risk is bounded by D19 sizing
  and reported, not silently accepted.

### 3.2 What deliberately does not change

Strategy rules −1…11 and every owner decision D1–D17; detector-mode
alerts (the owner keeps seeing everything, exactly as now); quiet mode;
`/notify`; the plan/zone/PD subsystems; per-instrument params in
`instruments.py`; the journal's model semantics (it must stay comparable
with its own history).

## 4. Phases and acceptance gates

- **Phase A — prep (small).** OANDA practice account + token; capture
  bid/ask spread at signal time; extend `SIGNAL_COLUMNS` with the F5
  context fields; `/stats` slice views. Port the replay downloader/
  harness into `scripts/replay/` against OANDA history (F3) and re-run
  the year *with costs* — this alone may adjust the roster/tier policy
  before any order is ever placed.
- **Phase B — executor on practice.** USDJPY/USDCAD on OANDA practice,
  ETHUSD on the paper broker; risk engine; reconciler; `/auto`; weekly
  report. Tests: order state machine on synthetic fills, risk gates,
  reconciler crash cases — network-free, same style as the journal tests.
- **Phase C — forward run, 4–8 weeks, hands off.** Pre-agreed go/no-go
  before it starts (proposal, owner to confirm at Phase C start):
  ≥25 star setups pool-wide; star-slice net R > 0; model-vs-demo
  divergence understood (spread/slippage accounted); zero unexplained
  risk-engine breaches; no unprotected-position incidents.
- **Phase D — live micro, only if C passes and the owner says go.**
  0.1–0.25% per trade on a small live account, D19 caps active, scale to
  the 0.5% cap on continued evidence. Any red flag → back to demo.

## 5. Split into several bots? — No.

One user, one account-set, one strategy, SQLite state, one Railway
service: separate services/Telegram bots would add queues, partial-
failure modes, deploy complexity and SQLite write contention, and buy
nothing at this scale. The separation that IS worth having:

- **Module seams inside one process** — detector (exists) / executor +
  risk (new, isolated, own state machine) / notifier — with the executor
  testable without either.
- **Research fully offline** — `scripts/replay/` is never imported by the
  worker; it shares the engine as a library.
- The only defensible second "bot" is cosmetic: a separate Telegram bot
  token so execution messages arrive from a distinct identity. Optional,
  cheap, changes nothing architecturally.

Splitting bots also would not touch the actual loss source the owner
named (off-signal trading). The autopilot addresses it structurally: the
system's P&L stops depending on the owner's hands, and the discretionary
track stays visible separately in `/journal` — two curves, compared
weekly, so the capital decision between them is made on data.

## 6. Explicitly not doing

More pairs; ML/signal "improvements" outside the written rules; tick
infrastructure; a web dashboard; microservices; any strategy relaxation
without replay evidence and an owner decision — "almost valid" still does
not exist in this system.
