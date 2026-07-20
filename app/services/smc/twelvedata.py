"""Twelve Data REST fetcher — free-tier alternative for forex/crypto candles.

Runs anywhere (plain REST + API key, no desktop terminal), so it works on
Railway unlike MetaTrader. The free tier allows 800 API credits/day, so the
higher timeframes — which change slowly — are cached and only M5 is refetched
every cycle. With 2-3 forex pairs this keeps the daily budget well under 800
(≈200 credits/day per pair). Native 4h/1h/5min intervals — no resampling.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import structlog

from app.core.exceptions import DataFetchError
from app.services.smc.models import Candle

logger = structlog.get_logger(__name__)

BASE_URL = "https://api.twelvedata.com/time_series"

# our interval -> Twelve Data interval string
_INTERVAL = {"4h": "4h", "1h": "1h", "5m": "5min"}
_CANDLE_MINUTES = {"4h": 240, "1h": 60, "5m": 5}

# how long a fetched series stays fresh; higher TFs change slowly so caching
# them keeps the daily request budget comfortably under the free-tier limit
_TF_CACHE_TTL = {
    "4h": timedelta(hours=1),
    "1h": timedelta(minutes=15),
    "5m": timedelta(seconds=60),
}


class _TimeframeCache:
    """Process-wide cache keyed by (symbol, interval) — fetchers are rebuilt
    every cycle, so the cache must outlive them."""

    def __init__(self) -> None:
        self._store: Dict[Tuple[str, str], Tuple[datetime, List[Candle]]] = {}

    def get(self, key: Tuple[str, str], ttl: timedelta) -> Optional[List[Candle]]:
        hit = self._store.get(key)
        if hit and datetime.now(tz=timezone.utc) - hit[0] < ttl:
            return hit[1]
        return None

    def put(self, key: Tuple[str, str], candles: List[Candle]) -> None:
        self._store[key] = (datetime.now(tz=timezone.utc), candles)

    def clear(self) -> None:
        self._store.clear()


_CACHE = _TimeframeCache()


def td_symbol(pair_key: str) -> str:
    """ETHUSD -> ETH/USD, USDJPY -> USD/JPY."""
    return f"{pair_key[:3]}/{pair_key[3:]}"


def parse_time_series(payload: dict, interval: str) -> List[Candle]:
    """Parse a Twelve Data time_series response into closed UTC candles."""
    if payload.get("status") == "error" or "code" in payload:
        raise DataFetchError(
            f"Twelve Data error: {payload.get('message', 'unknown')}"
        )
    values = payload.get("values")
    if not values:
        raise DataFetchError("Twelve Data returned no values")

    candles: List[Candle] = []
    for row in values:
        try:
            ts = datetime.fromisoformat(row["datetime"])
        except (KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        candles.append(
            Candle(
                timestamp=ts.astimezone(timezone.utc),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0),
            )
        )
    candles.sort(key=lambda c: c.timestamp)  # Twelve Data returns newest-first

    # Drop the still-forming candle
    minutes = _CANDLE_MINUTES.get(interval, 5)
    now = datetime.now(tz=timezone.utc)
    if candles and candles[-1].timestamp + timedelta(minutes=minutes) > now:
        candles = candles[:-1]
    return candles


class TwelveDataFetcher:
    """Fetches candles from Twelve Data with higher-timeframe caching."""

    def __init__(self, pair_key: str, api_key: str, timeout: float = 15.0):
        self.symbol = td_symbol(pair_key)
        self.api_key = api_key
        self.timeout = timeout

    async def fetch_candles(self, interval: str, limit: int = 400) -> List[Candle]:
        td_interval = _INTERVAL.get(interval)
        if not td_interval:
            raise DataFetchError(f"Unsupported Twelve Data interval: {interval}")

        cache_key = (self.symbol, interval)
        cached = _CACHE.get(cache_key, _TF_CACHE_TTL[interval])
        if cached is not None:
            return cached

        params = {
            "symbol": self.symbol,
            "interval": td_interval,
            "outputsize": limit,
            "timezone": "UTC",
            "apikey": self.api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(BASE_URL, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise DataFetchError(f"Twelve Data request failed for {self.symbol}: {e}")

        candles = parse_time_series(payload, interval)
        if len(candles) < 2:
            raise DataFetchError(
                f"Twelve Data returned too few candles for {self.symbol}"
            )
        _CACHE.put(cache_key, candles)
        return candles

    async def fetch_all_timeframes(self) -> Dict[str, List[Candle]]:
        return {
            "h4": await self.fetch_candles("4h", limit=300),
            "h1": await self.fetch_candles("1h", limit=400),
            "m5": await self.fetch_candles("5m", limit=400),
        }

    async def fetch_funding_rate(self) -> Optional[float]:
        """Forex has no funding rate."""
        return None
