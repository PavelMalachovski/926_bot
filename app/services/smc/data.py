"""Market data access for the SMC engine (Binance public REST, no API key)."""

from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
import structlog

from app.core.exceptions import DataFetchError
from app.services.smc.models import Candle

logger = structlog.get_logger(__name__)

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"


# D29: daily candles served alongside H4/H1/M5 — ~4 months, enough for the
# daily swing structure and last week's extremes.
D1_LIMIT = 120


class BinanceDataFetcher:
    """Fetches OHLC candles and funding rate from Binance public endpoints."""

    def __init__(self, symbol: str = "ETHUSDT", timeout: float = 15.0):
        self.symbol = symbol
        self.timeout = timeout

    async def fetch_candles(self, interval: str, limit: int = 300) -> List[Candle]:
        """Fetch closed candles for the given interval (e.g. '4h', '1h', '5m').

        Binance returns the still-forming candle last — it is dropped so the
        engine only ever sees closed candles.
        """
        url = f"{SPOT_BASE}/api/v3/klines"
        params = {"symbol": self.symbol, "interval": interval, "limit": limit}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                raw = response.json()
        except httpx.HTTPError as e:
            raise DataFetchError(f"Binance klines request failed: {e}")

        if not isinstance(raw, list) or len(raw) < 2:
            raise DataFetchError(f"Binance returned no data for {self.symbol}")

        candles = [
            Candle(
                timestamp=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in raw
        ]
        # Drop the in-progress candle: its close time is in the future.
        now_ms = datetime.now(tz=timezone.utc).timestamp() * 1000
        if raw[-1][6] > now_ms:
            candles = candles[:-1]
        return candles

    async def fetch_all_timeframes(
        self, force_fresh: bool = False
    ) -> Dict[str, List[Candle]]:
        """Fetch H4 / H1 / M5 candles in one call (Binance is never cached,
        so force_fresh is a no-op — kept for a uniform fetcher interface)."""
        data = {
            "h4": await self.fetch_candles("4h", limit=300),
            "h1": await self.fetch_candles("1h", limit=400),
            "m5": await self.fetch_candles("5m", limit=400),
        }
        data["d1"] = await self._fetch_d1()
        return data

    async def _fetch_d1(self) -> List[Candle]:
        """D29: the daily candles, best-effort — a D1 failure must never
        cost the cycle its H4/H1/M5 (the engine runs without D1)."""
        try:
            return await self.fetch_candles("1d", limit=D1_LIMIT)
        except DataFetchError as e:
            logger.warning("Binance D1 fetch failed — continuing without", error=str(e))
            return []

    async def fetch_funding_rate(self) -> Optional[float]:
        """Fetch the current perpetual funding rate (per 8h). None on failure."""
        url = f"{FUTURES_BASE}/fapi/v1/premiumIndex"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params={"symbol": self.symbol})
                response.raise_for_status()
                data = response.json()
                return float(data["lastFundingRate"])
        except (httpx.HTTPError, KeyError, ValueError) as e:
            logger.warning("Failed to fetch funding rate", error=str(e))
            return None

    async def fetch_last_price(self) -> float:
        """Fetch the latest trade price."""
        url = f"{SPOT_BASE}/api/v3/ticker/price"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params={"symbol": self.symbol})
                response.raise_for_status()
                return float(response.json()["price"])
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise DataFetchError(f"Binance ticker request failed: {e}")
