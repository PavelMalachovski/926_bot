# Money machine, phase 1: in-repo backtester + TradingView as the analyst's eyes

Date: 2026-08-30
Base: `origin/master` @ a5d8e8f
Branch: `claude/tradingview-smart-money-signals-5ndydk`

## Problem

The owner named four priorities for the next stage (conversation 2026-08-30):

1. **Prove the strategy with numbers** — rules −1…11 have never been run over
   history; winrate, expectancy and drawdown are unknown.
2. **Signal quality** — which ⚠️ warnings actually correlate with losers, is
   the ⭐ tier earning its star.
3. **More instruments** — the registry holds five pairs, only two are live;
   candidates (XAUUSD, BTCUSD, …) should be vetted before being enabled.
4. **Speed / realtime** — see less delay between a setup forming and the alert.

Separately, the owner asked whether
[tradesdontlie/tradingview-mcp](https://github.com/tradesdontlie/tradingview-mcp)
should become part of the system, hoping it carries SMC libraries and real
data "before Claude's eyes".

## What tradingview-mcp actually is (audit findings)

The repository was cloned and read end to end:

- It is a **bridge between Claude Code and the TradingView *Desktop* app
  running locally on the owner's machine**, over Chrome DevTools Protocol
  (port 9222). It is not a cloud API and holds no market data of its own.
  78 MCP tools + a `tv` CLI: read OHLCV (≤500 bars), control the chart,
  write/compile Pine Script, drive the Strategy Tester and bar replay,
  screenshots, local JSONL streaming.
- **It contains no SMC libraries.** The only "order block" in the codebase is
  an example query for `indicator_search`. The SMC value is indirect: the
  bridge can add TradingView *community* indicators (LuxAlgo Smart Money
  Concepts and similar) to a chart and read their drawings back
  (`data_get_pine_lines/labels/boxes/tables`).
- **It cannot run on Railway.** TradingView Desktop is a GUI Electron app
  with a login session, driven through undocumented internals that break on
  TV updates. It works only while the owner's computer is on with TV running.
- Its own README warns that programmatic consumption of TradingView data may
  conflict with TradingView's Terms of Use.

## Decisions (owner, 2026-08-30)

- **D18 — the detector stays on Railway.** 24/7 detection on
  TwelveData/OANDA/Binance exactly as today. TradingView is **never** a data
  source for the detector: machine-bound, fragile across TV updates, ToS
  risk. No hybrid feeder, no webhook server.
- **D19 — tradingview-mcp gets two roles, both on the owner's machine**:
  *analyst eyes* (a signal arrives in Telegram → the owner opens a local
  Claude Code session → Claude verifies the setup on the live chart:
  screenshot, LuxAlgo SMC overlay, levels, second opinion) and *practice /
  visual backtest aids* (replay, Pine experiments). The owner starts with a
  **free** TradingView account now; a paid plan is a later call.
  Setup guide: [docs/tradingview-mcp-setup.md](../../tradingview-mcp-setup.md).
- **D20 — the reference backtest is the in-repo Python harness.** It drives
  the production `engine.evaluate` and `journal.evaluate_signal` — the same
  bytes that run live. A Pine Script port would be a second implementation
  of rules that are law and would drift; if one is ever written it is a
  visualization aid, never the reference.
- **D21 — backtest v1 scope**: ETHUSD + USDJPY (the pairs in battle today).
  Forex history from **TwelveData** behind a local disk cache (the 800/day
  key is shared with the live bot — history is paid for once, then reused);
  crypto history from Binance (free, keyless). The news blackout is **off**
  in v1 and every report says so in its header (live would have suppressed
  some of the listed signals; the bias is toward *more* signals, not
  different ones).
- **D22 — plan first.** This spec lands alone; the backtester code follows
  in a separate PR after the owner reads this.

## Architecture: three pillars

```
┌─ Railway (unchanged) ─────────────────────────────────────────────┐
│ smc_watcher.py → engine → Telegram alerts, 24/7. Not touched.     │
└───────────────────────────────────────────────────────────────────┘
┌─ This repo, offline (new) ────────────────────────────────────────┐
│ smc_backtest.py → history cache → engine.evaluate (walk-forward)  │
│ → journal.evaluate_signal → stats report. Answers priorities 1-3. │
└───────────────────────────────────────────────────────────────────┘
┌─ Owner's machine (new, optional per session) ─────────────────────┐
│ TradingView Desktop + tradingview-mcp + local Claude Code =       │
│ on-demand verification of a live alert on the real chart.         │
└───────────────────────────────────────────────────────────────────┘
```

## Part A — the backtester

### New files

```
app/services/smc/history.py     paginated historical fetch + disk cache
app/services/smc/backtest.py    simulation loop + metrics
smc_backtest.py                 CLI entry point
tests/test_smc/test_backtest.py synthetic-candle tests, network-free
```

Nothing in the live path is modified. `data/backtest/` joins `.gitignore`.

### Data layer (`history.py`)

- TwelveData `time_series` pages backwards with `end_date`, up to 5000 bars
  per request. Budget for 12 months of one forex pair: M5 ≈ 74k bars ≈ 15
  requests, H1 ≈ 2, H4 ≈ 1 — both live pairs together stay far under the
  800/day free tier, and only on the first run.
- Binance klines for ETHUSD: 1000 bars/request, free, no key.
- Cache: one JSON file per `{pair}_{tf}` under `data/backtest/`, append-only
  incremental refresh (`--refresh-cache` forces a re-pull of the tail).
- Candle parsing reuses the existing fetchers' parsers
  (`twelvedata.parse_time_series`); no new candle shape.

### Simulation loop (`backtest.py`) — fidelity rules

The whole point is that the backtest sees **exactly what production sees**:

1. Walk forward over M5 candles. At each step, "now" = the M5 candle's close
   time; a candle exists for the engine only when its end ≤ now (closed
   candles only, same as every live fetcher).
2. The engine gets the same windows the live fetchers serve:
   `h4[-300:]`, `h1[-400:]`, `m5[-400:]`, sliced at "now"
   (`fetch_all_timeframes` in `twelvedata.py:231`).
3. Session gate replicated from `analyze()` with the historical "now"
   (`active_session`, weekday rule for forex) — `evaluate()` itself is pure.
   Implementation checkpoint: audit `evaluate()` for wall-clock reads before
   trusting historical replay; sessions and staleness live in `analyze()`.
4. Dedup is the watcher's own: `_setup_fingerprint` (`smc_watcher.py:156`,
   one announcement per zone per session block) against a `last_setup` map,
   so signal counts match what Telegram would have received.
