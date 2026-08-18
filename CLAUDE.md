# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

A single-process Telegram bot that watches currency pairs for **Triple Sync +
Imbalance** SMC (Smart Money Concepts) setups and sends an urgent alert when
one is found. There is no web server, no Redis, no Postgres — one worker
(`smc_watcher.py`) with a SQLite file. It runs on Railway.

The **strategy specification is law**: rules −1 through 11 (H4 trend — or H1
when H4 reads FLAT and H1 has a clean trend, owner decision 2026-08-06 → H1
zone of interest, an order block or, when none qualifies, an untouched H1
imbalance (owner decision D4) → M5 CHoCH + FVG → SL behind the swept extreme
→ TP at the nearest unswept liquidity level → session windows → news
blackouts → correlation limits) come from the owner's written trading
system. Never relax or "improve" a strategy rule without the owner's
explicit decision — implementation over-strictness may be fixed, the rules
themselves may not. "Almost valid" does not exist in this system.

The bot is a **detector, not a prescriber** (owner decision 2026-08-06,
"detector mode"): once a setup fully forms, the alert always fires.
`SMC_MIN_RR` (Rule 7, RR to the nearest unswept liquidity),
`SMC_MAX_ENTRY_GAP_R` (Rule 5.1, how far price has run past the entry) and
"no unswept liquidity ahead" used to return `Verdict.SKIP` and send nothing;
they now attach a `⚠️` warning to the alert instead — the setup is announced
either way, and the owner decides whether the warning matters. What still
suppresses: everything that decides whether a setup exists at all — off-
session, market closed, news blackout, no H4/H1 direction, no H1 zone,
price has not reached the zone yet, the zone was invalidated after the
touch (a body close through the far edge), no M5 CHoCH, no valid FVG —
plus one geometry check that runs after all of them: `risk <= 0` (Rule 6,
SL lands at the entry level) is a malformed trade, not a judgment call, and
still returns `Verdict.SKIP` rather than a warning.

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
                          journal tracking, auto-plan snapshots at 07:55/13:55
                          (silent summary + aplan_* buttons), per-cycle plan
                          recompute into the planbook with silent summary
                          edits on material change, plan-centric zone alerts
                          (replaced the old engine-zone ping)
