# Sniper Redesign — Phase 2: Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the validated sniper redesign into the live bot: hybrid 2R+3R exit, ⭐ two-tier alerting (room + sweep + pd + freshness), journal partial-close lifecycle, roster ETHUSD/USDJPY/USDCAD.

**Architecture:** A new pure module `app/services/smc/sniper.py` holds the tier primitives (ported from the validated `sn_rules.py`, with the test coverage the harness deferred). The engine attaches hybrid TP levels and a tier verdict to each setup; the journal gains the partial-close state machine (ported from validated `sn_exit.py` semantics); the watcher/notifier split alerts into ⭐-full and quiet tiers. A replay re-cut on the FVG basis gates the merge.

**Tech Stack:** Python 3, pytest (asyncio_mode=auto, network-free synthetic candles via tests/test_smc/helpers.py), flake8, SQLite (in-place column migration).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-12-sniper-redesign-design.md` incl. the **Phase 2 locked decisions** section — those values are law: runner 3.0R, TP1 2.0R on half, entry = M5 FVG (OB stays info line), ⭐ = room_r ≥ 2.5 (H1/H4 pools, None passes) AND sweep present AND pd ∈ {ok, None} AND no Rule 5.1 "run past" warning.
- **Detector mode is unchanged:** nothing that suppresses before a setup completes changes; after completion nothing is suppressed — the tier only routes loud vs quiet.
- Reference implementations (semantics source of truth, verified in Phase 1): `C:\temp\926_bot_data\scripts\sn_rules.py` (sweep/dealing-range/pd) and `sn_exit.py` (hybrid state machine: adverse-first on every ambiguous candle; TP1+SL same candle = SL; runner+BE same candle = BE; BE/runner judged from the candle AFTER the TP1 candle; timeout-after-TP1 realizes +1.0R — this last one FIXES a known Phase 1 harness simplification).
- Outcome R accounting: sl → −1.0; tp1_be → +1.0; tp1_runner → 0.5·2.0 + 0.5·3.0 = **+2.5**; expired → 0.0.
- All bot-facing text English; every dynamic value through `notifier.escape_html`; only `<b>`/`<pre>` tags; timestamps Prague.
- Per-instrument params from `instruments.py` only; liquidity tolerance = raw `Instrument.min_fvg`, never profile-scaled.
- DB: new signal columns extend `SIGNAL_COLUMNS`, the `CREATE TABLE`, AND the migration list in `db.py` (production DBs migrate in place). `db.py` must never crash the watcher.
- Chart rendering must never block an alert; `chart.py` stays matplotlib-only.
- `pytest tests/ -v` and `flake8 app/ tests/ smc_watcher.py` green before every commit; regression tests for ported semantics (incl. SHORT-sweep, EQ-pool, boundary cases — deferred from Phase 1 by ledger ruling).
- Candle pickles in `C:\temp\926_bot_data\candles\` remain READ-ONLY.
- Branch: `feat/sniper-redesign` (continues Phase 1's docs commits).

---

### Task 0: FVG-basis validation re-cut (analysis, gates the merge)

**Files:**
- Create: `C:\temp\926_bot_data\scripts\sn_fvg_recut.py` (outside repo, no commit)
- Reference: `sn_run.py`, `sn_analyze.py`, `sn_variants.py`, `sn_exit.py`

**Interfaces:**
- Consumes: the existing year JSONL records + candle pickles.
- Produces: `sn_fvg_recut.json` + a verdict appended to `C:\temp\926_bot_data\reports\sdd-sniper-replay\variants-report.md`; later tasks proceed only if the green light holds.

- [ ] **Step 1:** The year must be re-run with tier inputs at the FVG entry (records computed room_r/pd at `entry_used`, and pd's dealing range is not recoverable post-hoc). Modify a COPY of `sn_run.py` (`sn_fvg_recut.py`) so `entry_used = entry_fvg` always (`entry_kind = "fvg"`), room_r/pd computed at that entry. Re-run all 18 chunks (same commands, ~8 min).
- [ ] **Step 2:** Analyze with the FINAL config: study population (no "past the imbalance" warning), idea counting, V2+ tier (room ≥ 2.5/None pass, sweep required, pd ∈ {ok, None}), hybrid exit at runner 3.0, train/test at 2026-02-05.
- [ ] **Step 3:** Verdict: green light holds iff star-tier idea-R is positive on BOTH halves. Record star counts/week, R per half, giant-winner capture. Append to variants-report.md. **If it fails, STOP the plan and report to the owner** — the remaining tasks assume this config.

---

### Task 1: `app/services/smc/sniper.py` — tier primitives with full tests

**Files:**
- Create: `app/services/smc/sniper.py`
- Test: `tests/test_smc/test_sniper.py`

**Interfaces:**
- Consumes: `models.Candle/Direction/Zone`, `structure.find_pivots`, `liquidity.find_liquidity/nearest_liquidity`.
- Produces (consumed by Task 2):
  - `sweep_label(m5, h1, direction, touch_idx, choch_idx, tolerance, ts_utc) -> Optional[str]`
  - `dealing_range(h1) -> Optional[Tuple[float, float]]`
  - `pd_state(direction, entry, rng) -> Optional[str]` ("ok"/"bad"/None)
  - `room_r(h1, h4, direction, entry, risk, tolerance) -> Optional[float]` (distance to nearest H1/H4 pool ahead / risk; None if no pool)
  - `@dataclass TierVerdict: star: bool; missed: List[str]` and `classify(room, sweep, pd, stale: bool) -> TierVerdict` — star iff `(room is None or room >= 2.5) and sweep is not None and pd in ("ok", None) and not stale`; `missed` lists failing condition names from `("room", "sweep", "pd", "stale")`.

- [ ] **Step 1:** Port `sweep_label`/`dealing_range`/`pd_state` from `C:\temp\926_bot_data\scripts\sn_rules.py` verbatim in behavior (Prague day boundaries, PDH/PDL > Asia > EQ > swing priority, strict `<`/`>` crossing, levels as-of-touch `m5[:touch_idx]`), drop the unused `timedelta` import, add `room_r` (the `sn_run.py` computation: `find_liquidity(h1,"H1",tol)+find_liquidity(h4,"H4",tol)` → `nearest_liquidity(levels, direction, entry)` → distance/risk) and `classify`.
- [ ] **Step 2:** Write failing tests FIRST covering: the 5 ported Phase 1 cases (adapted from `sn_test_rules.py`), PLUS the ledger-mandated gaps: SHORT-direction PDH sweep; EQ-pool label (`EQH(2)` from two equal highs within tolerance); H1-sourced pool sweep; `extreme == level` boundary (strict — no sweep); `dealing_range` low ≥ high → None; `pd_state` at exactly mid (LONG ok, SHORT ok — `<=`/`>=`); `room_r` with no pool → None; `classify` truth table incl. each single-miss and `stale=True`.
- [ ] **Step 3:** Run `pytest tests/test_smc/test_sniper.py -v` (fail → implement → pass).
- [ ] **Step 4:** `flake8 app/services/smc/sniper.py tests/test_smc/test_sniper.py`; commit `feat: sniper tier primitives (sweep/pd/room/classify)`.

---

### Task 2: Engine — hybrid TP levels + tier verdict on the setup

**Files:**
- Modify: `app/services/smc/models.py` (TradeSetup + AnalysisResult), `app/services/smc/engine.py` (after Rule 6/risk, alongside the existing Rule 7 label block)
- Test: `tests/test_smc/test_engine.py` (extend)

**Interfaces:**
- Consumes: Task 1's functions.
- Produces (consumed by Tasks 3–4): `TradeSetup.tp1: Optional[float]`, `TradeSetup.runner_tp: Optional[float]`, `TradeSetup.tier_star: bool = False`, `TradeSetup.tier_missed: List[str]`; config `SMCSettings.tp1_r: float = 2.0`, `SMCSettings.runner_r: float = 3.0` (`SMC_TP1_R`/`SMC_RUNNER_R` in env.example); engine ctor args `tp1_r=2.0, runner_r=3.0`.

- [ ] **Step 1:** Failing tests: a full synthetic setup asserts `tp1 == entry + 2.0*risk` and `runner_tp == entry + 3.0*risk` (long; short mirrored); a setup whose zone-touch swept a synthetic PDL, in discount, with far H1/H4 pools and gap ≤ 0.75R asserts `tier_star is True, tier_missed == []`; variants asserting each miss label ("sweep" when no pool swept; "stale" when price ran > 0.75R — reuse the existing stale-warning test fixture; "pd" only when pd == "bad" — a None dealing range must NOT miss); existing tests must stay green (entry/SL/Rule 7 labels untouched).
- [ ] **Step 2:** Implement: in `evaluate()` after `risk` is fixed, compute `tp1`/`runner_tp` from `tp1_r`/`runner_r`; compute tier via `sniper.sweep_label(m5, h1, direction, touch, choch, tolerance, result.checked_at)`, `sniper.pd_state(direction, entry, sniper.dealing_range(h1))`, `sniper.room_r(h1, h4, direction, entry, risk, tolerance)`, `stale = gap > self.max_entry_gap_r * risk` (the same comparison that emits the warning — compute once, use twice). Rule 7 ladder/target/warnings stay exactly as they are (info).
- [ ] **Step 3:** pytest + flake8; commit `feat: engine computes hybrid TP levels and sniper tier`.

---

### Task 3: Journal + DB — partial-close lifecycle

**Files:**
- Modify: `app/services/smc/journal.py` (`evaluate_signal`, `SignalJournal.record`, `update_pair`, `stats_text`), `app/services/smc/db.py` (`SIGNAL_COLUMNS`, `CREATE TABLE`, migration list)
- Test: `tests/test_smc/test_journal.py`, `tests/test_smc/test_db.py` (extend)

**Interfaces:**
- Consumes: Task 2's `tp1`/`runner_tp`/`tier_star`/`tier_missed` on the recorded setup.
- Produces: signal dict fields `tp1`, `runner_tp`, `tier` ("star"/"regular"), `result_r` (float, set on resolution); statuses extended with `"tp1_runner"`, `"tp1_be"` (existing: pending/open/tp/sl/expired/timeout — `tp` remains for legacy rows and for signals with no tp1, e.g. pre-migration).
- New DB columns: `tp1 REAL`, `runner_tp REAL`, `tier TEXT`, `result_r REAL` — added to `SIGNAL_COLUMNS`, `CREATE TABLE`, and the in-place migration list.

- [ ] **Step 1:** Failing tests porting the 7 validated `sn_test_exit.py` scenarios onto `evaluate_signal` with a signal carrying tp1/runner_tp (full-stop −1.0; tp1→be +1.0; tp1→runner +2.5; TP1+SL same candle = sl; runner+BE same candle = tp1_be; pending expiry; signal-candle watermark), PLUS: timeout-after-TP1 → status `"tp1_be"` with `result_r == +1.0` (the Phase 2 fix — TP1 is banked, BE assumed on the rest, logged distinctly), and a legacy signal WITHOUT tp1 fields keeps today's exact tp/sl behavior byte-for-byte (regression).
- [ ] **Step 2:** Implement the state machine in `evaluate_signal` (guard: hybrid branch only when `signal.get("tp1")` is set; adverse-first; BE/runner judged from the candle after the TP1 candle — carry a `tp1_at` watermark). `record()` persists the new fields; `stats_text` counts tp1_runner/tp1_be as wins with their R and shows per-pair ⭐ performance (spec watch item: USDJPY star-negative must be visible).
- [ ] **Step 3:** DB tests: fresh schema has the columns; an existing DB file created from the OLD schema migrates in place and keeps its rows.
- [ ] **Step 4:** pytest + flake8; commit `feat: journal partial-close lifecycle + DB migration`.

---

### Task 4: Two-tier alerting — watcher + notifier

**Files:**
- Modify: `app/services/smc/notifier.py` (`_format_detector_alert` gains the tier header/levels; new `format_quiet_setup(result) -> str`), `smc_watcher.py` (`_send_alert` routes by `setup.tier_star`)
- Test: `tests/test_smc/test_notifier.py`, `tests/test_smc/test_watcher.py` (extend)

**Interfaces:**
- Consumes: Task 2 fields; Task 3 recording.
- Produces: ⭐ setups → today's full path (message with `⭐ SNIPER` header + TP1/runner lines, chart PNG, pin, Took/Skipped buttons, journal record). Non-⭐ → ONE short quiet message (no pin, no chart, no buttons), listing `tier_missed` as `Missed for ⭐: room, pd` style, still journal-recorded (tier "regular"). Dedup (`last_setup` fingerprint) unchanged and shared across tiers — a setup upgrading tiers within the same fingerprint does not re-alert.

- [ ] **Step 1:** Failing notifier tests: star alert contains `⭐`, `TP1` and runner values formatted with instrument decimals, all dynamic strings escaped (assert a crafted `<` in a zone label survives escaping); quiet message is < 400 chars, contains the missed-condition list, contains no `<pre>` ladder; both carry Prague timestamps.
- [ ] **Step 2:** Failing watcher test: a star result triggers send_photo+pin+buttons path; a regular result triggers exactly one send_message and no pin/photo/buttons; chart failure still delivers the star alert (existing try/except preserved).
- [ ] **Step 3:** Implement; keep quiet-mode discipline (no new chatter beyond the one quiet message per deduped setup).
- [ ] **Step 4:** pytest + flake8; commit `feat: two-tier sniper alerting`.

---

### Task 4b: Notification-level control (replaces the strategy picker; owner request 2026-08-12)

**Files:**
- Modify: `app/services/smc/state.py` (WatcherState), `app/services/smc/telegram_bot.py` (/notify command + callbacks, slash-menu registration, /strategy removal), `smc_watcher.py` (level check in the alert path)
- Test: `tests/test_smc/test_state.py`, `tests/test_smc/test_telegram_bot.py`, watcher tests (extend)

**Owner decisions:** one strategy ships (conservative), so the per-pair `/strategy` picker is retired and replaced by a GLOBAL notification level with an in-menu mute. Levels: `"all"` (⭐ loud + quiet regular), `"star"` (⭐ only; regular setups to logs), `"mute"` (no setup alerts at all). Mute affects SETUP alerts only — the 07:45 digest, plan-zone alerts and Rule 0.4/9 warnings (they concern already-open positions) keep flowing.

- [ ] **Step 1:** Failing tests: `WatcherState.notify_level` defaults to `"all"`, persists via the kv store, rejects unknown values; `/notify` renders the three options with the current one marked and its callbacks switch the level and edit the message; the slash-menu registration lists `notify` and no longer lists `strategy`; watcher: with level `"star"` a regular setup produces NO send_message but IS journal-recorded and its dedup fingerprint still updates (un-muting must not flood with stale setups); with `"mute"` a star setup is suppressed the same way (recorded, deduped, not sent); Rule 0.4 warnings and the digest path ignore the level.
- [ ] **Step 2:** Implement. `/strategy` command, its buttons and `set_profile` UI wiring are removed (engine profile plumbing stays — conservative is simply the only shipped profile); `set_profile`'s dedup-clearing behavior is no longer reachable from the UI and its tests are updated accordingly.
- [ ] **Step 3:** Full pytest + flake8; commit `feat: global notification level replaces strategy picker`.

### Task 5: Roster + config + docs

**Files:**
- Modify: `app/core/config.py` (`SMCSettings.pairs` default → `"ETHUSD,USDJPY,USDCAD"`), `env.example` (SMC_PAIRS, SMC_TP1_R, SMC_RUNNER_R), `CLAUDE.md` (detector-mode paragraph gains the two-tier sentence; architecture map gains sniper.py; conventions note the tier/partial-close semantics)
- Test: `tests/test_smc/` config pin test (extend wherever the pairs default is asserted)

- [ ] **Step 1:** Failing test pinning `SMCSettings().default_pairs == ["ETHUSD", "USDJPY", "USDCAD"]` and the tp1_r/runner_r defaults (2.0/3.0).
- [ ] **Step 2:** Implement; `WatcherState` runtime pair changes via /pairs are untouched (EURUSD/GBPUSD remain addable manually — the owner removes defaults, not capability).
- [ ] **Step 3:** Full `pytest tests/ -v` + `flake8`; commit `feat: sniper roster + config defaults + docs`.

---

### Task 6: Finish

- [ ] Full suite + flake8 green; final whole-branch review (subagent-driven flow); PR to master via `gh` per `.github/pull_request_template.md`, body summarizing Phase 1 evidence (link the spec §Phase 2 locked decisions) — the owner merges, Railway deploys.

## Self-review notes

- Spec coverage: locked decision 1 → Tasks 2/3 (runner 3.0); 2 → entry untouched + OB info line already shipped (no code change needed — asserted by existing engine tests); 3 → Tasks 1/2 (tier) incl. staleness leg; 4 → Task 0 gate; 5 → Task 3 stats. Spec §3 journal semantics → Task 3; §4 two tiers → Task 4; §1 roster → Task 5.
- Type consistency: `sweep_label/dealing_range/pd_state/room_r/classify` signatures match between Tasks 1 and 2; signal fields `tp1/runner_tp/tier/result_r` match between Tasks 2, 3, 4.
- The only behavior change to existing signals is guarded by `tp1` presence — legacy rows keep today's semantics (Task 3 regression test).
