"""The Opus analyst bot (owner request 2026-09-13): a second, separate
Telegram bot in which Claude Opus reads the candles itself and names the
order — limit, market, wait or no trade. Nothing here is imported by the
watcher; the two bots share only the market-data fetchers, the news
calendar, the sessions clock, the i18n catalog and the chart primitives."""
