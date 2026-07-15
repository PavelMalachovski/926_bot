# SMC Watcher — Triple Sync + Imbalance

A Telegram bot that runs the **Triple Sync + Imbalance** SMC strategy every
15 minutes for the selected currency pairs and alerts you the moment a valid
setup appears.

- 🚨 **Urgent alert** when a setup is APPROVED (entry / SL / TP / RR / lot size)
- 🔍 **Heartbeat** every 15 minutes when there is no setup («сетапа нет»)
- 💱 **Pairs are switchable at runtime** via Telegram: `/pairs`

## Supported pairs

| Pair | Data source | Min FVG | Notes |
|---|---|---|---|
| ETHUSD | Binance (no key needed) | $2.00 | 24/7, funding-rate advisory |
| USDJPY | Yahoo Finance (free) / OANDA | 5 pips | no key needed by default |
| EURUSD | Yahoo Finance (free) / OANDA | 5 pips | no key needed by default |
| GBPUSD | Yahoo Finance (free) / OANDA | 5 pips | no key needed by default |
| USDCAD | Yahoo Finance (free) / OANDA | 5 pips | no key needed by default |

Forex candles come from the keyless Yahoo Finance feed by default (5m/1h
native, H4 resampled from 1h). If `OANDA_API_TOKEN` is set, OANDA v20 is used
instead — slightly better data, same functionality.

Default watched pairs: **ETHUSD + USDJPY** (change with `/pairs` or `SMC_PAIRS`).

## Telegram commands

| Command | What it does |
|---|---|
| `/pairs` | inline keyboard — toggle watched pairs on/off |
| `/status` | enabled pairs, current session, last verdicts |
| `/check` | run the full strategy check right now |
| `/help` | command list |

## Strategy checklist (per pair, every 15 min)

1. **Session filter** — entries only inside Frankfurt/London & NY windows
   (Prague time, DST-aware). Closed forex market is detected automatically.
2. **H4 trend** — HH+HL / LH+LL with 2-closed-body pivot confirmation.
3. **H1 zone** — latest untested Demand/Supply zone; invalidation by body close.
4. **M5 trigger** — pullback into the zone → CHoCH in trend direction.
5. **FVG validation** — min size per instrument, fill < 50%, same session only.
6. **SL** — behind the confirmed M5 pivot + buffer; **TP** — nearest untested
   opposite zone; **RR ≥ 1:2** or SKIP.
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
| `SMC_INTERVAL_MINUTES` | `15` | check cadence |
| `SMC_DEPOSIT` | — | deposit in USD for lot hints |
| `SMC_NOTIFY_NO_SETUP` | `true` | 15-min heartbeat messages |
| `SMC_ENFORCE_SESSIONS` | `true` | only trade session windows |
| `OANDA_API_TOKEN` | — | optional: use OANDA instead of Yahoo for forex |
| `OANDA_ENVIRONMENT` | `practice` | `practice` / `live` |

## Tests

```bash
pytest tests/ -v
```

## Project layout

```
smc_watcher.py              # entry point: scheduler + Telegram command bot
app/core/                   # config, logging, exceptions
app/services/smc/
├── engine.py               # rules 0-8 orchestration
├── structure.py            # pivots, trend, zones, BOS/CHoCH
├── fvg.py                  # FVG detection & validation
├── sessions.py             # Prague session windows
├── instruments.py          # per-pair parameters & data source registry
├── data.py                 # Binance fetcher (ETHUSD)
├── yahoo.py                # Yahoo Finance fetcher (forex default, no key)
├── oanda.py                # OANDA v20 fetcher (forex, optional)
├── telegram_bot.py         # long-polling commands: /pairs /status /check
├── notifier.py             # message formatting & delivery
├── state.py                # persisted pairs & reported setups
└── models.py               # Candle, Zone, FVG, TradeSetup, AnalysisResult
tests/test_smc/             # unit + end-to-end strategy tests
```

## Risk disclaimer

The bot only **detects** setups by the strategy rules. Daily/weekly loss
limits, max trades per day and actual order execution remain the trader's
responsibility.
