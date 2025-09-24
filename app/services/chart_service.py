"""Advanced chart generation service with caching, fallbacks, and comprehensive functionality."""

import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import yfinance as yf
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import mplfinance as mpf
import numpy as np
from io import BytesIO
import pytz
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import structlog

from app.core.config import settings
from app.core.exceptions import ChartGenerationError, DataFetchError
from app.models.chart import ChartRequest, ChartResponse, ChartData, OHLCData

logger = structlog.get_logger(__name__)


class ChartService:
    """Advanced service for generating forex charts around news events."""

    def __init__(self, cache_dir: str = None, allow_mock_data: Optional[bool] = None, enable_alpha_vantage: Optional[bool] = None, enable_alternative_symbols: Optional[bool] = None):
        self.cache_dir = cache_dir or os.path.join(tempfile.gettempdir(), 'forex_charts')
        self._ensure_cache_dir()
        # Persistent charts directory and retention policy
        self.charts_dir = os.path.join(self.cache_dir, 'charts')
        self._ensure_charts_dir()
        self.chart_retention_days = int(os.getenv('CHART_RETENTION_DAYS', '3'))
        self._last_chart_prune = datetime.min

        # Control whether mock data is allowed as a last resort
        if allow_mock_data is None:
            # Default to real data only unless ALLOW_MOCK_DATA is explicitly set ("1", "true")
            env_val = os.getenv('ALLOW_MOCK_DATA', '0').strip().lower()
            self.allow_mock_data = env_val in ('1', 'true', 'yes')
        else:
            self.allow_mock_data = bool(allow_mock_data)

        # Control use of Alpha Vantage fallback (disabled by default)
        if enable_alpha_vantage is None:
            env_val = os.getenv('ENABLE_ALPHA_VANTAGE', '0').strip().lower()
            self.enable_alpha_vantage = env_val in ('1', 'true', 'yes')
        else:
            self.enable_alpha_vantage = bool(enable_alpha_vantage)

        # Control use of alternative symbols (enabled by default)
        if enable_alternative_symbols is None:
            env_val = os.getenv('ENABLE_ALTERNATIVE_SYMBOLS', '1').strip().lower()
            self.enable_alternative_symbols = env_val in ('1', 'true', 'yes')
        else:
            self.enable_alternative_symbols = bool(enable_alternative_symbols)

        # Alpha Vantage API key (if enabled)
        self.alpha_vantage_api_key = os.getenv('ALPHA_VANTAGE_API_KEY')

        # Rate limiting for yfinance requests
        self.yf_min_request_interval_sec = getattr(settings.chart, 'yf_min_request_interval_sec', 1)
        self._last_yf_request_time = 0

        # Display timezone
        self.display_timezone = pytz.timezone(getattr(settings.chart, 'display_timezone', 'Europe/Prague'))

        # Alternative symbol mappings for better data availability
        self.alternative_symbols = {
            'EURUSD': ['EURUSD=X', 'EURUSD', 'EUR/USD'],
            'GBPUSD': ['GBPUSD=X', 'GBPUSD', 'GBP/USD'],
            'USDJPY': ['USDJPY=X', 'USDJPY', 'USD/JPY'],
            'AUDUSD': ['AUDUSD=X', 'AUDUSD', 'AUD/USD'],
            'USDCAD': ['USDCAD=X', 'USDCAD', 'USD/CAD'],
            'USDCHF': ['USDCHF=X', 'USDCHF', 'USD/CHF'],
            'NZDUSD': ['NZDUSD=X', 'NZDUSD', 'NZD/USD'],
            'XAUUSD': ['XAUUSD=X', 'XAUUSD', 'XAU/USD', 'GOLD'],
            'BTCUSD': ['BTC-USD', 'BTCUSD', 'BTC/USD'],
            'ETHUSD': ['ETH-USD', 'ETHUSD', 'ETH/USD'],
        }

        logger.info("ChartService initialized",
                   cache_dir=self.cache_dir,
                   allow_mock_data=self.allow_mock_data,
                   enable_alpha_vantage=self.enable_alpha_vantage,
                   enable_alternative_symbols=self.enable_alternative_symbols)

    def _ensure_cache_dir(self):
        """Ensure cache directory exists."""
        os.makedirs(self.cache_dir, exist_ok=True)

    def _ensure_charts_dir(self):
        """Ensure charts directory exists."""
        os.makedirs(self.charts_dir, exist_ok=True)

    def _rate_limit_yf_request(self):
        """Rate limit yfinance requests to avoid being blocked."""
        current_time = time.time()
        time_since_last = current_time - self._last_yf_request_time
        if time_since_last < self.yf_min_request_interval_sec:
            sleep_time = self.yf_min_request_interval_sec - time_since_last
            logger.debug("Rate limiting yfinance request", sleep_time=sleep_time)
            time.sleep(sleep_time)
        self._last_yf_request_time = time.time()

    def _get_symbol_from_currencies(self, base_currency: str, quote_currency: str) -> str:
        """Convert currency pair to yfinance symbol format."""
        base = (base_currency or '').upper()
        quote = (quote_currency or '').upper()

        # Crypto pairs on Yahoo use dash notation, e.g., BTC-USD, ETH-EUR
        crypto = {"BTC", "ETH"}
        if base in crypto or quote in crypto:
            # Prefer crypto as the left side (Yahoo style)
            if base in crypto and quote not in crypto:
                return f"{base}-{quote}"
            if quote in crypto and base not in crypto:
                return f"{quote}-{base}"
            # Crypto-to-crypto (fallback dash form)
            return f"{base}-{quote}"

        # Metals/FX pairs use =X suffix, e.g., XAUUSD=X, EURUSD=X
        return f"{base}{quote}=X"

    def _get_alternative_symbols(self, symbol: str) -> List[str]:
        """Get alternative symbols for better data availability."""
        if not self.enable_alternative_symbols:
            return [symbol]

        # Check if we have alternative symbols for this pair
        for key, alternatives in self.alternative_symbols.items():
            if symbol.upper() in [alt.upper() for alt in alternatives]:
                return alternatives

        # Default alternatives for common patterns
        if symbol.endswith('=X'):
            base_symbol = symbol[:-2]
            return [symbol, base_symbol, base_symbol.replace('USD', '/USD')]

        return [symbol]

    async def generate_chart(self, request: ChartRequest) -> ChartResponse:
        """Generate chart for the given request."""
        try:
            logger.info("Generating chart", currency=request.currency, event_name=request.event_name)

            # Fetch price data
            chart_data = await self._fetch_price_data(request)

            if not chart_data or not chart_data.data:
                return ChartResponse(
                    success=False,
                    error_message="No price data available for the specified time range"
                )

            # Generate chart image
            chart_image = await self._generate_chart_image(chart_data, request)

            return ChartResponse(
                success=True,
                chart_data=chart_data,
                chart_image=chart_image,
                metadata={
                    "currency": request.currency,
                    "event_name": request.event_name,
                    "time_range": f"{request.start_time} to {request.end_time}",
                    "generated_at": datetime.utcnow().isoformat()
                }
            )

        except Exception as e:
            logger.error("Failed to generate chart", error=str(e), exc_info=True)
            raise ChartGenerationError(f"Failed to generate chart: {e}")

    async def _fetch_price_data(self, request: ChartRequest) -> Optional[ChartData]:
        """Fetch price data for the given request."""
        try:
            # Convert currency pair to symbol
            symbol = self._get_symbol_from_currencies(request.currency[:3], request.currency[3:])

            # Try multiple symbols for better data availability
            symbols_to_try = self._get_alternative_symbols(symbol)

            for symbol_to_try in symbols_to_try:
                try:
                    logger.info("Trying symbol", symbol=symbol_to_try)

                    # Rate limit requests
                    self._rate_limit_yf_request()

                    # Fetch data from yfinance
                    ticker = yf.Ticker(symbol_to_try)
                    data = ticker.history(
                        start=request.start_time,
                        end=request.end_time,
                        interval='1m' if request.chart_type == 'intraday' else '1d'
                    )

                    if not data.empty:
                        logger.info("Successfully fetched data", symbol=symbol_to_try, rows=len(data))

                        # Convert to our format
                        ohlc_data = []
                        for timestamp, row in data.iterrows():
                            ohlc_data.append(OHLCData(
                                timestamp=timestamp,
                                open=float(row['Open']),
                                high=float(row['High']),
                                low=float(row['Low']),
                                close=float(row['Close']),
                                volume=int(row['Volume']) if 'Volume' in row else 0
                            ))

                        return ChartData(
                            symbol=symbol_to_try,
                            currency=request.currency,
                            data=ohlc_data,
                            timezone=str(self.display_timezone)
                        )

                except Exception as e:
                    logger.warning("Failed to fetch data for symbol", symbol=symbol_to_try, error=str(e))
                    continue

            # If all symbols failed, try Alpha Vantage as fallback
            if self.enable_alpha_vantage and self.alpha_vantage_api_key:
                logger.info("Trying Alpha Vantage fallback")
                return await self._fetch_alpha_vantage_data(request)

            # If still no data and mock data is allowed, generate mock data
            if self.allow_mock_data:
                logger.warning("Generating mock data as last resort")
                return await self._generate_mock_data(request)

            logger.error("No data available from any source")
            return None

        except Exception as e:
            logger.error("Failed to fetch price data", error=str(e), exc_info=True)
            raise DataFetchError(f"Failed to fetch price data: {e}")

    async def _fetch_alpha_vantage_data(self, request: ChartRequest) -> Optional[ChartData]:
        """Fetch data from Alpha Vantage as fallback."""
        try:
            symbol = self._get_symbol_from_currencies(request.currency[:3], request.currency[3:])

            # Alpha Vantage uses different symbol format
            av_symbol = symbol.replace('=X', '').replace('-', '')

            url = f"https://www.alphavantage.co/query"
            params = {
                'function': 'FX_INTRADAY',
                'from_symbol': av_symbol[:3],
                'to_symbol': av_symbol[3:],
                'interval': '1min',
                'apikey': self.alpha_vantage_api_key
            }

            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()

            if 'Error Message' in data:
                logger.error("Alpha Vantage error", error=data['Error Message'])
                return None

            if 'Note' in data:
                logger.warning("Alpha Vantage rate limit", note=data['Note'])
                return None

            # Parse Alpha Vantage data
            time_series = data.get('Time Series (1min)', {})
            if not time_series:
                logger.warning("No time series data from Alpha Vantage")
                return None

            ohlc_data = []
            for timestamp_str, values in time_series.items():
                timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')

                # Filter by time range
                if request.start_time <= timestamp <= request.end_time:
                    ohlc_data.append(OHLCData(
                        timestamp=timestamp,
                        open=float(values['1. open']),
                        high=float(values['2. high']),
                        low=float(values['3. low']),
                        close=float(values['4. close']),
                        volume=0  # Alpha Vantage doesn't provide volume for FX
                    ))

            if ohlc_data:
                logger.info("Successfully fetched Alpha Vantage data", rows=len(ohlc_data))
                return ChartData(
                    symbol=symbol,
                    currency=request.currency,
                    data=ohlc_data,
                    timezone=str(self.display_timezone)
                )

            return None

        except Exception as e:
            logger.error("Failed to fetch Alpha Vantage data", error=str(e))
            return None

    async def _generate_mock_data(self, request: ChartRequest) -> ChartData:
        """Generate mock data for testing purposes."""
        try:
            # Generate mock OHLC data
            start_time = request.start_time
            end_time = request.end_time

            # Create time series
            if request.chart_type == 'intraday':
                # 1-minute intervals
                time_range = pd.date_range(start_time, end_time, freq='1min')
            else:
                # Daily intervals
                time_range = pd.date_range(start_time, end_time, freq='1D')

            # Generate mock price data
            base_price = 1.2000  # Base price for mock data
            price_changes = np.random.normal(0, 0.001, len(time_range))  # Small random changes

            ohlc_data = []
            current_price = base_price

            for i, timestamp in enumerate(time_range):
                # Generate OHLC from current price
                change = price_changes[i]
                open_price = current_price
                close_price = open_price + change
                high_price = max(open_price, close_price) + abs(change) * 0.5
                low_price = min(open_price, close_price) - abs(change) * 0.5

                ohlc_data.append(OHLCData(
                    timestamp=timestamp,
                    open=round(open_price, 4),
                    high=round(high_price, 4),
                    low=round(low_price, 4),
                    close=round(close_price, 4),
                    volume=1000 + np.random.randint(0, 5000)
                ))

                current_price = close_price

            logger.info("Generated mock data", rows=len(ohlc_data))

            return ChartData(
                symbol=f"{request.currency}=X",
                currency=request.currency,
                data=ohlc_data,
                timezone=str(self.display_timezone)
            )

        except Exception as e:
            logger.error("Failed to generate mock data", error=str(e))
            raise ChartGenerationError(f"Failed to generate mock data: {e}")

    async def _generate_chart_image(self, chart_data: ChartData, request: ChartRequest) -> bytes:
        """Generate chart image from data."""
        try:
            # Convert data to DataFrame
            df = pd.DataFrame([{
                'Open': item.open,
                'High': item.high,
                'Low': item.low,
                'Close': item.close,
                'Volume': item.volume
            } for item in chart_data.data])

            df.index = pd.to_datetime([item.timestamp for item in chart_data.data])

            # Create chart using mplfinance
            fig, axes = mpf.plot(
                df,
                type='candle',
                style='charles',
                title=f"{request.currency} - {request.event_name}",
                ylabel='Price',
                volume=True,
                returnfig=True,
                figsize=(12, 8)
            )

            # Convert to bytes
            buffer = BytesIO()
            fig.savefig(buffer, format='png', dpi=150, bbox_inches='tight')
            buffer.seek(0)
            chart_bytes = buffer.getvalue()
            buffer.close()

            # Clean up
            plt.close(fig)

            logger.info("Chart image generated", size_bytes=len(chart_bytes))
            return chart_bytes

        except Exception as e:
            logger.error("Failed to generate chart image", error=str(e), exc_info=True)
            raise ChartGenerationError(f"Failed to generate chart image: {e}")

    def _prune_old_charts(self):
        """Remove old chart files to manage disk space."""
        try:
            current_time = datetime.now()
            if current_time - self._last_chart_prune < timedelta(hours=1):
                return  # Only prune once per hour

            cutoff_time = current_time - timedelta(days=self.chart_retention_days)
            removed_count = 0

            for filename in os.listdir(self.charts_dir):
                file_path = os.path.join(self.charts_dir, filename)
                if os.path.isfile(file_path):
                    file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if file_mtime < cutoff_time:
                        os.remove(file_path)
                        removed_count += 1

            if removed_count > 0:
                logger.info("Pruned old charts", removed_count=removed_count)

            self._last_chart_prune = current_time

        except Exception as e:
            logger.error("Failed to prune old charts", error=str(e))

    async def create_event_chart(self, currency: str, event_time: datetime, event_name: str, window_hours: int = 2) -> Optional[bytes]:
        """Create a chart around a specific event time."""
        try:
            # Calculate time window
            start_time = event_time - timedelta(hours=window_hours)
            end_time = event_time + timedelta(hours=window_hours)

            # Create chart request
            request = ChartRequest(
                currency=currency,
                event_name=event_name,
                start_time=start_time,
                end_time=end_time,
                chart_type='intraday'
            )

            # Generate chart
            response = await self.generate_chart(request)

            if response.success:
                return response.chart_image
            else:
                logger.error("Failed to create event chart", error=response.error_message)
                return None

        except Exception as e:
            logger.error("Failed to create event chart", error=str(e), exc_info=True)
            return None

    async def get_chart_for_currency(self, currency: str, days: int = 7) -> Optional[bytes]:
        """Get a chart for a currency over the specified number of days."""
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(days=days)

            request = ChartRequest(
                currency=currency,
                event_name=f"{currency} Chart",
                start_time=start_time,
                end_time=end_time,
                chart_type='daily'
            )

            response = await self.generate_chart(request)

            if response.success:
                return response.chart_image
            else:
                logger.error("Failed to get currency chart", error=response.error_message)
                return None

        except Exception as e:
            logger.error("Failed to get currency chart", error=str(e), exc_info=True)
            return None

    def health_check(self) -> Dict[str, Any]:
        """Check the health of the chart service."""
        try:
            # Check cache directory
            cache_accessible = os.access(self.cache_dir, os.W_OK)

            # Check charts directory
            charts_accessible = os.access(self.charts_dir, os.W_OK)

            # Check Alpha Vantage API key if enabled
            av_key_configured = bool(self.alpha_vantage_api_key) if self.enable_alpha_vantage else True

            return {
                "status": "healthy" if all([cache_accessible, charts_accessible, av_key_configured]) else "unhealthy",
                "cache_dir": self.cache_dir,
                "cache_accessible": cache_accessible,
                "charts_dir": self.charts_dir,
                "charts_accessible": charts_accessible,
                "alpha_vantage_enabled": self.enable_alpha_vantage,
                "alpha_vantage_key_configured": av_key_configured,
                "alternative_symbols_enabled": self.enable_alternative_symbols,
                "mock_data_allowed": self.allow_mock_data,
                "chart_retention_days": self.chart_retention_days
            }

        except Exception as e:
            logger.error("Chart service health check failed", error=str(e))
            return {
                "status": "unhealthy",
                "error": str(e)
            }


# Global chart service instance
chart_service = ChartService()