app/services/smc/
├── engine.py             TripleSyncEngine: rules 0-8 checklist; pure
│                         evaluate() is fully unit-testable on synthetic candles
├── structure.py          fractal-5 pivots (2-closed-candle confirmation),
│                         H4 trend HH+HL/LH+LL with fakeout-reclaim, H1 zones
│                         (untested only) + zone_ladder (untested zones on the
│                         trade's own side, further out — deeper entries; the
│                         live zone excluded), find_h1_fvg_zone (the most
│                         recent untouched H1 imbalance on the trade's side,
│                         as a Zone), find_zone_of_interest (the order block
│                         if one qualifies, else the untouched imbalance —
│                         owner decision D4; feeds Rule 2 and the plan), M5
│                         CHoCH, sweep_extreme (Rule 6 stop reference),
│                         find_order_block (the M5 order block, a deeper
│                         second entry, searched only inside the zone-
│                         touch→FVG window and kept only when it is deeper
│                         than the entry and inside the stop), m5_marks (the
│                         M5 order block + imbalance inside a touched zone,
│                         label-only, for the 🔔 alert's 🔎 line)
├── liquidity.py          unswept swing highs/lows + EQH/EQL pools;
│                         nearest_liquidity (Rule 7 take-profit target) and
│                         liquidity_ladder (five rungs shown in the alert)
├── sniper.py             sniper-tier primitives (Phase 2 redesign): room_r
│                         (H1/H4 liquidity room ahead of entry), sweep_label
│                         (named pool taken by the touch→CHoCH excursion),
│                         pd_state (premium/discount vs. the H1 dealing
│                         range), classify (the ⭐ verdict, checked after a
│                         setup already fully formed — detector mode
│                         unchanged — and denied when H4/H1 trend disagree,
│                         owner decision D6)
├── fvg.py                FVG detection, validation (size/fill/session) and
│                         rejection diagnostics (best_rejected_fvg)
├── sessions.py           trading hours 08:00-18:30 Prague, two blocks split
│                         at 14:00 (London/NY FVG separation), forex Mon-Fri
├── instruments.py        per-pair registry: source, min FVG, SL buffer, pip
├── data.py / twelvedata.py / oanda.py   candle fetchers (same interface):
│                         crypto=Binance always; forex source per
│                         SMC_FOREX_SOURCE (auto = TwelveData key > OANDA
│                         token; a forex key is required, no keyless
│                         fallback). Twelve Data caches H4/H1 to stay under
│                         the free 800 req/day (see _TF_CACHE_TTL)
├── news.py               Forex Factory red-news calendar, blackout windows,
│                         digest day-timeline
├── journal.py            signal lifecycle pending→open→tp/sl/expired with
│                         state-change events, taken marks (alert buttons),
│                         discipline_block (Rule 10 / Rule 0.2), /stats
├── plan.py               Pre-Market Plan (Шаблон B): projected entry/SL/TP/RR
│                         from H4/H1 structure; both-way brackets when H4 flat;
│                         on-demand via /plan, folded with live engine status;
│                         Rule 1 direction parity with the engine (H1-trend
│                         fallback on H4 FLAT, ahead of the aggressive-profile
│                         CHoCH check, before both-way speculative brackets)
├── planbook.py           in-memory current-plan store (PlanBook/PlanEntry),
│                         filled by the 07:55/13:55 snapshot and by the
│                         per-cycle recompute; material fingerprint
│                         (plan_fingerprint) drives silent summary edits;
│                         scenario_for_touch backs the plan-zone alert
├── chart.py              alert chart PNG (M5) + plan chart PNG (H1): candles,
│                         zones, levels (matplotlib Agg, NO pandas — keep it so)
├── telegram_bot.py       long-polling commands, slash-menu registration,
│                         Took/Skipped callbacks; serves ONLY the owner chat
├── notifier.py           send/edit_message/pin/send_photo + escape_html
├── state.py              runtime state (pairs, dedup keys, zone-alert
│                         mutes) on SQLite kv
└── db.py                 SQLite wrapper (signals + kv), column auto-migration,
                          legacy JSON import, fallback to a local file if the
                          volume is unwritable
```

Data flow per cycle: news refresh → per enabled pair: blackout check →
fetch H4/H1/M5 → engine checklist → discipline check → alert (buttons +
pinned card + chart PNG, dedup per zone+session) / log → journal outcome
tracking → live-card edits on fill/TP/SL events.

## Conventions and gotchas

- **All bot-facing text is English**; conversation with the owner is Russian
  (address him as «Брат»). Message timestamps are **Prague time**.
- **Telegram messages use parse_mode=HTML**: any dynamic string embedded in a
  message MUST go through `notifier.escape_html` (a raw `<` in "fill < 50%"
  once broke message delivery in production). Only `<b>` and `<pre>` tags are
  used — `<pre>` was added 2026-08-06 (owner decision) to hold the liquidity
  ladder by itself, so its space-padded columns still line up in Telegram's
  proportional font. The widening does not relax the escaping rule: every
  dynamic value interpolated inside the `<pre>` block still goes through
  `escape_html`, exactly like everywhere else. Nothing else gains a tag.
- **Detector mode** (owner decision 2026-08-06): after a setup completes,
  nothing suppresses the message. `SMC_MIN_RR` and `SMC_MAX_ENTRY_GAP_R` are
  warning thresholds, not gates. Everything that suppresses BEFORE a setup
  completes is unchanged.
- **Quiet mode is the default**: Telegram receives only found setups (and
  Rule 9/0.4 warnings + the 07:45 digest). Everything else goes to logs.
  Do not add chatty messages without being asked.
- Engines see **closed candles only** — every fetcher drops the in-progress
  candle. Twelve Data and OANDA both serve native H4 candles; neither
  resamples.
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
- **Strategy profiles** (`profiles.py`) are read by the engine at exactly four
  points: direction (H4 CHoCH on FLAT trend), FVG size, H1 zone selection
  (`max_zone_touches`), and FVG scope. `fvg_size_factor` is a **multiplier** on
  the per-instrument minimum FVG — per-instrument thresholds stay in
  `instruments.py`, never hardcode a profile-specific size elsewhere. The
  per-pair `/strategy` picker UI is retired (owner request 2026-08-12, one
  strategy ships): profiles remain code-level plumbing only —
  `WatcherState.set_profile` still clears that pair's dedup keys
  (`last_setup`, `zone_pinged`) when called, it is just no longer reachable
  from any command.
- **`/notify`** (`WatcherState.notify_level`, replaces `/strategy`) is a
  GLOBAL setup-alert level — `"all"` (⭐ loud + regular quiet, default),
  `"star"` (⭐ only; regular setups are still journal-recorded and
  dedup-fingerprinted, just not sent), or `"mute"` (no setup alerts sent at
  all, same record/dedup guarantee) — and never touches the 07:45 digest,
  plan-zone alerts, or Rule 0.4/9 warnings.
- **Sniper tier** (`sniper.py`, Phase 2 redesign): a completed setup earns
  ⭐ when room >= 1.0R on H1/H4 liquidity (or unmeasurable), a pool was
  swept, premium/discount is ok (or unmeasurable), the entry isn't stale
  beyond `SMC_MAX_ENTRY_GAP_R` (0.75R), and H4/H1 trend do not point
  opposite ways (owner decision D6, 2026-08-18) — two alert tiers, both
  still sent (detector mode): a disagreeing setup fires without the star,
  and the alert header labels the disagreement (`⚠️ counter-hourly`).
  Every fill uses the hybrid exit regardless of tier: TP1 at `SMC_TP1_R`
  (2R) closes half and moves SL to break-even, the runner rides to
  `SMC_RUNNER_R` (3R) — journal statuses `tp1_be` (BE stop after TP1) and
  `tp1_runner` (runner target hit) track it.
- pytest config: `pytest.ini` (asyncio_mode=auto). Tests build synthetic
  candles via `tests/test_smc/helpers.py` (asymmetric wicks make turning
  points strict fractal pivots). Keep tests network-free.
- Liquidity (`liquidity.py`) and zones (`structure.py`) are different objects:
  a zone is an order block price reacts from, a liquidity level is the stop
  pool behind a swing extreme. Rule 7 aims at liquidity; Rule 2 enters at a
  zone. Sweep detection is wick-based with a tolerance of the raw per-
  instrument `min_fvg` — never the profile-scaled value.
- **The H1 zone of interest can be an order block or an imbalance**
  (`Zone.kind`, `"OB"`/`"FVG"`). `find_zone_of_interest` prefers the order
  block and falls back to the untouched imbalance only when none qualifies
  (owner decision D4): an order block is the footprint of a filled order —
  price already reacted there — while an imbalance is only a gap nobody
  has traded back into, a weaker claim. Freshness for that fallback is
  owner decision D10: the imbalance must have zero penetration
  (`find_h1_fvg_zone`), the exact mirror of an untested order block and
  deliberately stricter than Rule 4's `fill < 50%` for the M5 entry
  imbalance — H1 is a zone the bot is still *waiting* for, M5 is a zone
  it is *entering*. `m5_marks` (the M5 order block + imbalance inside a
  touched zone, shown on the 🔔 alert's `🔎` line) is label-only: no
  verdict, no suppression, no message of its own, and Rule 4's session/
  fill checks deliberately do not apply to it.
- **Plan-centric zone alert** (spec 2026-08-11 §5, dedup rewritten by owner
  decision 2026-08-16): fires when price touches a zone named by the pair's
  *current* plan (`planbook.scenario_for_touch`), carrying that scenario's
  projected bracket. It fires **once per zone per session block** —
  `sessions.session_block` supplies the block id, and
  `state.zone_pinged[key]` holds every `[bottom, top, direction, block_id]`
  already alerted. Two zones count as the same zone when the direction
  matches and the bounds overlap at all; the old rule (exact bounds, and
  the episode ending as soon as price left the zone) re-armed the alert
  every time price oscillated on a zone edge and sent USDCAD four identical
  messages on 2026-08-13. Nothing re-arms an alert inside a block. The 🔕
  button under the alert writes `state.zone_muted[pair]` (ISO UTC deadline
  from `sessions.mute_deadline`: this block's end, or tomorrow's open in
  the day's last block) and silences that pair's zone alerts only — setup
  alerts, Rule 0.4 warnings and the 07:45 digest ignore it. `/unmute`
  clears every mute; `/status` lists them.
  `state.remember_plan_zones` (the separate "from this morning's plan"
  provenance, spec 2026-08-06 §6) is still written **only** by the
  07:55/13:55 snapshots and manual `/plan` — never by the 5-minute
  recompute.

## Workflow

- Work on a feature branch (currently `feat/sniper-redesign`), PRs to `master` via `gh`
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