5. Outcome tracking reuses `SignalJournal` with a throwaway SQLite file —
   `record(result)` and `evaluate_signal` run byte-for-byte: entry fill =
   touch, TP+SL in one candle = SL, hybrid TP1/BE/runner for trend fills,
   no hybrid for RANGE (D14), session expiry, open timeout.
6. Profile: `CONSERVATIVE` (the one that ships); `--profile` exists for
   experiments but reports label non-default profiles loudly.

Performance: ~50k in-session evaluates per pair-year, a few minutes of pure
Python. `--days` bounds the window; no optimization tricks in v1 — fidelity
over speed.

### Metrics (the report)

Per pair, split by tier (⭐ / regular / `⚠️ counter-hourly`) and by direction
source (`h4` / `h1` fallback / range):

- signals, fill rate, winrate, expectancy in R, profit factor,
  max consecutive losses, equity curve in R;
- breakdown by session block (London / NY);
- **warning autopsy**: outcome distribution conditional on each ⚠️
  (`SMC_MIN_RR` below threshold, entry gap past `SMC_MAX_ENTRY_GAP_R`, no
  liquidity ahead) — the data that tells the owner whether a warning is a
  real filter candidate. Any threshold or rule change that follows is a
  **separate owner decision**; the backtester only produces the evidence.

Output: stdout table + a markdown report per run (path printed, not
committed). CLI: `python smc_backtest.py --pair USDJPY --days 365
[--profile aggressive] [--refresh-cache]`.

### Out of scope for v1

- News blackout replay (needs a historical Forex Factory calendar; v2
  candidate, bias documented until then).
- Correlation limits between simultaneously open pairs (v1 runs pairs
  independently).
- Discipline simulation (Rule 10 / 0.2 count *taken* marks — a human
  decision the backtest cannot know; reports assume every signal taken).
- Spread/slippage modeling (candle-touch semantics, same as the journal).

## Part B — TradingView as the analyst's eyes

- The owner creates a **free** TradingView account and installs TradingView
  Desktop + tradingview-mcp on his Windows machine per
  [docs/tradingview-mcp-setup.md](../../tradingview-mcp-setup.md).
- Free-plan realities: ~2 indicators per chart (LuxAlgo SMC fits), forex
  and crypto quotes are fine, intraday bar replay and multi-chart layouts
  are where a paid plan would later add value.
- Workflow: 🚨 alert in Telegram → local Claude Code in this repo →
  "проверь сетап <pair>" → Claude sets symbol/timeframe over the bridge,
  screenshots the chart, reads the LuxAlgo SMC drawings, compares them with
  the alert's zone/entry/SL/TP and gives a verdict. The bot's alert stays
  the signal of record; the chart session is a second opinion.
- Phase 3 adds a small project skill (`.claude/skills/verify-setup/`) so the
  local session runs this checklist the same way every time.

## Roadmap

| Phase | Deliverable | Needs from owner |
|---|---|---|
| 0 (this PR) | This spec + TV setup guide | Read & approve |
| 1 | `history.py` + `backtest.py` + CLI + tests | — |
| 2 | First full reports for ETHUSD + USDJPY; warning autopsy | Read numbers; any threshold change is an owner decision |
| 3 | TV Desktop + tradingview-mcp installed locally; verify-setup skill | Free TV account, ~30 min setup |
| 4 | Candidate instruments (EURUSD/GBPUSD/USDCAD already registered; XAUUSD/BTCUSD need registry entries) run through the backtester; enable only what earns it | Pick candidates from the report |

### On priority 4 (speed / realtime)

Already investigated: the scheduler aligns to M5 slot boundaries plus ~10 s
(`_seconds_until_next_slot`), so the bot evaluates moments after each M5
close. The remaining latency **is** the strategy: engines see closed candles
only (a deliberate property — an M5 CHoCH does not exist until the candle
closes). Streaming ticks from TradingView was considered and rejected under
D18. No action; recorded here so the question does not reopen by accident.
