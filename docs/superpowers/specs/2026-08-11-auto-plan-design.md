# Auto-Plan: hidden pre-market plans + plan-centric zone alerts

**Date:** 2026-08-11 · **Status:** approved by owner (interactive session)

## Problem

The Pre-Market Plan (`/plan`, Шаблон B) exists only on demand: the owner must
remember to press the button, and the plan he reads at 07:50 silently goes
stale as H1 structure shifts during the day. The zone-touch ping fires from
the *engine's* live zone, so it carries no plan numbers and stays silent on
flat pairs (no direction → no live zone → no ping), exactly where the plan's
speculative brackets are the only picture available.

## What changes (owner decisions, 2026-08-11)

1. The bot builds the plan for every enabled pair **automatically at 07:55
   and 13:55 Prague** — right before the Frankfurt/London and New York
   blocks (`sessions.WINDOWS`).
2. The plan content is **not sent**. One **silent summary message**
   (Telegram `disable_notification=true`) arrives with a one-line digest per
   pair and inline buttons; pressing a button delivers that pair's full plan
   + H1 chart.
3. The plan is **recomputed every watcher cycle** from the candles the cycle
   already fetched (no extra API calls — `build_plan` is pure). A material
   change silently **edits** the summary.
4. The old engine-based zone ping (`_maybe_zone_ping`) is **removed**. The
   "price reached the zone" alert becomes **plan-centric**: it fires when
   price first enters a zone named by the *current* plan and carries that
   scenario's projected numbers.
