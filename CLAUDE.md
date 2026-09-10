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
system. The M5 imbalance stopped being part of that chain on 2026-08-30
(owner decision D22): the third sync is the **M5 CHoCH alone**, and the gap
is a label — it supplies the best entry when it is there, it is required for
the ⭐ tier, and its absence costs the star rather than the alert (see the
conventions section). When **both** H4 and H1 read FLAT — the one state that used to end
in "no direction" — a valid range (`range.detect_range`, clustered H1
pivots) gives the bot two boundaries to trade between instead of standing
down (owner decision D11, 2026-08-18): the boundary price worked most
recently supplies the direction, standing in for Rule 2's H1 zone of
interest; rules 3 through 5 run on that boundary unchanged, while the stop
(Rule 6) and the target (Rule 7) come from the range itself instead (see
the conventions section). The H1-trend fallback keeps precedence, so no
signal the owner already gets is replaced by a range one. Never relax or
"improve" a strategy rule without the owner's explicit decision —
implementation over-strictness may be fixed, the rules themselves may not.
"Almost valid" does not exist in this system.

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
touch (a body close through the far edge), no M5 CHoCH — plus two geometry
checks that run after all of them: `risk <= 0` (Rule 6, SL lands at the
entry level) and an entry on the wrong side of its own stop are malformed
trades, not judgment calls, and still return `Verdict.SKIP` rather than a
warning. "No valid FVG" left this list on 2026-08-30 (D22).

## Commands

