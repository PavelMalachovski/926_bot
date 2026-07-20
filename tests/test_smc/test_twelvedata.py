"""Tests for the Twelve Data fetcher: parsing, symbol mapping, source pick."""

import pytest

from app.core.exceptions import DataFetchError
from app.services.smc import twelvedata as td
from app.services.smc.twelvedata import parse_time_series, td_symbol


@pytest.fixture(autouse=True)
def _clear_cache():
    td._CACHE.clear()
    yield
    td._CACHE.clear()


def _payload(rows):
    return {"status": "ok", "values": rows}


def _row(dt, o=1.10, h=1.11, low=1.09, c=1.105):
    return {"datetime": dt, "open": o, "high": h, "low": low, "close": c, "volume": "0"}


class TestSymbolMapping:
    def test_forex_and_crypto(self):
        assert td_symbol("USDJPY") == "USD/JPY"
        assert td_symbol("GBPUSD") == "GBP/USD"
        assert td_symbol("ETHUSD") == "ETH/USD"


class TestParsing:
    def test_orders_oldest_first(self):
        # Twelve Data returns newest-first; parser must sort ascending
        rows = [_row("2000-01-01 12:00:00"), _row("2000-01-01 11:00:00")]
        candles = parse_time_series(_payload(rows), "1h")
        assert candles[0].timestamp < candles[1].timestamp

    def test_error_payload_raises(self):
        with pytest.raises(DataFetchError):
            parse_time_series(
                {"status": "error", "code": 429, "message": "run out of credits"},
                "5m",
            )

    def test_empty_values_raises(self):
        with pytest.raises(DataFetchError):
            parse_time_series({"status": "ok", "values": []}, "5m")

    def test_drops_in_progress_candle(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(tz=timezone.utc)
        closed = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:00:00")
        forming = now.strftime("%Y-%m-%d %H:00:00")
        candles = parse_time_series(_payload([_row(forming), _row(closed)]), "1h")
        # the just-started hour candle is dropped, the older one stays
        assert len(candles) == 1


class TestSourceSelection:
    def test_auto_prefers_twelvedata_when_key_set(self, monkeypatch):
        from app.core.config import settings
        from app.services.smc.instruments import get_instrument
        from smc_watcher import _build_fetcher
        from app.services.smc.twelvedata import TwelveDataFetcher

        monkeypatch.setattr(settings.smc, "forex_source", "auto")
        monkeypatch.setattr(settings.twelvedata, "api_key", "KEY")
        fetcher = _build_fetcher(get_instrument("USDJPY"))
        assert isinstance(fetcher, TwelveDataFetcher)
        assert fetcher.symbol == "USD/JPY"

    def test_auto_falls_back_to_yahoo_without_keys(self, monkeypatch):
        from app.core.config import settings
        from app.services.smc.instruments import get_instrument
        from smc_watcher import _build_fetcher
        from app.services.smc.yahoo import YahooDataFetcher

        monkeypatch.setattr(settings.smc, "forex_source", "auto")
        monkeypatch.setattr(settings.twelvedata, "api_key", None)
        monkeypatch.setattr(settings.oanda, "api_token", None)
        assert isinstance(
            _build_fetcher(get_instrument("GBPUSD")), YahooDataFetcher
        )

    def test_explicit_override_forces_yahoo(self, monkeypatch):
        from app.core.config import settings
        from app.services.smc.instruments import get_instrument
        from smc_watcher import _build_fetcher
        from app.services.smc.yahoo import YahooDataFetcher

        monkeypatch.setattr(settings.smc, "forex_source", "yahoo")
        monkeypatch.setattr(settings.twelvedata, "api_key", "KEY")
        assert isinstance(
            _build_fetcher(get_instrument("USDJPY")), YahooDataFetcher
        )

    def test_crypto_always_binance(self, monkeypatch):
        from app.core.config import settings
        from app.services.smc.instruments import get_instrument
        from smc_watcher import _build_fetcher
        from app.services.smc.data import BinanceDataFetcher

        monkeypatch.setattr(settings.twelvedata, "api_key", "KEY")
        assert isinstance(
            _build_fetcher(get_instrument("ETHUSD")), BinanceDataFetcher
        )


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_throttles_beyond_limit(self):
        from app.services.smc.twelvedata import _RateLimiter

        clock = {"t": 1000.0}
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            clock["t"] += seconds  # advance so the window frees up

        lim = _RateLimiter(
            limit=8, window=60.0, clock=lambda: clock["t"], sleep=fake_sleep
        )
        # 8 immediate acquisitions, no sleep
        for _ in range(8):
            await lim.acquire()
        assert slept == []
        # the 9th must wait out the window (~60s)
        await lim.acquire()
        assert len(slept) == 1 and 59 < slept[0] < 61

    @pytest.mark.asyncio
    async def test_allows_after_window_passes(self):
        from app.services.smc.twelvedata import _RateLimiter

        clock = {"t": 0.0}
        lim = _RateLimiter(
            limit=2, window=60.0, clock=lambda: clock["t"], sleep=None
        )
        await lim.acquire()
        await lim.acquire()
        clock["t"] = 61.0  # both earlier calls aged out of the window
        await lim.acquire()  # should not need to sleep (sleep=None would crash)


class TestCache:
    @pytest.mark.asyncio
    async def test_second_call_hits_cache(self, monkeypatch):
        from app.services.smc.twelvedata import TwelveDataFetcher

        calls = {"n": 0}

        async def fake_fetch(self, interval, limit=400):
            # bypass HTTP: emulate the cache logic around a counted "network" hit
            from app.services.smc.twelvedata import _CACHE, _TF_CACHE_TTL

            key = (self.symbol, interval)
            cached = _CACHE.get(key, _TF_CACHE_TTL[interval])
            if cached is not None:
                return cached
            calls["n"] += 1
            candles = parse_time_series(
                _payload([_row("2000-01-01 10:00:00"), _row("2000-01-01 11:00:00")]),
                interval,
            )
            _CACHE.put(key, candles)
            return candles

        monkeypatch.setattr(TwelveDataFetcher, "fetch_candles", fake_fetch)
        f = TwelveDataFetcher("USDJPY", "KEY")
        await f.fetch_candles("4h")
        await f.fetch_candles("4h")
        assert calls["n"] == 1  # second call served from cache
