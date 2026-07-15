"""OANDA v20 REST data fetcher for forex pairs (matches BinanceDataFetcher API)."""

from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
import structlog

from app.core.exceptions import DataFetchError
from app.services.smc.models import Candle

logger = structlog.get_logger(__name__)

HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

GRANULARITY = {"4h": "H4", "1h": "H1", "5m": "M5"}


class OandaDataFetcher:
    """Fetches OHLC candles from OANDA v20 (mid prices, complete candles only)."""

    def __init__(
        self,
        symbol: str,  # OANDA instrument, e.g. "USD_JPY"
        api_token: str,
        environment: str = "practice",
        timeout: float = 15.0,
    ):
        if environment not in HOSTS:
            raise ValueError(f"OANDA environment must be practice|live, got {environment}")
        self.symbol = symbol
        self.base_url = HOSTS[environment]
        self.headers = {"Authorization": f"Bearer {api_token}"}
        self.timeout = timeout

    async def fetch_candles(self, interval: str, limit: int = 300) -> List[Candle]:
        """Fetch closed candles for '4h' / '1h' / '5m'."""
        granularity = GRANULARITY.get(interval, interval)
        url = f"{self.base_url}/v3/instruments/{self.symbol}/candles"
        params = {"granularity": granularity, "count": limit, "price": "M"}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers=self.headers
            ) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:200]
            raise DataFetchError(
                f"OANDA candles request failed ({e.response.status_code}): {detail}"
            )
        except httpx.HTTPError as e:
            raise DataFetchError(f"OANDA candles request failed: {e}")

        candles = [
            Candle(
                timestamp=_parse_time(row["time"]),
                open=float(row["mid"]["o"]),
                high=float(row["mid"]["h"]),
                low=float(row["mid"]["l"]),
                close=float(row["mid"]["c"]),
                volume=float(row.get("volume", 0)),
            )
            for row in payload.get("candles", [])
            if row.get("complete")  # drop the in-progress candle
        ]
        if len(candles) < 2:
            raise DataFetchError(f"OANDA returned no data for {self.symbol}")
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


def _parse_time(value: str) -> datetime:
    """Parse OANDA RFC3339 time like '2026-07-06T14:00:00.000000000Z'."""
    # Trim nanoseconds to microseconds for fromisoformat
    if "." in value:
        head, tail = value.split(".", 1)
        frac = tail.rstrip("Z")[:6]
        value = f"{head}.{frac}+00:00"
    else:
        value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(timezone.utc)