```bash
pytest tests/ -v                       # full test suite (fast, no network)
flake8 app/ tests/ smc_watcher.py smc_backtest.py   # lint (config in .flake8)
python smc_watcher.py --once           # live one-shot check (real market data)
python smc_backtest.py --pair USDJPY --days 365     # walk-forward backtest
                                       # (forex needs TWELVEDATA_API_KEY;
                                       # history cached in data/backtest/)
python smc_backtest.py --selftest      # offline pipeline check on synthetic
                                       # candles — no network, no keys
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
                          setup cards, discipline suppression, 07:55 weekday
                          news digest, /plan -> Strategy audit on fresh
                          candles with Claude's read (D27), Rule 0.4
                          warnings, journal tracking, per-cycle plan + audit
                          recompute into the planbook, the per-pair daily
                          setup cap (D25). Legacy behind default-off flags:
                          08:05/14:05 plan summaries (SMC_AUTO_PLAN),
                          plan-centric zone alert, PD radar, 🔁
                          plan-updated message
smc_backtest.py           backtest CLI: cached history -> backtest.py ->
                          plain-text report (see the commands block)
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
│                         find_order_block (the M5 order block: a deeper
│                         second entry inside the zone-touch→FVG window,
│                         kept only when it is deeper than the entry and
│                         inside the stop — and, since D22, the entry
│                         itself over the touch→CHoCH window when no valid
│                         imbalance formed, where no such guard applies),
│                         m5_marks (the
│                         M5 order block + imbalance inside a touched zone,
│                         label-only, for the 🔔 alert's 🔎 line)
├── range.py              detect_range: clusters confirmed H1 pivots
│                         (structure.find_pivots) into two boundaries,
│                         each needing 2+ touches and the box at least
│                         3x tolerance tall, or it's chop, not a range;
│                         broken on an H1 body close beyond a boundary —
│                         a pierce that closes back inside does not break
│                         it (D9/D15), it is a sweep (boundary_swept,
│                         D16); boundary_zone exposes a boundary as a
│                         Zone(kind="RANGE") for the existing pipeline;
│                         boundary_excursion_start heals the excursion a
│                         raid splits, so Rule 6's stop clears the whole
│                         raid
├── liquidity.py          unswept swing highs/lows + EQH/EQL pools;
│                         nearest_liquidity (Rule 7 take-profit target) and
│                         liquidity_ladder (five rungs shown in the alert)
├── pd.py                 premium/discount: dealing_range (last confirmed
│                         swing low + high of one timeframe), resolve_range
│                         (H4 first, H1 fallback, each accepted only while it
│                         CONTAINS the reference price — owner decision D17),
│                         position as a fraction of the box, ote (the 62-79%
│                         retracement band), read() -> PDRead. Feeds the ⭐'s
│                         pd condition, the PD line on setup alerts, the PD
│                         radar and /pd
├── sniper.py             sniper-tier primitives (Phase 2 redesign): room_r
│                         (H1/H4 liquidity room ahead of entry), sweep_label
│                         (named pool taken by the touch→CHoCH excursion;
│                         PDH/PDL and Asia read off M5 AND H1 so the day is
│                         whole, every level sliced at the touch),
│                         pd_state (which half of the range the entry sits
│                         in — the range comes from pd.py, see D17),
│                         classify (the ⭐ verdict, checked after a
│                         setup already fully formed — detector mode
│                         unchanged — denied when H4/H1 trend disagree
│                         (owner decision D6) or when the impulse left no
│                         valid M5 imbalance (owner decision D22))
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
├── ai_read.py            D26: Claude's second opinion (AIReader, Sonnet 5 by
│                         default): the chart PNGs + describe_for_ai's fact
│                         sheet of the engine's own numbers in, a bounded
│                         JSON read out (stance / preferred entry /
│                         confidence / read / risks). Comment only, never a
│                         gate; every failure returns None. Appended to the
│                         🚨 card by editing it, stored with the audit
├── pending.py            D25: pending (limit) entries — build_pending
│                         prices the MAIN/DEEP rungs (M5 FVG edge / 50%, M5
│                         OB, H1 zone, zones further out, range boundary)
│                         with entry / SL / TP1-3 for the Strategy-audit
│                         button. Pure geometry over an engine result;
│                         liquidity.take_profits supplies TP1-3 (three
│                         nearest unswept pools)
├── planbook.py           in-memory current-plan store (PlanBook/PlanEntry,
│                         since D25 also the engine result + the audit),
│                         filled by the 08:05/14:05 snapshot and by the
│                         per-cycle recompute; material fingerprint
│                         (plan_fingerprint) drives silent summary edits;
│                         scenario_for_touch backs the plan-zone alert
├── chart.py              alert chart PNG (M5) + plan chart PNG (H1): candles,
│                         zones, levels (matplotlib Agg, NO pandas — keep it so)
├── backtest.py           walk-forward replay (spec 2026-08-30, D20): drives
│                         the PRODUCTION engine.evaluate + journal semantics
│                         + the watcher's dedup fingerprint over historical
│                         candles, same 300/400/400 closed-candle windows the
│                         live fetchers serve; stats per tier/direction
│                         source/session block + warning autopsy. Reports
│                         state what v1 does NOT simulate: news blackout,
│                         discipline, funding, correlation (D21). Offline —
│                         never imported by the watcher
├── history.py            historical candle download for the backtest CLI:
│                         Twelve Data paged via the production fetcher's
│                         _request/parse (rate limiter + key redaction) and
│                         Binance klines, behind a JSON disk cache in
│                         data/backtest/ (gitignored) that only fetches the
│                         ranges it does not already cover
├── telegram_bot.py       long-polling commands (/plan /journal /news
│                         /settings /help since 2026-09-10; /pairs /pause
│                         /resume still answer when typed but left the menu),
│                         the ⚙️ /settings hub (language, pairs, setup-alert
│                         level, pause — every press edits one message in
│                         place, callbacks `st_*`), slash-menu registration
│                         in the current language, Took/Skipped callbacks;
│                         serves ONLY the owner chat
├── i18n.py               bot-facing text in two languages (owner request
│                         2026-09-10): `t("English key", **fields)` looks the
│                         key up in the RU catalog when the language is
│                         Russian (the default, SMC_LANG) and returns the
│                         key itself in English; `set_language` is
│                         process-wide, applied from `state.language` at
│                         startup and by the /settings button. Trading
│                         vocabulary (CHoCH, FVG, OB, LONG/SHORT, SL/TP,
│                         Supply/Demand, PD, OTE, RR) stays Latin in both
├── trade_journal.py      /journal: MT4 history screenshots parsed by Claude
│                         (vision, D27) into trades behind a Save/Cancel
│                         step; stats text
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

- **Bot-facing text is bilingual, Russian by default** (owner request
  2026-09-10; `SMC_LANG=ru|en` is the first-start default, the ⚙️
  `/settings` choice is stored in the kv store and wins afterwards).
  Every string the owner can see — cards, buttons, the slash menu, engine
  reasons/warnings, plan notes, the digest, the journal, chart labels —
  is written in English in the code and wrapped in `i18n.t(...)`; the
  English text IS the catalog key, `i18n.RU` maps it to Russian, and a
  missing entry degrades to English (tests pin that every literal key in
  the code is translated and that placeholders match). Format numbers
  BEFORE passing them as `t()` fields; `t` never escapes — the caller
  still runs every dynamic value through `escape_html`. Terms stay Latin
  in both languages (CHoCH, FVG, OB, LONG/SHORT, SL/TP, H4/H1/M5,
  Supply/Demand, PD, OTE, EQH/EQL, RR); only prose is translated. The AI
  read is asked for the bot's language (`ai_read.LANGUAGE_INSTRUCTION`,
  enum values stay English). Logs stay English. The test suite runs in
  English (`tests/conftest.py` pins it) so assertions read as written;
  `smc_backtest` pins English too because `backtest.WARNING_BUCKETS`
  matches the engine's warning text. Do not shadow `t` with a loop
  variable in a module that imports it. Conversation with the owner is
  Russian (address him as «Брат»). Message timestamps are **Prague time**.
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
- **The daily schedule is wall-clock, and the scheduler wakes for it**
  (owner request 2026-08-31): red-news digest **07:55**, auto-plan
  snapshots **08:05 / 14:05** Prague (`SMC_NEWS_DIGEST_TIME`,
  `SMC_AUTO_PLAN_TIMES`). Both fire from inside `run_cycle`, so a time off
  the cadence grid would be served by the NEXT tick — 08:05/14:05 sit on
  the 5-minute in-session grid, but 07:55 sits between the off-session
  ticks at 07:45 and 08:00. `Watcher._wake_slots` therefore feeds BOTH the
  plan slots and the digest time into `_seconds_until_next_wake`, and the
  scheduler sleeps to whichever comes first. Change a time and the wake
  follows it; nothing else needs touching.
- **Quiet mode is the default**: Telegram receives only found setups (and
  Rule 9/0.4 warnings + the 07:55 digest). Everything else goes to logs.
  Do not add chatty messages without being asked. The Rule 0.4 pre-news
  warning is **one message per release** (owner request 2026-09-10, after
  four identical Core PPI warnings for four ETHUSD journal rows): every
  exposed pair is listed under the event with what is at risk (an open
  position → SL to breakeven, a limit order → cancel), dedup key
  `event:<currency>:<time>:<title>` in `state.news_warned`, written only
  after the send succeeded. The **data-source warning** reaches Telegram
  only for a credentials failure on a forex pair (an expired TwelveData
  key — `_looks_like_auth_failure`, owner request 2026-09-10): a
  timeout, a rate limit, a 5xx or any Binance error is logged and never
  sent, the next cycle just fetches again.
- **Two modes, minimal messages** (owner decision D25, 2026-09-05, tightened
  the same evening). Telegram receives exactly two kinds of trading message:
  the **07:55 news digest** and the 🚨 **setup alert**, which now means
  "enter at market": it prints `📈 Enter at market <price>` with the Rule 6
  stop, the risk and `🎯 TP1/TP2/TP3` — the three nearest unswept pools off
  the ladder (`liquidity.take_profits`), RR from the market price. The
  Phase 2 hybrid exit (TP1 2R / runner 3R) is still computed and still
  drives the journal; it just no longer prints. **Nothing is sent before a
  setup completes** — there is no get-ready message of any kind: the
  plan-zone alert (`SMC_ZONE_PING`), the PD radar (`SMC_PD_ALERT`) and the
  🔁 plan-updated message (`SMC_PLAN_CHANGE_ALERT`) are legacy, off by
  default (code and tests kept, one flag each to restore), and the
  short-lived "price is almost there" alert was removed outright.
  Everything about **limit entries lives behind the buttons**: the pair
  buttons under the silent 08:05/14:05 summary (`aplan_*`,
  `Watcher.on_setup_analysis`) answer with the **Strategy audit**
  (`format_setup_analysis`): the checklist state and
  `pending.build_pending`'s two pending (limit) entries, MAIN (the
  shallowest rung: the Rule 5 entry, or the zone's near edge while waiting)
  and DEEP (the deepest distinct rung: M5 OB, the H1 zone, the next
  untested zone), each with entry / SL / TP1-3 / RR in a `<pre>` table;
  mid-range it shows one bracket per boundary. The audit is **computed on
  schedule and delivered on demand**: the 08:05/14:05 snapshot builds it
  fresh, every cycle's `_recompute_plan` refreshes it from the candles the
  engine already fetched (`PlanEntry.result` / `PlanEntry.audit`, via
  `Watcher._audit`), and a button press costs zero API calls — only an
  empty book (a restart before any cycle) fetches, and an entry without an
  audit falls back to the plan text. **Daily cap**:
  `SMC_MAX_SETUPS_PER_DAY` (2) setup alerts SENT per pair per Prague day
  (`state.daily_counts`, stamped with the result's `checked_at` on both
  the check and the bump); a regular setup past the cap is
  journal-recorded and dedup-fingerprinted but not sent (same contract as
  `/notify star`), a ⭐ always goes through, 0 disables. **Both tiers get
  the full card**, the ✅/❌ buttons, the live card and the chart — only the
  pin stays a ⭐ privilege, and the regular card carries `🔹 Missed for ⭐:
  …` where the star header would be. Detector mode is untouched: nothing
  here decides whether a setup exists.
- **The minimal bot** (owner decision D27, 2026-09-06). Telegram gets two
  kinds of trading message — the 07:55 digest and the 🚨 market-entry
  card (with Claude's read appended) — and, since 2026-09-10, five
  commands: `/plan` (the Strategy audit for a pair or ALL on FRESH
  candles: the pending entries table, the H1 chart and Claude's read in
  one message — `Watcher.on_plan` → `_send_setup_analysis(fresh=True)`),
  `/journal` (a photo message parses an MT4 history screenshot), `/news`,
  `/settings` (ONE ⚙️ menu for every setting, owner request 2026-09-10:
  🌐 language ru/en, 📊 pairs on/off, 🔔 setup-alert level — the retired
  `/notify` levels all / ⭐ only / none, back as buttons — and ⏸/▶️
  pause; the language button also re-registers the slash menu in the new
  language) and `/help`. `/pairs`, `/pause` and `/resume` still answer
  when typed (muscle memory) but are gone from the menu and from `/help`.
  Retired from the menu and the handlers: `/notify`, `/check`, `/status`,
  `/pd`, `/stats`, `/unmute` (their watcher-side text builders —
  `status_text`, `stats_text`, `pd_text` — still exist as plumbing). The
  scheduled 08:05/14:05 plan summaries are legacy, off by default
  (`SMC_AUTO_PLAN`); a legacy `aplan_*` press answers exactly like
  `/plan`. The MT4 screenshot parser moved from OpenAI to Claude
  (`trade_journal.py`, `settings.anthropic`) — one `ANTHROPIC_API_KEY`
  for everything AI, `OpenAISettings` is gone.
- **The plan is primary** (owner decision 2026-09-10, «план — главный»).
  Every `/plan` press stores the pair's plan in the kv store
  (`state.primary_plan[pair]`, shape `planbook.primary_plan_snapshot`:
  the zones the plan showed, the MAIN/DEEP pending prices and Claude's
  read) and re-writes the older `plan_zones` provenance the D27 `/plan`
  had stopped writing. The 🚨 alert is that plan's continuation: `_send_alert`
  reads the formed setup against it (`planbook.match_primary_plan` — same
  direction AND an overlapping zone, the `zone_was_planned` overlap rule)
  and the card carries a `📋` line: `Per the 10.09 14:05 plan: LONG · MAIN
  … · DEEP … · Claude: AGREE 4/5, preferred DEEP`, or `Not the 10.09 14:05
  plan (SHORT 1.3900–1.3920 there)`; no plan, no line (the `in_plan`
  provenance line then falls back in). Claude's alert read gets the same
  plan in its fact sheet (`describe_for_ai(plan=)`, "YOUR EARLIER PLAN")
  and is told to say whether this is the setup it planned. **Detector
  mode is untouched**: a mismatch is labelled, never suppressed, and a
  counter-H1 setup still arrives without the ⭐ and says so. The setup
  itself is still the engine's — the three-timeframe rule decides when it
  exists; the plan only says what the owner and Claude expected.
- **Audit polish** (owner picks, 2026-09-10): the pending table carries a
  `Size` row (Rule 8, `engine.position_size`, compact form; only with
  `SMC_DEPOSIT`); a `⏱ <session> ends in 1h05 (18:30 Prague)` clock sits
  under the price (`notifier.session_time_left`) and the same minutes go
  into Claude's fact sheet; `liquidity_ladder`/`take_profits` take a
  `tolerance` and every caller passes the raw per-instrument `min_fvg`,
  so two pools within the sweep tolerance (2519.48 H1 / 2521.30 H4) are
  one objective, the richer pool kept — Rule 7's `nearest_liquidity` is
  untouched; chart level and zone labels are stacked (`chart._stacked_y`,
  `LABEL_SEP_FRAC`) so an entry and a stop a few dollars apart do not
  overprint, the lines stay on price. Claude's alert stance and
  confidence are stored on the signal (`ai_stance`, `ai_confidence`,
  `journal.attach_ai_read`) and `/journal` appends
  `journal.ai_accuracy_text` — wins (tp/tp1_be/tp1_runner) vs stops per
  stance over 90 days; expired/timeout rows never traded and are left out.
- **The AI read is a comment, never a gate** (owner decision D26,
  2026-09-06). `ai_read.AIReader` (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
  Sonnet 5 by the owner's choice; `SMC_AI_READ`, `SMC_AI_EFFORT`,
  `SMC_AI_TIMEOUT_S`) shows Claude the same picture the owner gets — the M5
  setup chart, the H1 plan chart and `describe_for_ai`'s fact sheet of the
  engine's own numbers — and asks for a bounded JSON read (`READ_SCHEMA`:
  stance agree/caution/against, preferred entry market/main/deep/wait,
  confidence 1-5, a three-sentence read, up to three risks). It runs at
  exactly two points: **after** a 🚨 alert has been sent (`_ai_read_alert`
  edits the card to append the `🧠 AI read` block and re-stores the text
  via `journal.attach_message`, so live-card edits keep it) and with each
  **fresh audit** (`_fetch_pair_plan` → `_ai_read_audit`, i.e. every
  `/plan` press since D27, or a legacy 08:05/14:05 snapshot);
  `_recompute_plan` carries the stored read forward rather than paying
  for one every five minutes. Nothing about it suppresses, delays,
  re-labels or re-prices a setup: the alert goes out first, the block is
  appended afterwards, and a missing key, a network error, a refusal or an
  unparsable answer all leave the bot exactly as it was before D26. The
  fact sheet is the model's only source of levels (the prompt says so). All
  model text goes through `escape_html` like any other dynamic string.
  `anthropic` is imported lazily, so the watcher and the tests run without
  the package or a key.
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
  all, same record/dedup guarantee) — and never touches the 07:55 digest,
  plan-zone alerts, or Rule 0.4/9 warnings.
- **Sniper tier** (`sniper.py`, Phase 2 redesign): a completed setup earns
  ⭐ when room >= 1.0R on H1/H4 liquidity (or unmeasurable), a pool was
  swept, premium/discount is ok (or unmeasurable — see D17 below for the
  range that is measured on), the entry isn't stale
  beyond `SMC_MAX_ENTRY_GAP_R` (0.75R), and H4/H1 trend do not point
  opposite ways (owner decision D6, 2026-08-16) — two alert tiers, both
  still sent (detector mode): a disagreeing setup fires without the star,
  and the alert header labels the disagreement (`⚠️ counter-hourly`).
  Every **trend** fill uses the hybrid exit regardless of tier: TP1 at
  `SMC_TP1_R` (2R) closes half and moves SL to break-even, the runner rides
  to `SMC_RUNNER_R` (3R) — journal statuses `tp1_be` (BE stop after TP1)
  and `tp1_runner` (runner target hit) track it. A **range** setup has no
  hybrid exit at all (owner decision D14, 2026-08-18): it carries no
  `tp1`/`runner_tp`, and `journal.evaluate_signal` tracks it on
  `take_profit` like any non-hybrid signal — a row stored before D14 is
  disqualified by its `zone_kind == "RANGE"`.
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
  it is *entering*. The beaten imbalance is not simply discarded: when the
  order block wins, `engine.py` looks it up again with `find_h1_fvg_zone`
  and, if it is a genuinely deeper entry than the live one, prepends it to
  `zones_ahead` (the zone ladder) — otherwise it is lost, same as any
  ladder candidate that fails the deeper-entry check. `m5_marks` (the M5
  order block + imbalance inside a touched zone, shown on the 🔔 alert's
  `🔎` line) is label-only: no verdict, no suppression, no message of its
  own, and Rule 4's session/fill checks deliberately do not apply to it —
  but it does take the **earliest** qualifying gap of the excursion, which
  is `select_valid_fvg`'s rule, so the 🔎 line and the 🚨 alert's ⚡ line
  name one imbalance rather than two.
- **The counter-H4 setup is shown, never starred** (owner decision D23,
  2026-08-31). When H4 and H1 trend OPPOSITE ways, the watcher runs a
  SECOND pure `evaluate(force_direction=...)` pass on the candles the first
  pass already fetched (no extra API call) and announces the H1-side setup
  alongside the H4-side one. It ADDS, it never replaces: Rule 1 still
  resolves the primary direction exactly as before, and the counter result
  carries `direction_source="h1_counter"` so the header reads "⚠️ against
  H4" and `trends_disagree` (D6) denies it the ⭐. `force_direction`
  bypasses Rule 1's whole ladder, so a forced pass can never wander into
  the range (D11) or aggressive-CHoCH branches. The two tracks dedup in
  SEPARATE slots (`state.last_setup` vs `state.last_counter_setup`) — one
  shared slot would let each side's fingerprint clear the other's and
  re-announce both every cycle. Only a COMPLETED counter setup is
  announced; a counter-trend WATCH is noise.
- **A plan correction reaches Telegram as its own message** (owner request
  2026-08-31; LEGACY since D25 — off unless `SMC_PLAN_CHANGE_ALERT=true`). The 08:05/14:05 summary is still edited silently on every
  material change — that message must always show the truth — but the pairs
  that actually moved now also get a `🔁 Plan updated` message naming what
  moved (`planbook.describe_plan_changes` over `plan_snapshot`, the
  fingerprint's own facts in a diffable shape). Throttled to one message
  per pair per hour (`state.plan_change_notified`): the plan is recomputed
  every five minutes, and an unthrottled announcement would turn quiet mode
  inside out. The silent edit is NOT throttled. A pair with no stored
  snapshot stays silent — there is nothing honest to diff against — and the
  throttle timestamp is written only after the send actually succeeds.
- **The imbalance labels a setup, it no longer defines one** (owner
  decision D22, 2026-08-30). Rule 4 still runs and still prefers the valid
  M5 gap — when `select_valid_fvg` returns one it is the entry, exactly as
  before, and every pre-D22 signal is byte-for-byte unchanged. When it
  returns nothing the setup is still announced, and Rule 5 walks an entry
  ladder instead: the **M5 order block** of the same touch→CHoCH excursion
  (`find_order_block` with no deeper-guard — there is no FVG entry to be
  deeper than, and the block sits inside the stop by construction), else
  **price itself**, a market entry on the CHoCH. `TradeSetup.entry_source`
  ("fvg"/"ob"/"market") says which rung paid, and it is what names the
  ladder's RR column and the `entry_is_market` band. A gap that FAILED Rule
  4 never becomes an entry — it is carried on `rejected_fvg` /
  `rejected_fvg_problems` purely so the alert can show it and say what was
  wrong with it: entering at the edge of a half-filled gap is entering at a
  price the market has already left. The cost of missing the imbalance is
  the ⭐ (`sniper.classify`'s sixth condition), never the message.
  `SMC_REQUIRE_IMBALANCE=true` restores the pre-D22 gate without a deploy,
  the same escape hatch `SMC_PD_BASIS=h1` gives D17. Rule 6 is untouched:
  the stop never read the imbalance.
- **A range boundary is a `Zone` with `Zone.kind == "RANGE"`**
  (`range.boundary_zone`): expressing it that way is what lets the existing
  Rule 3/4/6 pipeline — `zone_touch_span`, `find_choch`, `select_valid_fvg`,
  `find_order_block`, `sweep_extreme` — run on a range boundary unchanged;
  only the stop and the target differ. The stop sits beyond the boundary
  itself (or the swept extreme, whichever is further out) plus `sl_buffer`;
  the target is the **opposite boundary**, one `sl_buffer` short of it, not
  Rule 7's nearest unswept liquidity — taken full size, with no hybrid exit
  (D14). Messages name a boundary a boundary: never "H1 Supply/Demand
  zone", and the range alert carries the box, the target and its RR on both
  tiers. `range.boundary_swept` (D9/D16, owner decision 2026-08-18) counts
  any pierce of a boundary that price came back inside from as liquidity
  taken — the pierce may close beyond the boundary or merely wick through
  it, because D15 made the body-close raid tradeable and that raid is the
  one the owner calls a sweep. The stop follows the same reading: a
  boundary band is one tolerance thick, so a raid printing a candle wholly
  outside it splits the excursion `zone_touch_span` measures;
  `range.boundary_excursion_start` walks that split back so Rule 6 anchors
  beyond **every** candle of the raid, and it is used for RANGE zones only
  (the OB/FVG paths and `m5_marks` keep `zone_touch_span` untouched).
  A body close beyond a boundary breaks it only while the break **still
  holds** (D15, owner decision 2026-08-18) — both for `Range.broken` on H1
  and for Rule 3's invalidation of a RANGE zone on M5 (`engine._zone_broken`,
  mirroring `structure._break_still_holds`); pierce-and-reclaim is the
  stop-hunt the owner trades, not a breakout. OB and FVG zones keep the
  one-way invalidation memory, which is load-bearing. The ⭐ verdict asks
  the same sweep question at a
  tighter scope (D13, owner decision 2026-08-18): only a sweep inside the
  setup's own touch→CHoCH excursion earns it, the same excursion
  `sweep_extreme` is anchored on, so a boundary raided once, long before
  this setup formed, does not star every touch afterward. D11 (owner
  decision 2026-08-18) puts the range behind the existing H1-trend
  fallback in Rule 1's precedence — it only trades when **both** H4 and H1
  read FLAT, so no signal the owner already gets is replaced. D12: when a
  range is in play, its two boundary scenarios **replace** the plan's
  speculative both-way breakout brackets rather than joining them.
- **An excursion into a zone begins after the zone exists**
  (`zone_touch_span`): a candle joins the span only when its timestamp is
  at/after `zone.timestamp` AND it trades *strictly* inside the band —
  meeting an edge is not a touch, which is exactly how `measure_fill`
  already measures penetration, so the two can never disagree about
  whether price is in the band. Without both conditions a fresh H1
  imbalance read as "price is in the zone" from the moment it formed (a
  gap is a band price just left, and the impulse candle sits on its edge):
  Rule 3's pullback-wait state became unreachable and the touch anchor fed
  a pre-zone index to the M5 CHoCH search, Rule 4's floor and
  `sweep_extreme` — the Rule 6 stop reference. Both conditions are
  kind-agnostic; nothing branches on `Zone.kind` for geometry. The time
  filter stands down only when the whole candle list ends before the zone
  formed (incoherent fixtures; live data cannot produce it), the same
  fallback `engine.evaluate` already carries for `first_zone_touch`.
- **Premium/discount is measured on an explicit dealing range** (`pd.py`,
  owner decision D17, 2026-08-26). `resolve_range` asks H4 first — H4 is
  where Rule 1 takes the direction from — and falls back to H1, accepting
  either only while it still CONTAINS the reference price; when neither
  does, price is expanding rather than retracing and the answer is None,
  which `sniper.classify` already treats as unmeasurable-and-passing. The
  ⭐'s verdict is unchanged in shape (`sniper.pd_state` still asks which
  half of the range the ENTRY sits in) — only the box it asks about is.
  `SMC_PD_BASIS=h1` restores the pre-audit last-two-H1-pivots reading
  without a deploy. The number now leaves the code: `AnalysisResult.pd`
  carries a `PDRead`, the 🚨 alert prints `PD 24% discount (H4 …) · OTE …`
  and the quiet 🔹 line appends `· PD 62% premium (H4)` right after
  `Missed for ⭐:`, so the most common star-blocker is finally readable.
  OTE (62-79% retracement) is label-only: it marks a message, never a gate.
- **PD radar** (`SMC_PD_ALERT`, owner request 2026-08-26; LEGACY since
  D25 — off by default, no get-ready messages, `/pd` still answers on
  demand): one alert per
  pair per **side** per session block when price reaches the half of its
  dealing range the bias wants — discount under a long bias, premium under
  a short one. It fires ONLY with the bias (a discount under a downtrend is
  the trend working, not an entry), takes that bias from Rule 1's own
  precedence (H4, then H1 on H4 FLAT, nothing when both are flat — the
  range state D11), stands down when a plan-zone alert already went out
  this cycle, and never fires once a setup has formed (the 🚨 alert carries
  its own PD line). Dedup lives in `state.pd_pinged`; `/pd` answers the
  same question on demand from the last completed cycle, never a fresh
  fetch.
- **Plan-centric zone alert** (spec 2026-08-11 §5, dedup rewritten by owner
  decision 2026-08-16; LEGACY since D25 — off unless `SMC_ZONE_PING=true`,
  no get-ready messages by default): fires when price touches a zone named by the pair's
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
  the day's last block) and silences that pair's **get-ready** alerts —
  plan-zone alerts and the PD radar, which carries the same button (owner
  request 2026-08-26). Setup alerts, Rule 0.4 warnings and the 07:55
  digest ignore it. `/unmute`
  clears every mute; `/status` lists them.
  `state.remember_plan_zones` (the separate "from this morning's plan"
  provenance, spec 2026-08-06 §6) is still written **only** by the
  08:05/14:05 snapshots and manual `/plan` — never by the 5-minute
  recompute.

## Workflow

- Work on a feature branch, PRs to `master` via `gh`
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
