# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

A single-process Telegram bot that watches currency pairs for **Triple Sync +
Imbalance** SMC (Smart Money Concepts) setups and sends an urgent alert when
one is found. There is no web server, no Redis, no Postgres — one worker
(`smc_watcher.py`) with a SQLite file. It runs on Railway.

The **strategy specification is law**: rules −1 through 11 (H4 trend → H1 zone
→ M5 CHoCH + FVG, RR ≥ 1:2, session windows, news blackouts, correlation
limits) come from the owner's written trading system. Never relax or "improve"
a strategy rule without the owner's explicit decision — implementation
over-strictness may be fixed, the rules themselves may not. "Almost valid"
does not exist in this system.

## Commands

```bash
pytest tests/ -v                       # full test suite (fast, no network)
flake8 app/ tests/ smc_watcher.py      # lint (config in .flake8)
python smc_watcher.py --once           # live one-shot check (real market data)
python smc_watcher.py --test-telegram  # sends test messages to the owner chat
python smc_watcher.py                  # run forever: scheduler + command bot
```

Local runs need `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (a dummy token
like `123:dummy` works for `--once` — sends fail gracefully). Tests need
nothing.

## Architecture

```
smc_watcher.py            Watcher class: 5-min in-session scheduler (15-min
                          off-session), per-pair cycle, alert dedup, live
                          setup cards, discipline suppression, 07:45 weekday
                          news digest, on-demand /plan, Rule 0.4 warnings,
                          journal tracking
app/services/smc/
├── engine.py             TripleSyncEngine: rules 0-8 checklist; pure
│                         evaluate() is fully unit-testable on synthetic candles
├── structure.py          fractal-5 pivots (2-closed-candle confirmation),
│                         H4 trend HH+HL/LH+LL with fakeout-reclaim, H1 zones
│                         (untested only), M5 CHoCH, TP target zones
├── fvg.py                FVG detection, validation (size/fill/session) and
│                         rejection diagnostics (best_rejected_fvg)
├── sessions.py           trading hours 08:00-20:00 Prague, two blocks split
│                         at 14:00 (London/NY FVG separation), forex Mon-Fri
├── instruments.py        per-pair registry: source, min FVG, SL buffer, pip
├── data.py / twelvedata.py / yahoo.py / oanda.py   candle fetchers (same
│                         interface): crypto=Binance always; forex source per
│                         SMC_FOREX_SOURCE (auto = TwelveData key > OANDA token
│                         > keyless Yahoo). Twelve Data caches H4/H1 to stay
│                         under the free 800 req/day (see _TF_CACHE_TTL)
├── news.py               Forex Factory red-news calendar, blackout windows,
│                         digest day-timeline
├── journal.py            signal lifecycle pending→open→tp/sl/expired with
│                         state-change events, taken marks (alert buttons),
│                         discipline_block (Rule 10 / Rule 0.2), /stats
├── plan.py               Pre-Market Plan (Шаблон B): projected entry/SL/TP/RR
│                         from H4/H1 structure; both-way brackets when H4 flat;
│                         on-demand only via /plan (not auto-sent)
├── chart.py              alert chart PNG (M5) + plan chart PNG (H1): candles,
│                         zones, levels (matplotlib Agg, NO pandas — keep it so)
├── telegram_bot.py       long-polling commands, slash-menu registration,
│                         Took/Skipped callbacks; serves ONLY the owner chat
├── notifier.py           send/edit_message/pin/send_photo + escape_html
├── state.py              runtime state (pairs, dedup keys) on SQLite kv
└── db.py                 SQLite wrapper (signals + kv), column auto-migration,
                          legacy JSON import, fallback to a local file if the
                          volume is unwritable
```

Data flow per cycle: news refresh → per enabled pair: blackout check →
fetch H4/H1/M5 → engine checklist → discipline check → alert (buttons +
pinned card + chart PNG, dedup per session) / log → journal outcome
tracking → live-card edits on fill/TP/SL events.

## Conventions and gotchas

- **All bot-facing text is English**; conversation with the owner is Russian
  (address him as «Брат»). Message timestamps are **Prague time**.
- **Telegram messages use parse_mode=HTML**: any dynamic string embedded in a
  message MUST go through `notifier.escape_html` (a raw `<` in "fill < 50%"
  once broke message delivery in production). Only `<b>` tags are used.
- **Quiet mode is the default**: Telegram receives only found setups (and
  Rule 9/0.4 warnings + the 07:45 digest). Everything else goes to logs.
  Do not add chatty messages without being asked.
- Engines see **closed candles only** — every fetcher drops the in-progress
  candle; Yahoo H4 is resampled from 1h into 0/4/8/12/16/20 UTC buckets.
- Per-instrument parameters (min FVG 5 pips forex / $2 ETH, SL buffer, pip,
  decimals) live in `instruments.py` — never hardcode them elsewhere.
- The container runs **as root** on purpose: Railway volumes are root-owned
  (a non-root user caused a production crash-loop). `db.py` must never crash
  the watcher — it falls back to an ephemeral local DB and logs loudly.
- Journal outcome semantics: entry fill = candle touch; TP and SL in the same
  candle counts as **SL** (conservative); pending orders expire with their
  session (Rule 10).
- **Discipline is driven by `taken` marks only** (the ✅/❌ alert buttons):
  Rule 10 re-entry bans and the Rule 0.2 daily stop count taken stops, never
  skipped or unanswered signals. Do not weaken this without the owner.
- **Chart rendering must never block an alert** — `_send_alert` wraps it in
  try/except; keep `chart.py` matplotlib-only (no pandas/mplfinance, image
  size matters on Railway).
- Adding a signal column? Extend `SIGNAL_COLUMNS`, the `CREATE TABLE` and the
  migration list in `db.py` — existing production DBs are migrated in place.
- pytest config: `pytest.ini` (asyncio_mode=auto). Tests build synthetic
  candles via `tests/test_smc/helpers.py` (asymmetric wicks make turning
  points strict fractal pivots). Keep tests network-free.

## Workflow

- Work on branch `feat/smc-watcher-railway`, PRs to `master` via `gh`
  (installed at `C:\Program Files\GitHub CLI` on the owner's machine; add to
  PATH in bash). The owner merges; Railway deploys master.
- PR template lives in `.github/pull_request_template.md` — follow it.
- Run `pytest` and `flake8` before every commit; add regression tests for
  every production bug fixed.

## Deployment (Railway)

One service from this repo (Dockerfile default CMD). Required vars:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Recommended: volume mounted at
`/data` + `SMC_DB_FILE=/data/smc.db` (persistence), `SMC_DEPOSIT` (lot hints).
Optional: `OANDA_API_TOKEN`/`OANDA_ENVIRONMENT` for better forex data.
All tunables are documented in `env.example`.
