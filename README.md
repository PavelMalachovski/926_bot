# SMC Watcher — Triple Sync + Imbalance

A Telegram bot that runs the **Triple Sync + Imbalance** SMC strategy for the
selected currency pairs — every 5 minutes during trading hours (08:00–20:00
Prague), every 15 minutes outside them — and alerts you the moment a valid
setup appears.

- 🚨 **Urgent alert** when a setup is APPROVED — entry / SL / TP / RR / lot
  size, a **dark-style M5 chart PNG** (H1 zone, FVG box, ENTRY/SL/TP levels)
  and **✅ Took it / ❌ Skipped buttons**
- 📌 **Live setup card** — the alert is pinned and edited in place as the
  signal evolves: `📈 Filled @ … → 🎯 TP HIT (+2.1R)`; unpinned on resolution
- 🛡 **Discipline on autopilot** — trades you mark as taken enforce Rule 10
  (no re-entry after a stop in the same session) and Rule 0.2 (two taken
  stops close the trading day: alerts muted until tomorrow)
- 🤫 **Silent otherwise** — checks without a setup only go to the logs, with
  precise reasons («best FVG candidate: 3.2 pips < required 5»); `/check`
  shows the current picture on demand
- 💱 **Pairs are switchable at runtime** via Telegram: `/pairs`
- 📅 **Forex Factory red-news digest** every weekday at 07:45 Prague
  (incl. a session-block breakdown of today's releases)
- 📋 **`/plan`** — an on-demand Pre-Market Plan for any watched pair, folded
  together with the **live checklist status** (projection + where the pair is
  right now); conditional entry/SL/TP/RR + H1 chart, both-way brackets when flat
- 🔔 **Zone-touch ping** — a light "get ready" nudge the moment price reaches a
  live H1 zone, before the full 🚨 setup forms (`SMC_ZONE_PING`)
- 📒 **Signal journal**: every alert is auto-tracked to its TP/SL outcome;
  `/stats` shows signal winrate and your personal (taken) winrate separately

## Supported pairs

| Pair | Data source | Min FVG | Notes |
|---|---|---|---|
| ETHUSD | Binance (no key needed) | $2.00 | 24/7, funding-rate advisory |
| USDJPY | Twelve Data / OANDA / Yahoo | 5 pips | free tier, no key needed by default |
| EURUSD | Twelve Data / OANDA / Yahoo | 5 pips | free tier, no key needed by default |
| GBPUSD | Twelve Data / OANDA / Yahoo | 5 pips | free tier, no key needed by default |
| USDCAD | Twelve Data / OANDA / Yahoo | 5 pips | free tier, no key needed by default |

**Forex data source** (`SMC_FOREX_SOURCE`, default `auto`) resolves in this
order: **Twelve Data** if `TWELVEDATA_API_KEY` is set → **OANDA** if
`OANDA_API_TOKEN` is set → **Yahoo Finance** (keyless, always available).
ETHUSD always uses Binance. Twelve Data is recommended: free 800 req/day,
native 4h/1h/5min candles, runs on Railway; the higher timeframes are cached
so 2–3 forex pairs stay comfortably within the free budget (~200 req/day/pair).
Grab a free key at [twelvedata.com](https://twelvedata.com).

Default watched pairs: **ETHUSD + USDJPY** (change with `/pairs` or `SMC_PAIRS`).

## Telegram commands

Commands are registered in the bot's slash menu (type `/` in the chat).

| Command | What it does |
|---|---|
| `/pairs` | inline keyboard — toggle watched pairs on/off |
| `/status` | enabled pairs, current session, last verdicts |
| `/check` | run the full strategy check right now |
| `/plan` | pre-market plan for a pair (buttons pick from enabled pairs / all) |
| `/stats` | journal: winrate bars, outcome sparkline, personal (taken) stats |
| `/news` | today's red news (Forex Factory) and blackout windows |
| `/help` | command list |

## Red-news filter (Forex Factory)

The official FF weekly JSON feed is fetched every morning (and every ~6h).
Entries are blocked **60 min before** and **15 min after** every high-impact
event: forex pairs react to news for either of their currencies, ETHUSD only
to USD news. A morning digest of today's red news is sent at **07:45 Prague**
(`SMC_NEWS_DIGEST_TIME`; `SMC_NEWS_DIGEST=false` to disable). Rule 0.4: if a journal signal is active
(pending/open) and red news is ≤30 min away, the bot sends a "SL to breakeven /
pull the order" warning. Tunables: `SMC_NEWS_BLACKOUT_BEFORE_MIN` (60),
`SMC_NEWS_BLACKOUT_AFTER_MIN` (15), `SMC_NEWS_ENABLED` (true).

## Signal journal

Every approved setup is recorded and tracked automatically against M5 candles:
pending (limit not reached) → open (entry touched) → **tp / sl** (whichever hit
first; both in one candle counts as sl, conservative). A pending order that
outlives its session becomes **expired** (Rule 10). `/stats` shows counts,
winrate and per-pair breakdown — the data basis for tuning the strategy.

Pressing **✅ Took it** on an alert marks the signal as a real trade: `/stats`
then tracks your personal winrate, and the discipline kill-switches activate —
a taken stop bans re-entry on that pair+direction for the session (Rule 10),
and the second taken stop of the day suppresses all further alerts until
tomorrow (Rule 0.2). Skipped signals never count against the limits.

Pressing **✅ Took it** also **mutes new alerts for that pair for 4 hours**
(`SMC_TAKEN_COOLDOWN_HOURS`) — you are managing the position, not hunting a
second one. The live card for the taken trade keeps updating; `/status` shows
what's muted and for how long.

Signals and runtime state (selected pairs, dedup keys) live in one **SQLite
database** (`SMC_DB_FILE`, default `.smc_watcher.db`; legacy JSON files are
imported automatically). On Railway attach a volume (e.g. mounted at `/data`)
and set `SMC_DB_FILE=/data/smc.db` so entries and pair selection survive
redeploys.

## Strategy checklist (per pair, every 5 min in session)

1. **Session filter** — trading hours 08:00–20:00 Prague (Frankfurt/London
   08–14, New York 14–20): crypto every day, forex Monday–Friday. A closed
   forex market is also detected automatically. All message times are Prague.
2. **H4 trend** — HH+HL / LH+LL with 2-closed-body pivot confirmation;
   a reclaimed fakeout beyond the last HL/LH does not kill the trend.
3. **H1 zone** — latest untested Demand/Supply zone; invalidation by body close.
4. **M5 trigger** — pullback into the zone → CHoCH in trend direction.
5. **FVG validation** — min size per instrument, fill < 50%; session scope:
   forex per London/NY block, crypto per whole Prague day. Rejections are
   explained in logs (best candidate size / fill / session).
6. **SL** — behind the confirmed M5 pivot + buffer; **TP** — nearest untested
   opposite zone (H1, falling back to H4 if the H1 target fails min RR);
   **RR ≥ 1:2** or SKIP.
7. **Position size** — from `SMC_DEPOSIT` at 2% risk (crypto qty / forex lots).
8. **Rule 9 correlation guard** — warns about forbidden USD combinations
   (e.g. EURUSD + GBPUSD in the same direction).

## Running

```bash
cp env.example .env          # fill in TELEGRAM_* and OANDA_API_TOKEN
python smc_watcher.py                  # run forever (scheduler + command bot)
python smc_watcher.py --once           # single check, prints the summary
python smc_watcher.py --test-telegram  # verify Telegram wiring
```

### Railway deployment

One service, no database, no Redis, no public domain needed:

1. Create a service from this repo (Dockerfile is picked up automatically;
   the default command runs the watcher).
2. Variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   (+ optionally `SMC_DEPOSIT`; `OANDA_API_TOKEN` only if you want OANDA data).

The bot uses Telegram long polling — any old webhook is removed automatically
at startup.

### Optional: OANDA API token

Forex works out of the box via Yahoo. To switch to OANDA data: OANDA account →
**Manage API Access** (My Services) → Generate, then set `OANDA_API_TOKEN` and
`OANDA_ENVIRONMENT` (`practice` for a demo token, `live` for a real one).

## Configuration

All settings are environment variables — see [env.example](env.example).
Key ones:

| Variable | Default | Meaning |
|---|---|---|
| `SMC_PAIRS` | `ETHUSD,USDJPY` | initial pairs (runtime changes via `/pairs`) |
| `SMC_SESSION_INTERVAL_MINUTES` | `5` | check cadence inside sessions (M5 close) |
| `SMC_INTERVAL_MINUTES` | `15` | check cadence outside sessions |
| `SMC_DEPOSIT` | — | deposit in USD for lot hints |
| `SMC_NOTIFY_NO_SETUP` | `false` | opt-in 15-min heartbeat messages |
| `SMC_DB_FILE` | `.smc_watcher.db` | SQLite path (put on a volume for persistence) |
| `SMC_NEWS_DIGEST_TIME` | `07:45` | Prague time of the morning news digest |
| `SMC_ENFORCE_SESSIONS` | `true` | only trade session windows |
| `OANDA_API_TOKEN` | — | optional: use OANDA instead of Yahoo for forex |
| `OANDA_ENVIRONMENT` | `practice` | `practice` / `live` |

## Tests

```bash
pytest tests/ -v
```

## Project layout

```
smc_watcher.py              # entry point: scheduler, alerts, live cards,
                            # news, journal tracking, discipline
app/core/                   # config (pydantic-settings), logging, exceptions
app/services/smc/
├── engine.py               # rules 0-8 orchestration (pure, testable)
├── structure.py            # pivots, trend, zones, BOS/CHoCH
├── fvg.py                  # FVG detection, validation & rejection diagnostics
├── sessions.py             # 08-20 Prague trading hours, London/NY blocks
├── instruments.py          # per-pair parameters & data source registry
├── data.py                 # Binance fetcher (ETHUSD)
├── twelvedata.py           # Twelve Data fetcher (forex, cached, free tier)
├── yahoo.py                # Yahoo Finance fetcher (forex fallback, no key)
├── oanda.py                # OANDA v20 fetcher (forex, optional)
├── news.py                 # Forex Factory calendar, blackouts, day timeline
├── journal.py              # signal lifecycle, taken marks, discipline, /stats
├── chart.py                # setup chart PNG for alerts (matplotlib, no pandas)
├── telegram_bot.py         # long-polling commands, slash menu, alert buttons
├── notifier.py             # send/edit/pin/photo, HTML escaping, formatting
├── state.py                # runtime state on SQLite (pairs, dedup keys)
├── db.py                   # SQLite wrapper, column migrations, JSON import
└── models.py               # Candle, Zone, FVG, TradeSetup, AnalysisResult
tests/test_smc/             # 93 unit + end-to-end strategy tests
CLAUDE.md                   # guidance for AI-assisted development
```

## Roadmap — Aggressive breakout mode (not yet implemented)

An optional, opt-in mode for ranging weeks where the strict trend-following
rules produce zero forex setups. The standard strategy waits for a **confirmed**
H4 trend (HH+HL / LH+LL) before hunting — so it sits out the *first* leg of a
breakout, entering only on the second (see the ETHUSD/GBPUSD breakout days).

**Aggressive breakout mode** would enter earlier: after an **H4 CHoCH** (the
prior trend is broken), take the entry on the **retest of the impulse FVG**,
**without** waiting for a confirmed HH+HL. Alerts would be tagged `⚡ aggressive`
with a recommendation to halve risk (lower-probability-per-trade, catches the
first leg). Default **off**; enabled per-pair or globally via a flag.

Decision gate: **add it on Saturday if the week's setup count is zero.** Judge
with `/stats`, not frustration. Keep the standard mode as the default — the
strict rules protect capital in chop; this is a deliberate trade of win-rate
for participation.

## Risk disclaimer

The bot **detects** setups by the strategy rules and tracks discipline for
trades you mark as taken. Actual order execution, weekly loss limits and
final risk decisions remain the trader's responsibility.
