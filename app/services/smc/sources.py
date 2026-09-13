"""Market-data source resolution shared by the two bots.

The watcher (`smc_watcher.py`) and the Opus analyst bot (`opus_bot.py`)
fetch the same candles from the same providers with the same keys:
crypto from Binance (keyless), forex from Twelve Data or OANDA, selected
by `SMC_FOREX_SOURCE`. Both build their fetchers here so a change to the
resolution rules reaches both processes.

There is no keyless forex fallback (the previous keyless feed was removed —
its data was bad enough to have produced a wrong strategy conclusion during
replay validation). A forex pair with no usable key is a configuration
error, not a silent downgrade: it must fail clearly here so the caller can
warn the owner instead of quietly returning no data.
"""

from app.core.config import settings
from app.core.exceptions import ConfigurationError
from app.services.smc.data import BinanceDataFetcher
from app.services.smc.instruments import Instrument
from app.services.smc.oanda import OandaDataFetcher
from app.services.smc.twelvedata import TwelveDataFetcher


def forex_source() -> str:
    """Resolve the configured forex source, honouring 'auto'."""
    source = settings.smc.forex_source.strip().lower()
    if source == "auto":
        if settings.twelvedata.api_key:
            return "twelvedata"
        if settings.oanda.api_token:
            return "oanda"
        raise ConfigurationError(
            "No forex data source configured: set TWELVEDATA_API_KEY or "
            "OANDA_API_TOKEN (SMC_FOREX_SOURCE=auto has nothing to pick "
            "from — the keyless forex fallback has been removed)."
        )
    if source == "twelvedata":
        if not settings.twelvedata.api_key:
            raise ConfigurationError(
                "SMC_FOREX_SOURCE=twelvedata but TWELVEDATA_API_KEY is not set."
            )
        return "twelvedata"
    if source == "oanda":
        if not settings.oanda.api_token:
            raise ConfigurationError(
                "SMC_FOREX_SOURCE=oanda but OANDA_API_TOKEN is not set."
            )
        return "oanda"
    raise ConfigurationError(
        f"Unknown SMC_FOREX_SOURCE: {source!r} (use 'auto', 'twelvedata' or "
        "'oanda')."
    )


def build_fetcher(instrument: Instrument):
    """The candle fetcher for one instrument (uniform interface:
    `fetch_candles(interval, limit)` / `fetch_all_timeframes()`)."""
    if instrument.source == "crypto":
        # ETHUSD stays on Binance: unlimited, deep history, funding rate.
        return BinanceDataFetcher(instrument.source_symbol)
    source = forex_source()
    if source == "twelvedata":
        return TwelveDataFetcher(instrument.key, settings.twelvedata.api_key)
    return OandaDataFetcher(
        symbol=instrument.source_symbol,
        api_token=settings.oanda.api_token,
        environment=settings.oanda.environment,
    )
