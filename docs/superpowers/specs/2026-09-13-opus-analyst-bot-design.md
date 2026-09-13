# Opus analyst bot — the alternative to the rule engine

**Date:** 2026-09-13 · **Status:** approved by owner (chat, 2026-09-13)
**Owner's words:** «предлагаю попробовать сделать альтернативу этому боту …
по нажатию кнопки план подключался Opus, он делал анализ и говорил, куда
ставить лимитку или даже входить по рынку. Моя цель — 1-2 качественные
сделки в день на пару. Пар у меня три.»

## 1. Decisions (the owner's answers, 2026-09-13)

| Question | Answer |
|---|---|
| Form | A **separate bot**: second Telegram token, second Railway service from this repo (`python opus_bot.py`). The watcher is untouched; the two run side by side and can be compared. |
| Rules | **Full SMC discretion**: Opus reads H4/H1/M5 itself — narrative, zones, liquidity, premium/discount, M5 displacement. No engine rule gates it. |
| Inputs | Candles as text (H4 80 / H1 150 / M5 200 rows), the H1 and M5 charts as PNG, today's red news, and the rule engine's reading as a labelled hint it may disagree with. |
| Trigger | A `/plan` press **plus follow-up until the entry**: a cheap price check every 5 minutes in session; Opus is asked again only on events. |
| Hard limits (code-enforced) | Session 08:00–18:30 Prague; red-news blackout 60/15; **RR ≥ 1:2 to TP1**. Nothing else — no daily order cap (the 1-2 trades goal lives in the prompt). |
| After the entry | **Only up to the entry.** A fill is tracked silently to TP1/SL for the journal; the position is the owner's. |
| Event budget | **Events only** (no hourly re-reads): zone reached, plan invalidated, new session block. ~3-6 calls a day per pair. |
| Pairs | ETHUSD, USDJPY, USDCAD — same sources, same keys. |

## 2. What the bot does

**`/plan <pair|ALL>`** → `OpusWatcher._decide`: fetch H4/H1/M5 fresh →
`brief.build_brief` (clock, session and minutes left, blackout in force,
today's red news with their windows, orders already issued today, the
minimum RR, the rule engine's `describe_for_ai` reading and its unswept
liquidity as hints, the candle rows) → two context charts
(`chart.render_context_chart`) → `OpusAnalyst.decide` → `PlanStore.put_plan`
→ the card (`messages.format_plan_card`) → the M5 plan chart.

**The answer** (`decision.DECISION_SCHEMA`, structured output): `action`
limit / market / wait / no_trade; `direction`; `bias`; `entry`,
`stop_loss`, `tp1`, `tp2`; `invalidation`; `watch_low` / `watch_high`;
`valid_for` session / day; `confidence` 1-5; `read` (≤ 5 sentences);
`reasons`; `risks`. Language follows the bot (`SMC_LANG`), enums stay
English.

**The hard limits** (`decision.violations`): an order needs direction,
entry, stop and tp1; long geometry `sl < entry < tp1 (< tp2)`, short the
mirror; a buy limit below the market, a sell limit above; no market entry
off session (a limit off session is a plan for the next block); no entry
of any kind inside a blackout; RR to tp1 ≥ `OPUS_MIN_RR`; the invalidation
on the right side (see §3). A market entry's `entry` is overwritten with
the live price and its invalidation dropped (a position has a stop). The
first failing answer goes back to the model verbatim with the violations
(`OpusAnalyst.decide`, one retry); a still-failing order is downgraded to
WAIT (`decision.downgrade`) with the model's numbers kept and printed under
`⚠️ Opus wanted …`.

**The monitor** (`OpusWatcher.monitor_tick`, every 5 min in session at
slot+`OPUS_TICK_OFFSET_S`, every 30 min off session only while a trade is
open): one M5 fetch per pair with something to watch, then
`plan.advance_plan` — a pending limit **fills** on a touch (→ `Trade`,
silent), is **invalidated** on a body close beyond the cancel level, a
watch plan's **zone is reached** on an overlap, a live plan **expires** at
`valid_until` (one "pull the order" line). Each event fires once per plan.
`zone_reached` / `invalidated` / `session_opened` re-ask Opus with the
previous plan and the event in the brief (`trigger` on the new plan, the
card says `🔁 Re-read after: …`) — under `OPUS_MAX_EVENT_CALLS_PER_DAY`
per pair (a press never counts) and `OPUS_EVENT_COOLDOWN_MIN` after any
call; past either, the event is announced in one line with "press /plan".
`session_opened` fires once for a plan made in the previous block that is
still live at the first tick of the new one.

**Trades** (`plan.advance_trade`): TP1 or SL on candle touch, both in one
candle = SL, timeout after 5 days. `/journal` counts decisions by action
and trades by outcome with the summed R; `/status` shows each pair's plan
and the open trades.

## 3. The invalidation has two meanings

For a **limit** the cancel level sits on the **target side of the
market**: a body close there means the move left without filling the
order — pull it. (A level on the stop side beyond the entry could never
be reached without filling the order first; that side is the stop's.)
For **wait / no trade** it sits on the **stop side of the watch zone**: a
close there breaks the zone. `plan.cancel_side` is the one function that
knows which; the card's `❌ Cancel if M5 closes above/below …`, the
monitor and the limits all read it.

## 4. What is deliberately NOT here

- No engine rule, no ⭐ tier, no dedup fingerprint, no discipline bans —
  the model decides, the owner decides.
- No position management, no BE/partial advice, no messages after the
  entry (owner's pick).
- No daily order cap in code (the owner did not pick one); the brief
  states the orders already issued today and the 1-2 goal.
- No get-ready messages beyond the three events; no hourly re-reads.
- Nothing in `app/services/smc/` changed in behaviour: the fetcher factory
  moved to `sources.py` and is re-exported under the old private names.

## 5. Cost and rate limits

One decision ≈ 2 images (~1.2k tokens each) + ~6k tokens of brief + the
cached system prompt, adaptive thinking at `high`: roughly $0.15–0.30.
Three pairs, one press each plus ~3 events: about $2–4 a day.

The two services share one Twelve Data key. The monitor fetches **M5
only** (1 credit per pair per tick, in session only, and only when there
is a live plan or an open trade); `/plan` fetches all three timeframes.
Ticks are offset from the watcher's by `OPUS_TICK_OFFSET_S` (90 s) and
both services should run `TWELVEDATA_MAX_PER_MIN=4` so the per-minute cap
holds across processes.

## 6. Deployment

Second Railway service, same repo/Dockerfile, start command
`python opus_bot.py`, its own volume with `OPUS_DB_FILE=/data/opus.db`,
`OPUS_BOT_TOKEN` (a new bot from @BotFather), `OPUS_CHAT_ID` optional
(defaults to `TELEGRAM_CHAT_ID`). Shared: `ANTHROPIC_API_KEY`,
`TWELVEDATA_API_KEY` / OANDA, `SMC_NEWS_*`, `SMC_LANG`.
