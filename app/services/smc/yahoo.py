"""Yahoo Finance chart API fetcher for forex pairs — free, no API key.

Yahoo serves 5m and 1h candles natively; H4 is resampled from 1h (aligned to
0/4/8/12/16/20 UTC). Data is unofficial but stable; a browser User-Agent is
required or Yahoo throttles the request.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx
import structlog

from app.core.exceptions import DataFetchError
from app.services.smc.models import Candle

logger = structlog.get_logger(__name__)

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# interval requested from Yahoo and how much history to pull
RANGES = {
    "5m": ("5m", "5d"),
    "1h": ("60m", "1mo"),
    "4h": ("60m", "3mo"),  # resampled to H4 locally
}


class YahooDataFetcher:
    """Fetches OHLC candles from Yahoo Finance (e.g. symbol 'USDJPY=X')."""

    def __init__(self, symbol: str, timeout: float = 15.0):
        self.symbol = symbol
        self.timeout = timeout

    async def fetch_candles(self, interval: str, limit: int = 300) -> List[Candle]:
        if interval not in RANGES:
            raise DataFetchError(f"Unsupported Yahoo interval: {interval}")
        yahoo_interval, yahoo_range = RANGES[interval]
        params = {
            "interval": yahoo_interval,
            "range": yahoo_range,
            "includePrePost": "false",
        }
        url = f"{BASE_URL}/{self.symbol}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers=HEADERS
            ) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as e:
            raise DataFetchError(
                f"Yahoo request failed ({e.response.status_code}) for {self.symbol}"
            )
        except (httpx.HTTPError, ValueError) as e:
            raise DataFetchError(f"Yahoo request failed for {self.symbol}: {e}")

        candles = parse_chart_payload(payload)
        if not candles:
            raise DataFetchError(f"Yahoo returned no data for {self.symbol}")

        # Drop the still-forming candle of the native interval
        native = timedelta(minutes=5) if yahoo_interval == "5m" else timedelta(hours=1)
        now = datetime.now(tz=timezone.utc)
        if candles and candles[-1].timestamp + native > now:
            candles = candles[:-1]

        if interval == "4h":
            candles = resample_h4(candles, now=now)
        return candles[-limit:]

    async def fetch_all_timeframes(self) -> Dict[str, List[Candle]]:
        return {
            "h4": await self.fetch_candles("4h", limit=300),
            "h1": await self.fetch_candles("1h", limit=400),
            "m5": await self.fetch_candles("5m", limit=400),
        }

    async def fetch_funding_rate(self) -> Optional[float]:
        """Forex has no funding rate."""
        return None


def parse_chart_payload(payload: Dict) -> List[Candle]:
    """Parse Yahoo v8 chart JSON into candles (null rows are skipped)."""
    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        error = (payload.get("chart") or {}).get("error")
        raise DataFetchError(f"Unexpected Yahoo payload: {error or 'no data'}")

    candles: List[Candle] = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = (
            quote["open"][i],
            quote["high"][i],
            quote["low"][i],
            quote["close"][i],
        )
        if None in (o, h, l, c):
            continue
        candles.append(
            Candle(
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
            )
        )
    return candles


def resample_h4(h1_candles: List[Candle], now: Optional[datetime] = None) -> List[Candle]:
    """Aggregate 1h candles into H4 buckets aligned to 0/4/8/12/16/20 UTC.

    The bucket that has not ended yet is dropped so only closed H4 candles
    remain (a Friday-close bucket with fewer than four hours still counts
    once its window has passed).
    """
    now = now or datetime.now(tz=timezone.utc)
    buckets: Dict[datetime, List[Candle]] = {}
    for candle in h1_candles:
        start = candle.timestamp.replace(
            hour=candle.timestamp.hour // 4 * 4, minute=0, second=0, microsecond=0
        )
        buckets.setdefault(start, []).append(candle)

    result: List[Candle] = []
    for start in sorted(buckets):
        if start + timedelta(hours=4) > now:
            continue  # still forming
        group = buckets[start]
        result.append(
            Candle(
                timestamp=start,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
            )
        )
    return result
