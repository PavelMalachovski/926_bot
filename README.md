# SMC Watcher — Triple Sync + Imbalance

A Telegram bot that runs the **Triple Sync + Imbalance** SMC strategy for the
selected currency pairs — every 5 minutes during trading hours (08:00–18:30
Prague), every 15 minutes outside them — and alerts you the moment a valid
setup appears.

- 🚨 **Setup alert** the moment Triple Sync + Imbalance completes — zone,
  FVG entry, M5 order block (a deeper second entry), stop, and a ladder of
  up to five unswept liquidity levels with their RR, a **dark-style M5 chart
  PNG** and **✅ Took it / ❌ Skipped buttons**. The bot is a **detector**: it
  always announces a completed setup, flagging a thin RR, a stale entry or
  no liquidity ahead with `⚠️` rather than staying silent — you place the
  order, you pick the level
- 📌 **Live setup card** — the alert is pinned and edited in place as the
  signal evolves: `📈 Filled @ … → 🎯 TP HIT (+2.1R)`; unpinned on resolution
- 🛡 **Discipline on autopilot** — trades you mark as taken enforce Rule 10
  (no re-entry after a stop in the same session) and Rule 0.2 (two taken
  stops close the trading day: alerts muted until tomorrow)
- 🤫 **Silent otherwise** — checks without a setup only go to the logs, with
  precise reasons («best FVG candidate: 3.2 pips < required 5»); `/check`
  shows the current picture on demand
- 💱 **Pairs are switchable at runtime** via Telegram: `/pairs`
- ⚡ **Strategy profile per pair** via Telegram: `/strategy` (🛡 Conservative /
  ⚡ Aggressive)
- 📅 **Forex Factory red-news digest** every weekday at 07:45 Prague
  (incl. a session-block breakdown of today's releases)
- 📋 **`/plan`** — an on-demand Pre-Market Plan for any watched pair, folded
  together with the **live checklist status** (projection + where the pair is
  right now); conditional entry/SL/TP/RR + H1 chart, both-way brackets when
  flat. The bot also builds it automatically at **07:55/13:55 Prague** for
  every enabled pair: a silent summary (no push notification) with a button
  per pair delivers that pair's full plan on demand (`SMC_AUTO_PLAN=true` by
  default)
- 🔔 **Zone-touch alert** — a "get ready" nudge the moment price reaches a
  zone named by the *current* Pre-Market Plan, carrying that scenario's
  projected entry/SL/TP/RR, before the full 🚨 setup forms (on by default,
  `SMC_ZONE_PING=false` to disable)
- ⏸ **`/pause` / `/resume`** — mute everything (alerts, digest, warnings)
  until you switch it back on; survives restarts
- 📒 **Signal journal**: every alert is auto-tracked to its TP/SL outcome;
  `/stats` shows signal winrate and your personal (taken) winrate separately

## Supported pairs

| Pair | Data source | Min FVG | Notes |
|---|---|---|---|
| ETHUSD | Binance (no key needed) | $2.00 | 24/7, funding-rate advisory |
| USDJPY | Twelve Data / OANDA | 5 pips | forex key required |
| EURUSD | Twelve Data / OANDA | 5 pips | forex key required |
| GBPUSD | Twelve Data / OANDA | 5 pips | forex key required |
| USDCAD | Twelve Data / OANDA | 5 pips | forex key required |