5. `build_plan` direction selection is brought to **parity with the
   engine's Rule 1 precedence** (review finding, 2026-08-11): on H4 FLAT
   the plan must consult the H1 trend first (owner decision 2026-08-06),
   then — aggressive profile only — the H4 CHoCH, and only then fall back
   to both-way speculative brackets. Today the plan skips the H1 step and
   checks CHoCH first, so it can project a non-speculative direction the
   live engine will never alert, and label an engine-directional pair
   "speculative". A zone alert that quotes plan numbers makes this a
   correctness requirement, not polish. The H1-derived scenario is
   non-speculative and labels its source like the alert does ("H4 flat —
   direction from H1 …").

Detector mode is untouched: the full 🚨 setup alert still fires on M5
CHoCH + FVG regardless of any plan.

## 1. Schedule

- New settings: `SMC_AUTO_PLAN` (bool, default `true`),
  `SMC_AUTO_PLAN_TIMES` (default `"07:55,13:55"`, Prague `HH:MM` list).
  Documented in `env.example`.
- Gate runs inside the scheduler loop exactly like the 07:45 digest gate
  (`_morning_briefing`): fire once per (slot, Prague day), tracked in state
  so a restart does not re-send. A late start fires only the **most recent**
  missed slot (boot at 09:30 → the 07:55 plan; boot at 15:00 → only the
  13:55 plan, the morning one is stale and skipped).
- Per-pair market gate: crypto pairs are planned every day, forex pairs
  Mon–Fri only (`active_session` semantics). A pair whose market is closed
  is left out of the summary; if no pair qualifies, no summary is sent.
- `/pause` suppresses auto-plan snapshots (quiet mode: a paused bot sends
  nothing).
- Snapshot fetch uses `force_fresh=True` — same freshness contract as the
  manual `/plan`.

## 2. Silent summary message

One message per slot (the 13:55 summary is a new message; the morning one
stays in history). Format, all dynamic strings through `escape_html`:

```
📋 Pre-Market Plan 07:55 — press a pair for details
EURUSD  🔼 LONG   zone 1.0840–1.0860  (~1:2.1)
ETHUSD  ⛔ waiting: no untested H1 zone
USDJPY  🔽 SHORT  zone 157.20–157.45  (~1:1.8)  (speculative)
```

- One line per pair: direction arrow + side + zone bounds + approx RR for
  the best scenario; flat both-way brackets show both lines marked
  `(speculative)`; a blocked pair shows `⛔ waiting:` + the blocker sentence
  (the plan's existing vocabulary).
- Inline keyboard: one button per pair + `🌐 All pairs`. New callback prefix
  `aplan_<PAIR>` / `aplan_ALL` — serves the **stored** current plan
  (text via `format_plan` + chart via `render_plan_chart`) with its "as of"
  timestamp, instantly and with zero API calls. Fallback: no stored plan in
  memory (fresh restart, button pressed before the first cycle) → run the
  normal `/plan` path (`_send_pair_plan`, force-fresh).
- `TelegramNotifier.send()` gains a `disable_notification: bool = False`
  passthrough; the summary (and its edits) is the only caller today.

## 3. Dynamic recompute

- Every `run_cycle` pass over a pair rebuilds `build_plan(...)` from the
  H4/H1/M5 lists already fetched for the engine, and stores it in memory as
  the pair's **current plan** (plus the H1 candles for the chart).
- Off-session cycles (15-min cadence) recompute too, whenever they fetch
  candles — the plan a button serves is never older than the last fetch.
- **Material-change fingerprint** per pair: the sorted set of scenario
  tuples `(direction, zone_bottom, zone_top, speculative)` plus the blocker
  sentence (or None). Price drift and RR drift are *not* material.
- When any pair's fingerprint differs from what the latest summary shows,
  the summary message is edited in place (`editMessageText`, keyboard
  preserved) with an `· upd HH:MM` suffix in the header. Edits are silent by
  nature (no push).
- Persistence (`WatcherState` kv): latest summary `message_id`, its slot
  label + Prague date, and the per-pair fingerprints backing it. Full plans
  and candles stay in memory only — a restart rebuilds them within one
  cycle, and the kv keeps edits working across the restart.

## 4. Plan-zone provenance (spec 2026-08-06 §6) — unchanged meaning

Only the **07:55/13:55 snapshots** call `state.remember_plan_zones`. The
5-minute recomputes do not: an alert compared against a plan rebuilt from
the same candles seconds earlier would always match, and the "from this
morning's plan" line would stop meaning anything. Provenance keeps meaning
"the zone was in the pre-session picture". Manual `/plan` runs still update
the stored zones, as today.

## 5. Plan-centric zone alert (replaces the ping)

- `_maybe_zone_ping` and its engine-zone arming logic are removed. The
  existing `SMC_ZONE_PING` setting remains the on/off gate for the new
  alert (same intent: "tell me when price reaches the zone").
- Trigger: during a session window (Rule 0.1), the last closed M5 candle's
  range first overlaps a **scenario zone of the pair's current plan** (wick
  touch — the engine's own Rule 2 touch semantics). Blocker-named zones do
  not alert: they carry no bracket; if a setup completes there anyway, the
  🚨 alert covers it (detector mode).
- Message (normal notification — this is the "get ready" moment):

  ```
  🔔 EURUSD: price reached the Demand zone 1.0840–1.0860
  📋 Plan: LONG — Buy Limit 1.0860 | SL 1.0835 | TP 1.0920 | ~1:2.1 (speculative)
  Watching M5 for a bullish CHoCH + FVG.
  ```

  `(speculative)` only on flat-bracket scenarios; numbers are the plan's
  projections (preliminary SL, as the plan message itself warns).
- Episode dedup: state stores the pinged zone's bounds per pair
  (replacing the `zone_pinged` bool). Reset when price leaves the zone,
  when the current plan no longer names an overlapping same-direction zone,
  or on a new Prague day. Zone matching by overlap + direction, the
  `zone_was_planned` logic.
- The "already managing a position here" cooldown check is kept.
- Alert failure must not mark the episode as pinged (send first, then mark
  — same ordering rule the fingerprint dedup uses).

## 6. Out of scope (owner may ask later)

- Push notification when the plan materially changes (edits stay silent).
- A pinned "today's plan" card.
- Persisting full `PairPlan` objects to the DB.
- Any change to `/plan`, the engine checklist, discipline rules, or the 🚨
  alert itself.

## Touched files

| File | Change |
|---|---|
| `smc_watcher.py` | auto-plan scheduler gate, per-cycle recompute + store, summary build/edit, plan-zone alert, remove `_maybe_zone_ping` |
| `app/services/smc/plan.py` | Rule 1 direction parity: H1-trend fallback on H4 FLAT, ahead of the aggressive CHoCH check |
| `app/services/smc/planbook.py` (new) | in-memory plan store, fingerprint, summary line formatting — pure & unit-testable |
| `app/services/smc/notifier.py` | `send(disable_notification=)`, summary formatter lives with the other formatters |
| `app/services/smc/telegram_bot.py` | `aplan_*` callbacks |
| `app/services/smc/state.py` | summary message_id/slot/fingerprints kv; `zone_pinged` bool → pinged-zone bounds |
| `app/config.py` + `env.example` | `SMC_AUTO_PLAN`, `SMC_AUTO_PLAN_TIMES` |
| `tests/test_smc/` | see below |

## Testing

Synthetic candles via `tests/test_smc/helpers.py`, network-free:

1. Schedule gate: fires once per slot/day; restart mid-day does not
   re-send; late boot fires the missed slot; weekend skips forex, keeps
   crypto; `/pause` suppresses.
2. Fingerprint: scenario appears/disappears, zone bounds shift, blocker
   changes → material; price/RR drift → not material.
3. Summary formatting: escaped dynamic strings, speculative markers,
   blocker lines, button rows.
4. Plan-zone alert: fires on wick overlap of a scenario zone in-session;
   silent off-session; silent for blocker zones; episode reset on exit /
   plan change / new day; cooldown respected; send-failure does not mark
   the episode.
5. Provenance: snapshot updates `plan_zones`, per-cycle recompute does not.
6. Callback: `aplan_` serves stored plan; falls back to fresh build when
   the store is empty.
7. Plan direction parity: H4 flat + clean H1 uptrend → one non-speculative
   LONG (matching `engine.direction_source == "h1"`); aggressive profile
   with both an H1 trend and an opposing H4 CHoCH → the H1 direction wins,
   same as the engine; no H1 trend + aggressive CHoCH → CHoCH direction;
   nothing → both-way speculative brackets, as today.
