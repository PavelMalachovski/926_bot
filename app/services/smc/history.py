"""Historical candle download + disk cache for the backtester.

Phase 1 of the money-machine plan (spec 2026-08-30, D21): forex history
comes from Twelve Data behind a local disk cache — the 800/day key is
shared with the live bot, so history is paid for once and reused — and
crypto history from Binance (free, keyless). Never imported by the live
watcher; only `smc_backtest.py` reaches this module.

Fidelity notes:
- Twelve Data requests go through `TwelveDataFetcher._request` — the same
  rate limiter, 429 back-off and key-redacting error paths production uses
  — and responses through `parse_time_series`, so a historical candle is
  parsed by exactly the code that parses a live one.
- The cache file is plain JSON, one file per pair+timeframe under
  `data/backtest/` (gitignored). A refresh only fetches the ranges the
  cache does not already cover.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import structlog

from app.core.exceptions import DataFetchError
from app.services.smc.data import SPOT_BASE
from app.services.smc.instruments import get_instrument
from app.services.smc.models import Candle
from app.services.smc.twelvedata import (
    TwelveDataFetcher,
    _INTERVAL as _TD_INTERVAL,
    parse_time_series,
    td_symbol,
)

logger = structlog.get_logger(__name__)

DEFAULT_CACHE_DIR = Path("data/backtest")

# Candle duration per engine timeframe key (the fetchers' own vocabulary).
CANDLE_MINUTES: Dict[str, int] = {"m5": 5, "h1": 60, "h4": 240}

# Fetcher interval strings per timeframe key — Binance takes these verbatim;
# Twelve Data goes through the production `_INTERVAL` map.
_FETCH_INTERVAL: Dict[str, str] = {"m5": "5m", "h1": "1h", "h4": "4h"}

_BINANCE_PAGE = 1000  # klines hard limit per request
_TWELVE_PAGE = 5000  # time_series outputsize hard limit per request


def cache_path(cache_dir: Path, pair_key: str, tf: str) -> Path:
    return Path(cache_dir) / f"{pair_key}_{tf}.json"


def load_cache(path: Path) -> List[Candle]:
    """Read a cache file back into candles. Missing/corrupt file -> empty."""
    try:
        rows = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    candles = []
    for row in rows:
        try:
            candles.append(
                Candle(
                    timestamp=datetime.fromisoformat(row["t"]),
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                    volume=float(row.get("v", 0.0)),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    candles.sort(key=lambda c: c.timestamp)
    return candles


def save_cache(path: Path, candles: List[Candle]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "t": c.timestamp.isoformat(),
            "o": c.open,
            "h": c.high,
            "l": c.low,
            "c": c.close,
            "v": c.volume,
        }
        for c in candles
    ]
    path.write_text(json.dumps(rows))


def merge_candles(*series: List[Candle]) -> List[Candle]:
    """Union by timestamp, sorted. Later series win on a duplicate stamp."""
    by_ts: Dict[datetime, Candle] = {}
    for candles in series:
        for c in candles:
            by_ts[c.timestamp] = c
    return [by_ts[ts] for ts in sorted(by_ts)]


async def _fetch_binance(
    symbol: str, tf: str, start: datetime, end: datetime
) -> List[Candle]:
    """Page forward through /api/v3/klines with startTime."""
    interval = _FETCH_INTERVAL[tf]
    step = timedelta(minutes=CANDLE_MINUTES[tf])
    out: List[Candle] = []
    cursor = start
    async with httpx.AsyncClient(timeout=30.0) as client:
        while cursor < end:
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": _BINANCE_PAGE,
                "startTime": int(cursor.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
            }
            try:
                response = await client.get(f"{SPOT_BASE}/api/v3/klines", params=params)
                response.raise_for_status()
                raw = response.json()
            except httpx.HTTPError as e:
                raise DataFetchError(f"Binance history request failed: {e}")
            if not isinstance(raw, list) or not raw:
                break
            page = [
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
            # Same closed-candles-only rule as the live fetcher.
            now_ms = datetime.now(tz=timezone.utc).timestamp() * 1000
            if raw[-1][6] > now_ms:
                page = page[:-1]
            out.extend(page)
            if len(raw) < _BINANCE_PAGE or not page:
                break
            cursor = page[-1].timestamp + step
    return out


async def _fetch_twelvedata(
    pair_key: str, tf: str, start: datetime, end: datetime, api_key: str
) -> List[Candle]:
    """Page backwards through time_series with end_date.

    Rides `TwelveDataFetcher._request` so the production rate limiter,
    429 back-off and key-redacting errors all apply.
    """
    fetcher = TwelveDataFetcher(pair_key, api_key=api_key, timeout=30.0)
    interval_key = _FETCH_INTERVAL[tf]
    step = timedelta(minutes=CANDLE_MINUTES[tf])
    pages: List[List[Candle]] = []
    cursor = end
    while cursor > start:
        params = {
            "symbol": td_symbol(pair_key),
            "interval": _TD_INTERVAL[interval_key],
            "outputsize": _TWELVE_PAGE,
            "timezone": "UTC",
            "apikey": api_key,
            "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": cursor.strftime("%Y-%m-%d %H:%M:%S"),
        }
        payload = await fetcher._request(params)
        page = parse_time_series(payload, interval_key)
        if not page:
            break
        pages.append(page)
        if page[0].timestamp <= start or len(page) < 2:
            break
        next_cursor = page[0].timestamp - step
        if next_cursor >= cursor:  # no progress — never spin
            break
        cursor = next_cursor
    return merge_candles(*reversed(pages))


async def fetch_history(
    pair_key: str,
    tf: str,
    start: datetime,
    end: datetime,
    api_key: Optional[str] = None,
) -> List[Candle]:
    """Fetch [start, end] closed candles for one pair+timeframe."""
    instrument = get_instrument(pair_key)
    if instrument.source == "crypto":
        candles = await _fetch_binance(instrument.source_symbol, tf, start, end)
    else:
        if not api_key:
            raise DataFetchError(
                f"Forex history for {pair_key} needs a Twelve Data API key "
                "(TWELVEDATA_API_KEY)"
            )
        candles = await _fetch_twelvedata(pair_key, tf, start, end, api_key)
    return [c for c in candles if start <= c.timestamp <= end]


async def load_history(
    pair_key: str,
    tf: str,
    start: datetime,
    end: datetime,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    api_key: Optional[str] = None,
    refresh: bool = False,
) -> List[Candle]:
    """Cache-first history: fetch only the ranges the cache does not cover.

    `refresh=True` ignores the cache contents (the file is still rewritten
    with the merged result, so a forced pull repairs a bad cache in place).
    """
    path = cache_path(cache_dir, pair_key, tf)
    cached = [] if refresh else load_cache(path)
    step = timedelta(minutes=CANDLE_MINUTES[tf])

    fetched: List[List[Candle]] = []
    if not cached:
        fetched.append(await fetch_history(pair_key, tf, start, end, api_key))
    else:
        if start < cached[0].timestamp - step:
            fetched.append(
                await fetch_history(
                    pair_key, tf, start, cached[0].timestamp, api_key
                )
            )
        if end > cached[-1].timestamp + step:
            fetched.append(
                await fetch_history(
                    pair_key, tf, cached[-1].timestamp, end, api_key
                )
            )

    merged = merge_candles(cached, *fetched)
    if fetched and merged != cached:
        save_cache(path, merged)
        logger.info(
            "History cache updated",
            pair=pair_key,
            tf=tf,
            candles=len(merged),
            path=str(path),
        )
    return [c for c in merged if start <= c.timestamp <= end]