**Forex data source** (`SMC_FOREX_SOURCE`, default `auto`) resolves in this
order: **Twelve Data** if `TWELVEDATA_API_KEY` is set → **OANDA** if
`OANDA_API_TOKEN` is set. ETHUSD always uses Binance. A forex key is
required — there is no keyless fallback any more (the previous keyless feed
was removed: its OHLC data was quantised coarsely enough to distort replay
results). Twelve Data is recommended: free 800 req/day, native 4h/1h/5min
candles, runs on Railway; the higher timeframes are cached, and with the
trading day ending at 18:30 that is ~170 credits/day/pair — four forex pairs
fit inside the free budget with room to spare for `/plan`. Grab a free key
at [twelvedata.com](https://twelvedata.com).

Default watched pairs: **ETHUSD + USDJPY** (change with `/pairs` or `SMC_PAIRS`).

## Telegram commands

Commands are registered in the bot's slash menu (type `/` in the chat).

| Command | What it does |
|---|---|
| `/pairs` | inline keyboard — toggle watched pairs on/off |
| `/strategy` | inline keyboard — switch a pair's strategy profile (🛡 Conservative / ⚡ Aggressive), or all pairs at once |
| `/status` | enabled pairs, current session, last verdicts |
| `/check` | run the full strategy check right now |
| `/plan` | pre-market plan for a pair (buttons pick from enabled pairs / all) |
| `/stats` | journal: winrate bars, outcome sparkline, personal (taken) stats |
| `/journal` | manual trade journal — send an MT4 history screenshot to log trades |
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

## Trade journal (`/journal`)

A **manual** log of your real trades, separate from the automatic signal
journal above. Send the bot a **screenshot of your MetaTrader 4/5 history**
(from the mobile app) and it:

1. Parses **every** closed trade in the image with OpenAI Vision
   (`OPENAI_MODEL`, default `gpt-4o-mini`): symbol, direction, volume,
   open/close price and time, S/L, T/P, profit, swap, commission, taxes,
   ticket, and the `[sl]` marker.
2. Replies with a **preview** and **💾 Сохранить / ❌ Отмена** buttons —
   nothing is written until you confirm.
3. On confirm, stores the trades in the `trades` table (SQLite, same
   `SMC_DB_FILE`), **de-duplicated by ticket**.
4. `/journal` shows aggregate stats: total P/L, win rate, profit factor,
   best/worst trade, a per-symbol breakdown and the most recent trades
   (net = profit + swap + commission − taxes).

Only the owner's chat is served (same gate as every other command). Requires
`OPENAI_API_KEY`; without it the bot runs normally but screenshot parsing is
disabled.

## Strategy checklist (per pair, every 5 min in session)

1. **Session filter** — trading hours 08:00–18:30 Prague (Frankfurt/London
   08:00–14:00, New York 14:00–18:30): crypto every day, forex Monday–Friday.
   A closed
   forex market is also detected automatically. All message times are Prague.
2. **H4 trend** — HH+HL / LH+LL with 2-closed-body pivot confirmation;
   a reclaimed fakeout beyond the last HL/LH does not kill the trend. When H4
   reads flat but H1 has a clean trend, direction is taken from H1 instead
   (header reads `H4 flat — direction from H1 …`).
3. **H1 zone** — latest untested Demand/Supply zone; invalidation by body close.
4. **M5 trigger** — pullback into the zone → CHoCH in trend direction.
5. **FVG validation** — min size per instrument, fill < 50%; session scope:
   forex per London/NY block, crypto per whole Prague day. Rejections are
   explained in logs (best candidate size / fill / session).
6. **SL** — behind the sweep extreme (the wick that ran the liquidity just
   before the CHoCH, not the last fractal pivot) + buffer; **TP** — the
   nearest unswept liquidity level (an unswept swing high/low, or an EQH/EQL
   pool) minus one buffer, plus up to five further rungs on the same ladder.
   The bot is a **detector**: the setup is announced either way — an RR below
   `SMC_MIN_RR` (default `1.0`), an entry that has run more than
   `SMC_MAX_ENTRY_GAP_R` past the imbalance, or no unswept liquidity ahead
   all attach a `⚠️` warning to the alert instead of silencing it.
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
   (+ optionally `SMC_DEPOSIT`; a forex key — `TWELVEDATA_API_KEY` or
   `OANDA_API_TOKEN` — is required if any forex pair is enabled).

The bot uses Telegram long polling — any old webhook is removed automatically
at startup.

### Optional: OANDA API token

Forex requires a key from either Twelve Data (recommended) or OANDA — there
is no keyless feed. To use OANDA instead of Twelve Data: OANDA account →
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
| `SMC_DEFAULT_PROFILE` | `conservative` | strategy profile for a pair with no explicit `/strategy` choice: `conservative` \| `aggressive` |
| `OANDA_API_TOKEN` | — | optional: use OANDA instead of Twelve Data for forex |
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
├── sessions.py             # 08:00-18:30 Prague trading hours, London/NY blocks
├── instruments.py          # per-pair parameters & data source registry
├── data.py                 # Binance fetcher (ETHUSD)
├── twelvedata.py           # Twelve Data fetcher (forex, cached, free tier)
├── oanda.py                # OANDA v20 fetcher (forex, optional)
├── news.py                 # Forex Factory calendar, blackouts, day timeline
├── journal.py              # signal lifecycle, taken marks, discipline, /stats
├── chart.py                # setup chart PNG for alerts (matplotlib, no pandas)
├── telegram_bot.py         # long-polling commands, slash menu, alert buttons
├── notifier.py             # send/edit/pin/photo, HTML escaping, formatting
├── state.py                # runtime state on SQLite (pairs, dedup keys)
├── db.py                   # SQLite wrapper, column migrations, JSON import
└── models.py               # Candle, Zone, FVG, TradeSetup, AnalysisResult
tests/test_smc/             # 327 unit + end-to-end strategy tests
scripts/funnel.py           # offline calibration: replays the engine over
                            # historical candles per profile to size fvg_size_factor
CLAUDE.md                   # guidance for AI-assisted development
```

## Strategy profiles

Each watched pair runs under one of two **strategy profiles**. Both share the
same rulebook (H4 trend → H1 zone → M5 CHoCH + FVG, TP at the nearest unswept
liquidity level, flagged `⚠️` below `SMC_MIN_RR`, sessions, news,
correlation); a profile only scales strictness — it never bends a rule.

| Profile | Label | Behaviour |
|---|---|---|
| **Conservative** (default) | 🛡 Conservative | today's behaviour, unchanged: waits for a **confirmed** H4 trend, untested-only H1 zones, per-session FVG scope, full min FVG size |
| **Aggressive** (opt-in) | ⚡ Aggressive | relaxes four things (below); alerts are tagged `⚡ aggressive` |

The aggressive profile relaxes:

1. **Direction on H4 CHoCH** — when the H4 trend is FLAT, it takes trade
   direction from an H4 CHoCH instead of waiting for a confirmed HH+HL/LH+LL,
   catching the first leg of a breakout (the standard profile sits it out and
   only catches the second leg — see the ETHUSD/GBPUSD breakout days).
2. **Smaller minimum FVG** — the per-instrument minimum FVG size
   (`instruments.py`) is scaled by `AGGRESSIVE.fvg_size_factor`, calibrated to
   `0.4` via `scripts/funnel.py` (~21-45 days of M5, point-in-time replay).
   Conservative yields ~0 setups/week on forex and ~1 on ETHUSD; aggressive at
   0.4 gives ~5.5/wk on ETHUSD and ~1.4-2.5/wk per forex pair. A higher factor
   starves forex (its M5 FVGs are small vs the 5-pip minimum). Re-run the funnel
   to recalibrate.
3. **One retest allowed on H1 zones** — accepts a zone with up to one prior
   retest (`max_zone_touches=1`) instead of untested-only.
4. **Whole-day FVG scope** — an FVG stays valid for the entire Prague trading
   day instead of resetting at the London/NY session split.

Switch a pair's profile with **`/strategy`** (per-pair buttons, plus "All
conservative" / "All aggressive"). The choice persists in SQLite and is shown
in `/status`. Switching a pair clears its dedup keys, so the new profile's
first alert is not suppressed by a stale fingerprint from the old one.

`SMC_DEFAULT_PROFILE` (default `conservative`) sets the profile used for a
pair with no explicit `/strategy` choice — deploying this feature changes
nothing on its own until a button is pressed.

`scripts/funnel.py` is an offline calibration tool (run by hand, not part of
the worker): `python -m scripts.funnel --all --days 45` replays the engine
over historical candles per profile and reports where setups die in the rule
funnel plus the near-miss FVG size spread, used to pick `fvg_size_factor`.

## Risk disclaimer

The bot **detects** setups by the strategy rules and tracks discipline for
trades you mark as taken. Actual order execution, weekly loss limits and
final risk decisions remain the trader's responsibility.
