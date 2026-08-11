# Review-Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the robustness/infra findings of the 2026-08-11 code review (HIGH №3–7 plus the approved medium/low items) so a bad feed, a flaky boot, corrupted state or an owner double-tap can no longer kill alerts or corrupt the journal.

**Architecture:** Surgical fixes inside existing modules — no new modules. Each fix lands with the regression test the review scenario describes (CLAUDE.md: a regression test for every production bug).

**Tech Stack:** Python 3.11, httpx, pytest (network-free), flake8.

**Branch:** `fix/review-hardening` (stacked on `feat/auto-plan`; PR base `feat/auto-plan`, auto-retargets to master when #50 merges).

## Global Constraints

- Every dynamic string in a Telegram message goes through `notifier.escape_html`; only `<b>`/`<pre>` tags.
- Quiet mode: no new chatty messages. Log lines are fine.
- Tests stay network-free and wall-clock independent.
- `python -m pytest tests/ -q` and `python -m flake8 app/ tests/ smc_watcher.py` green before every commit.
- Do not change strategy semantics anywhere — these are infra fixes.
- Commit bodies end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Message hygiene — OCR text and news errors

**Files:**
- Modify: `app/services/smc/trade_journal.py` (format_preview ~382, format_journal ~425/439 — every OCR-derived value)
- Modify: `app/services/smc/news.py` (~152: `fetch_error` interpolation)
- Test: `tests/test_smc/test_trade_journal.py`, `tests/test_smc/test_news.py`

**Contract:** Any string that came out of OpenAI Vision (symbol, direction, and any other free-text field these formatters interpolate — audit both functions end to end) and `news.fetch_error` pass through `escape_html` before landing in a parse_mode=HTML message. Numeric formatting of floats is exempt.

- [ ] **Step 1 (RED):** Regression tests: a preview/journal render with `symbol="S&P500"` and `direction="<buy>"` must produce `S&amp;P500` / `&lt;buy&gt;` and never the raw text; `digest_text` with `fetch_error=ValueError("bad <tag> & such")` must escape it. Run; verify failures show raw text.
- [ ] **Step 2 (GREEN):** Route every audited interpolation through `escape_html` (import exists or add from notifier). Re-run.
- [ ] **Step 3:** Full suite + flake8; commit `escape OCR-derived and news-error text in HTML messages`.

### Task 2: DB crash-loop and pair-list resurrection

**Files:**
- Modify: `app/services/smc/db.py` (`kv_get`, `kv_set`, `signals_all`, `signals_upsert`, `migrate_legacy_json` condition ~348)
- Modify: `app/services/smc/state.py` (~26: pairs load)
- Modify: `smc_watcher.py` (~285: the env-default block — keep behavior, adjust to the new state contract)
- Test: `tests/test_smc/test_db.py`, `tests/test_smc/test_autoplan.py` or a new `tests/test_smc/test_state_pairs.py`

**Contract:**
1. A `sqlite3.Error` raised by any read/write in `Database` after construction must not propagate: reads return the "empty" default (`None`/`[]`) after logging loudly AND trigger the same fallback-to-local-file path `_connect` uses (one attempt; if the fallback also fails, keep returning defaults — the watcher must boot and alert with ephemeral state rather than crash-loop). `migrate_legacy_json`'s gating `kv_get` call moves inside its own try.
2. `WatcherState` must distinguish "never set" from "deliberately empty": `db.kv_get("pairs")` returning `None` → default pairs; returning `[]` → stay empty (no resurrection after restart). The instruments filter still applies to non-empty lists. `smc_watcher.py`'s env-default block already checks `kv_get("pairs") is None` — verify it still composes.

- [ ] **Step 1 (RED):** Tests: (a) corrupt DB simulation — monkeypatch the connection/cursor to raise `sqlite3.DatabaseError("database disk image is malformed")` on execute; `kv_get` returns None, `signals_all` returns [], `WatcherState(db)` constructs, no exception escapes; (b) `kv_set("pairs", [])` → reload `WatcherState` → `state.pairs == []`; (c) `pairs` never set → defaults appear (existing behavior pinned).
- [ ] **Step 2 (GREEN):** Implement; keep the "log loudly" contract (`logger.error` per failure, throttled to once per method per process is fine).
- [ ] **Step 3:** Full suite + flake8; commit `db: never let SQLite errors crash the watcher; keep a deliberately empty pair list empty`.

### Task 3: Telegram transport — boot guard, JSON guard, backoff

**Files:**
- Modify: `app/services/smc/telegram_bot.py` (`_api` ~84, `run` ~124, polling loop ~127-141)
- Test: `tests/test_smc/test_telegram_transport.py` (new)

**Contract:**
1. `_api` never raises: transport errors AND non-JSON bodies (`response.json()` ValueError) are caught, logged, return None (mirror `notifier._api`'s discipline).
2. `run()`'s startup calls (`deleteWebhook`, `_setup_bot_profile`) are wrapped so a failure logs and continues into the polling loop — a flaky boot must not kill the process (the scheduler shares it).
3. When getUpdates yields no result because the API answered `ok: false` (409 second instance, 401 revoked token) or `_api` returned None, the loop sleeps with backoff before retrying: start 5s, double to a 300s cap, reset to normal cadence on the first successful poll. A 409/401 must also log at error level with the status/description when available.

- [ ] **Step 1 (RED):** Tests with a fake `_api`/httpx transport: (a) `_api` returning a non-JSON 502 body → returns None, no raise; (b) `run()` with `deleteWebhook` raising → polling loop still entered (stub the loop to record entry then cancel); (c) three consecutive `ok:false` polls → recorded sleep intervals grow (monkeypatch `asyncio.sleep` to capture), then a successful poll resets.
- [ ] **Step 2 (GREEN):** Implement. Keep the loop structure recognizable; no new chatty messages.
- [ ] **Step 3:** Full suite + flake8; commit `telegram: survive flaky boots and back off when getUpdates is refused`.

### Task 4: Cycle serialization and honest alert accounting

**Files:**
- Modify: `smc_watcher.py` (`run_cycle`, `_send_alert`, `on_plan`, `on_stored_plan`), `app/services/smc/telegram_bot.py` (callback/command paths that await `run_cycle`/`on_plan`/`on_stored_plan`)
- Modify: `app/services/smc/journal.py` (only if the rollback needs a helper — e.g. `discard(signal_id)`)
- Test: `tests/test_smc/test_autoplan.py` or `tests/test_smc/test_cycle_lock.py` (new)

**Contract:**
1. A single `asyncio.Lock` on the Watcher serializes `run_cycle` bodies: `/check` racing the scheduler waits instead of interleaving (kills the duplicate-alert/duplicate-journal-row race). The Telegram side spawns `run_cycle`/`on_plan`/`on_stored_plan` via `asyncio.create_task` (fire-and-forget with exception logging) so the polling loop keeps serving commands while a long cycle or `plan ALL` runs; the lock keeps the bodies serialized. `/check`'s reply behavior may change from "summary when done" to "started — result follows" only if necessary; prefer keeping current UX by awaiting the task in the `/check` handler path only.
2. `_send_alert` must not leave orphan journal rows: if `notifier.send` returns None, the just-recorded signal row is removed (journal helper `discard(signal_id)` — delete from memory + DB) and the heartbeat line says the alert failed instead of `SETUP FOUND — details above!`. The existing render-first/record-second ordering and the fingerprint/attach flow on success stay as they are.

- [ ] **Step 1 (RED):** Tests: (a) two concurrent `run_cycle()` invocations on a stub watcher whose `check_pair` yields control (asyncio.sleep(0)) — assert the second waits (no interleaved duplicate alert: exactly one send for one setup); (b) `_send_alert` with a notifier that returns None → journal has no pending row afterwards, and `run_cycle`'s summary line for that pair reflects the failure.
- [ ] **Step 2 (GREEN):** Implement. The lock must wrap the same scope the dedup fingerprints assume.
- [ ] **Step 3:** Full suite + flake8; commit `watcher: serialize cycles under one lock; no orphan journal rows on failed sends`.

### Task 5: Poison-pill timestamps and read-path purity

**Files:**
- Modify: `smc_watcher.py` (`_rule_04_warnings` prune ~807, `_track_journal` call placement ~829, `_cooldown_left`)
- Modify: `app/services/smc/journal.py` (`_parse` ~46/90)
- Test: `tests/test_smc/test_poison_state.py` (new)

**Contract:**
1. One malformed or naive persisted timestamp must never kill a cycle: the `news_warned` prune skips (and drops) unparseable/naive values; `journal._parse` treats a naive timestamp as UTC and an unparseable one as "very old" (so the row resolves as expired) — log once per offender, never raise out of `run_cycle` or `update_pair`.
2. `_cooldown_left` becomes read-only: it no longer mutates state or calls `state.save()`; expired-cooldown cleanup moves to a single place inside `run_cycle`.

- [ ] **Step 1 (RED):** Tests: (a) `state.news_warned = {"k": "not-a-date", "k2": "2026-08-11T00:00:00"}` (naive) → `_rule_04_warnings` completes, garbage entries dropped; (b) a journal row with `created_at="garbage"` and one with a naive timestamp → `update_pair` completes, rows resolve without raising; (c) `_cooldown_left` on an expired entry returns falsy without calling `state.save` (spy).
- [ ] **Step 2 (GREEN):** Implement; keep the defensive-parse style `_warn_data_source_failure` already uses.
- [ ] **Step 3:** Full suite + flake8; commit `state: poison timestamps degrade instead of killing cycles; read paths stop writing`.

### Task 6: Event-loop relief, funding text, journal write costs

**Files:**
- Modify: `smc_watcher.py` (`_send_chart`, `_deliver_plan` — chart render calls), `app/services/smc/engine.py` (funding advisory ~374-384), `app/services/smc/journal.py` (`save` / per-row writes)
- Test: `tests/test_smc/test_engine.py` (funding), `tests/test_smc/test_journal_writes.py` (new)

**Contract:**
1. Matplotlib renders run off the event loop: `png = await asyncio.to_thread(render_setup_chart, result)` (same for `render_plan_chart`). Failure isolation contracts unchanged (chart must never block or break an alert).
2. Funding advisory (Rule 9.3, advisory only — wording fix, not a gate change): the danger tier fires for LONG with rate > 0.001 AND for SHORT with rate < -0.001 (shorts paying — squeeze risk), each with direction-correct text; the mild tier's text states the actual configured band using the FUNDING_WARN constant, not a hardcoded claim that can be false. No verdict changes.
3. Journal mutations write only the touched row (`record`/`mark_taken`/`attach_message`/`update_pair` → one upsert), not the whole list. `save()` (all rows) remains only for the legacy-import path.

- [ ] **Step 1 (RED):** Tests: (a) funding SHORT at −0.0015 → warning present, text mentions shorts/negative side, no false "0.05–0.1%" band claim; LONG at +0.0015 → danger text as today; ±0.0007 → mild text quoting the real threshold; (b) journal `mark_taken` triggers exactly one DB upsert (spy on the db layer), not len(signals).
- [ ] **Step 2 (GREEN):** Implement.
- [ ] **Step 3:** Full suite + flake8; commit `watcher: charts off the event loop; honest funding bands; per-row journal writes`.

---

## Final

Full suite + flake8 + `--once` smoke; whole-branch review; push `fix/review-hardening`; PR with base `feat/auto-plan` explaining the stack (auto-retargets to master when #50 merges).
