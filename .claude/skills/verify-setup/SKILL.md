---
name: verify-setup
description: Verify a bot alert on the owner's live TradingView chart via the tradingview-mcp bridge (Phase 3, spec 2026-08-30 D19). Use when the owner asks to check/verify a setup or pastes a 🚨 alert — e.g. «проверь сетап USDJPY». Requires TradingView Desktop running with the debug port and the tradingview-mcp MCP server connected.
---

# Verify a setup on the live chart

You are the second pair of eyes on a Triple Sync + Imbalance alert. The bot
is the detector of record; your job is to look at the live chart and give
the owner an independent read BEFORE he acts. You never place trades, never
create TV alerts, and never call the bot wrong on the strength of a
different indicator's definitions — the strategy spec is law (CLAUDE.md).

Speak Russian, address the owner as «Брат». Keep the verdict short.

## Inputs

The pair, plus the alert's levels (zone, entry, SL, TP, direction, tier).
If the owner named only the pair, ask him to paste the 🚨 alert text — do
not guess levels.

Symbol map (bot key → TradingView symbol):

| Bot | TradingView |
|-----|-------------|
| ETHUSD | BINANCE:ETHUSDT |
| USDJPY | OANDA:USDJPY |
| EURUSD | OANDA:EURUSD |
| GBPUSD | OANDA:GBPUSD |
| USDCAD | OANDA:USDCAD |

## Procedure

1. **Bridge up?** `tv_health_check`. If it fails: tell the owner to launch
   TradingView via the debug script (docs/tradingview-mcp-setup.md) and stop.
2. **Context (H4)**: `chart_set_symbol` → `chart_set_timeframe` "240" →
   `capture_screenshot`. Read the trend with your own eyes: does it agree
   with the alert's direction/source (h4 / h1-fallback / range)?
3. **Zone (H1)**: timeframe "60". If the LuxAlgo **Smart Money Concepts**
   indicator is on the chart, read its drawings — `data_get_pine_boxes` /
   `data_get_pine_labels` with `study_filter: "Smart Money Concepts"` — and
   check whether any marked order block / FVG overlaps the alert's H1 zone.
   No LuxAlgo on the chart is fine: say so and judge structure visually.
4. **Entry picture (M5)**: timeframe "5", `chart_scroll_to_date` if needed.
   Mark the alert's levels with `draw_shape` horizontal lines: entry, SL,
   TP (and TP1/runner if present). Screenshot.
5. **Cross-checks**, each answered explicitly:
   - Price now vs entry: has it run past (stale) or not reached?
   - Anything between entry and TP that argues (an opposing LuxAlgo zone,
     an obvious equal-highs/lows pool, a session level)?
   - Does the SL sit beyond the swept extreme, or inside the raid's wick?
   - News in the next hour the owner should know about (if visible).
6. **Verdict** (Russian, this order):
   - `✅ Подтверждаю` / `⚠️ Есть сомнения` / `⛔ Не согласен` — one line why.
   - The 2–3 observations that matter, tied to what is on the screenshots.
   - Any disagreement between LuxAlgo's marking and the bot's zone is an
     *observation*, not a refutation — their definitions differ by design.
   - Close with: решение за тобой, Брат — бот детектор, я второе мнение.

## Cleanup

Offer to remove the drawn levels (`draw_clear` or per-shape `draw_remove`)
once the owner is done — his chart, his call.
