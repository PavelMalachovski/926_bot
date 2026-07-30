# Pause button, quiet zone ping, wider alert chart — design

Date: 2026-07-30. Approved by the owner in chat.

## Goals

1. A "turn the bot off" control (owner request): mute everything until
   explicitly resumed.
2. Stop the "price reached the zone — get ready" pings by default (owner:
   "это лишнее уже").
3. Double the number of M5 candles on the alert chart so the setup context
   is visible.

## 1. /pause and /resume

- `WatcherState.paused: bool`, persisted in the SQLite kv store (key
  `paused`) — survives Railway restarts. New `WatcherState.set_paused()`.
- `Watcher.run_cycle()` returns early with "⏸ Bot is paused — /resume to
  continue" when paused: no news refresh, no data fetches, no alerts, no
  digest, no Rule 0.4 warnings, no journal tracking. The scheduler keeps
  ticking but each tick is a cheap no-op. `/check` while paused therefore
  answers with the paused message instead of silently waking the bot.
- On resume the next cycle catches up naturally (journal tracking fetches
  enough M5 history).
- Telegram: `/pause` command confirms with an inline "▶️ Resume" button;
  `/resume` command or the button clears the flag. Both commands are added
  to the slash menu and /help. `/status` shows a prominent paused line.
- Bot commands (/status, /stats, /plan, …) keep answering while paused —
  pause silences outbound-initiated messages only.

## 2. Zone ping off by default

- `SMCSettings.zone_ping` default flips `True` → `False`. Code stays; the
  owner can re-enable with `SMC_ZONE_PING=true`. env.example updated.

## 3. Alert chart: 384 candles

- `SMC_CHART_CANDLES` default 192 → 384 (~32h of M5). Canvas widens
  (figsize 13×6 → 20×6) and wicks stay thin so candles remain legible.
- Note: an explicit `SMC_CHART_CANDLES=192` in Railway variables would
  override the new default and must be removed there.

## Testing

- New `tests/test_smc/test_pause.py`: state persistence across reload,
  run_cycle no-op + message while paused, /pause and /resume commands and
  the resume callback, /status paused line.
- Config default test: `SMCSettings().zone_ping is False` with the env var
  unset; `chart.CHART_CANDLES == 384`.
- Existing zone-ping tests already force the flag on and stay green.
