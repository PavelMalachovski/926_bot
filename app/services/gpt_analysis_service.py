"""Advanced GPT analysis service with technical indicators and market analysis."""

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
import numpy as np
import pandas as pd
import pytz
import requests
import structlog

from app.core.config import settings
from app.core.exceptions import AnalysisError, ExternalAPIError
from app.services.chart_service import chart_service
from app.utils.telegram_utils import escape_markdown_v2

logger = structlog.get_logger(__name__)

# Simple in-memory rate limiter for GPT calls
_LAST_GPT_CALLS: Dict[str, float] = {}


def _get_symbol_from_currencies(base_currency: str, quote_currency: str) -> str:
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


def _infer_price_decimals(symbol: str) -> int:
    """Infer number of decimal places for price display."""
    # JPY pairs conventionally use 2 decimals; others use 4
    return 2 if "JPY" in symbol else 4


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Calculate Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index."""
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate MACD (Moving Average Convergence Divergence)."""
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger_bands(close: pd.Series, period: int = 20, std_dev: float = 2) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate Bollinger Bands."""
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper_band = sma + (std * std_dev)
    lower_band = sma - (std * std_dev)
    return upper_band, sma, lower_band


class ChatGPTAnalyzer:
    """Advanced ChatGPT analyzer for forex news and market analysis."""

    def __init__(self):
        self.api_key = os.getenv('CHATGPT_API_KEY')
        self.base_url = "https://api.openai.com/v1/chat/completions"
        self.model = "gpt-3.5-turbo"
        self.max_tokens = 500
        self.temperature = 0.7
        self.rate_limit_delay = 1.0  # seconds between calls

    def _rate_limit_check(self, key: str = "default") -> bool:
        """Check if enough time has passed since last GPT call."""
        current_time = datetime.now().timestamp()
        last_call = _LAST_GPT_CALLS.get(key, 0)

        if current_time - last_call < self.rate_limit_delay:
            return False

        _LAST_GPT_CALLS[key] = current_time
        return True

    async def analyze_news(self, news_item: Dict[str, Any]) -> str:
        """Analyze forex news using ChatGPT."""
        try:
            if not self.api_key:
                logger.warning("ChatGPT API key not configured, returning mock analysis")
                return self._generate_mock_analysis(news_item)

            if not self._rate_limit_check():
                logger.warning("Rate limit exceeded, returning mock analysis")
                return self._generate_mock_analysis(news_item)

            # Prepare the prompt
            prompt = self._create_analysis_prompt(news_item)

            # Make API call
            response = await self._call_chatgpt_api(prompt)

            if response:
                logger.info("ChatGPT analysis completed", event=news_item.get('event', 'N/A'))
                return response
            else:
                logger.warning("ChatGPT API call failed, returning mock analysis")
                return self._generate_mock_analysis(news_item)

        except Exception as e:
            logger.error("Failed to analyze news with ChatGPT", error=str(e))
            return self._generate_mock_analysis(news_item)

    def _create_analysis_prompt(self, news_item: Dict[str, Any]) -> str:
        """Create analysis prompt for ChatGPT."""
        event = news_item.get('event', 'N/A')
        currency = news_item.get('currency', 'N/A')
        actual = news_item.get('actual', 'N/A')
        forecast = news_item.get('forecast', 'N/A')
        previous = news_item.get('previous', 'N/A')
        impact = news_item.get('impact', 'N/A')

        prompt = f"""
Analyze this forex news event and provide a brief market analysis:

Event: {event}
Currency: {currency}
Actual: {actual}
Forecast: {forecast}
Previous: {previous}
Impact: {impact}

Please provide:
1. Brief interpretation of the data
2. Potential market direction
3. Key factors to watch

Keep the analysis concise and professional (max 200 words).
"""

        return prompt.strip()

    async def _call_chatgpt_api(self, prompt: str) -> Optional[str]:
        """Make API call to ChatGPT."""
        try:
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json'
            }

            data = {
                'model': self.model,
                'messages': [
                    {'role': 'system', 'content': 'You are a professional forex market analyst.'},
                    {'role': 'user', 'content': prompt}
                ],
                'max_tokens': self.max_tokens,
                'temperature': self.temperature
            }

            response = requests.post(
                self.base_url,
                headers=headers,
                json=data,
                timeout=30
            )

            response.raise_for_status()
            result = response.json()

            if 'choices' in result and len(result['choices']) > 0:
                return result['choices'][0]['message']['content'].strip()
            else:
                logger.error("Unexpected ChatGPT API response", response=result)
                return None

        except requests.exceptions.RequestException as e:
            logger.error("ChatGPT API request failed", error=str(e))
            return None
        except Exception as e:
            logger.error("Unexpected error in ChatGPT API call", error=str(e))
            return None

    def _generate_mock_analysis(self, news_item: Dict[str, Any]) -> str:
        """Generate mock analysis when ChatGPT is not available."""
        event = news_item.get('event', 'N/A')
        currency = news_item.get('currency', 'N/A')
        impact = news_item.get('impact', 'N/A')

        mock_analyses = {
            'high': f"High impact event for {currency}. Monitor price action closely for potential volatility spikes.",
            'medium': f"Medium impact event for {currency}. Moderate market reaction expected.",
            'low': f"Low impact event for {currency}. Minimal market reaction anticipated."
        }

        return mock_analyses.get(impact, f"Analysis for {event} in {currency} market.")


class TechnicalAnalysisService:
    """Service for technical analysis of forex data."""

    def __init__(self):
        self.chart_service = chart_service

    async def analyze_price_data(self, symbol: str, period_days: int = 30) -> Dict[str, Any]:
        """Perform technical analysis on price data."""
        try:
            # Fetch price data
            end_time = datetime.now()
            start_time = end_time - timedelta(days=period_days)

            # Create chart request
            from app.models.chart import ChartRequest
            request = ChartRequest(
                currency=symbol,
                event_name=f"{symbol} Technical Analysis",
                start_time=start_time,
                end_time=end_time,
                chart_type='daily'
            )

            # Get chart data
            response = await self.chart_service.generate_chart(request)

            if not response.success or not response.chart_data:
                logger.error("Failed to get price data for technical analysis", symbol=symbol)
                return {"error": "Failed to fetch price data"}

            # Convert to DataFrame
            df = pd.DataFrame([{
                'Open': item.open,
                'High': item.high,
                'Low': item.low,
                'Close': item.close,
                'Volume': item.volume
            } for item in response.chart_data.data])

            df.index = pd.to_datetime([item.timestamp for item in response.chart_data.data])

            # Perform technical analysis
            analysis = await self._perform_technical_analysis(df, symbol)

            logger.info("Technical analysis completed", symbol=symbol, indicators=len(analysis))
            return analysis

        except Exception as e:
            logger.error("Failed to analyze price data", symbol=symbol, error=str(e))
            return {"error": str(e)}

    async def _perform_technical_analysis(self, df: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        """Perform comprehensive technical analysis."""
        try:
            analysis = {
                "symbol": symbol,
                "analysis_date": datetime.now().isoformat(),
                "data_points": len(df),
                "indicators": {}
            }

            if len(df) < 20:
                analysis["error"] = "Insufficient data for technical analysis"
                return analysis

            close = df['Close']
            high = df['High']
            low = df['Low']
            volume = df['Volume']

            # Moving Averages
            analysis["indicators"]["sma_20"] = float(close.rolling(window=20).mean().iloc[-1])
            analysis["indicators"]["sma_50"] = float(close.rolling(window=50).mean().iloc[-1]) if len(df) >= 50 else None
            analysis["indicators"]["ema_12"] = float(_ema(close, 12).iloc[-1])
            analysis["indicators"]["ema_26"] = float(_ema(close, 26).iloc[-1])

            # RSI
            analysis["indicators"]["rsi"] = float(_rsi(close).iloc[-1])

            # MACD
            macd_line, signal_line, histogram = _macd(close)
            analysis["indicators"]["macd"] = float(macd_line.iloc[-1])
            analysis["indicators"]["macd_signal"] = float(signal_line.iloc[-1])
            analysis["indicators"]["macd_histogram"] = float(histogram.iloc[-1])

            # Bollinger Bands
            bb_upper, bb_middle, bb_lower = _bollinger_bands(close)
            analysis["indicators"]["bb_upper"] = float(bb_upper.iloc[-1])
            analysis["indicators"]["bb_middle"] = float(bb_middle.iloc[-1])
            analysis["indicators"]["bb_lower"] = float(bb_lower.iloc[-1])

            # ATR
            analysis["indicators"]["atr"] = float(_atr(high, low, close).iloc[-1])

            # Price levels
            analysis["indicators"]["current_price"] = float(close.iloc[-1])
            analysis["indicators"]["high_20d"] = float(high.rolling(window=20).max().iloc[-1])
            analysis["indicators"]["low_20d"] = float(low.rolling(window=20).min().iloc[-1])

            # Volume analysis
            analysis["indicators"]["avg_volume"] = float(volume.rolling(window=20).mean().iloc[-1])
            analysis["indicators"]["volume_ratio"] = float(volume.iloc[-1] / analysis["indicators"]["avg_volume"])

            # Generate trading signals
            analysis["signals"] = self._generate_trading_signals(analysis["indicators"])

            # Generate summary
            analysis["summary"] = self._generate_analysis_summary(analysis["indicators"], analysis["signals"])

            return analysis

        except Exception as e:
            logger.error("Failed to perform technical analysis", error=str(e))
            return {"error": str(e)}

    def _generate_trading_signals(self, indicators: Dict[str, Any]) -> Dict[str, str]:
        """Generate trading signals based on technical indicators."""
        signals = {}

        # RSI signals
        rsi = indicators.get("rsi", 50)
        if rsi > 70:
            signals["rsi"] = "Overbought"
        elif rsi < 30:
            signals["rsi"] = "Oversold"
        else:
            signals["rsi"] = "Neutral"

        # MACD signals
        macd = indicators.get("macd", 0)
        macd_signal = indicators.get("macd_signal", 0)
        if macd > macd_signal:
            signals["macd"] = "Bullish"
        else:
            signals["macd"] = "Bearish"

        # Moving Average signals
        current_price = indicators.get("current_price", 0)
        sma_20 = indicators.get("sma_20", 0)
        if current_price > sma_20:
            signals["ma"] = "Bullish"
        else:
            signals["ma"] = "Bearish"

        # Bollinger Bands signals
        bb_upper = indicators.get("bb_upper", 0)
        bb_lower = indicators.get("bb_lower", 0)
        if current_price > bb_upper:
            signals["bb"] = "Overbought"
        elif current_price < bb_lower:
            signals["bb"] = "Oversold"
        else:
            signals["bb"] = "Neutral"

        return signals

    def _generate_analysis_summary(self, indicators: Dict[str, Any], signals: Dict[str, str]) -> str:
        """Generate a summary of the technical analysis."""
        try:
            current_price = indicators.get("current_price", 0)
            rsi = indicators.get("rsi", 50)
            macd_signal = signals.get("macd", "Neutral")
            ma_signal = signals.get("ma", "Neutral")

            summary_parts = []

            # Price trend
            if ma_signal == "Bullish":
                summary_parts.append("📈 Bullish trend above 20-day MA")
            else:
                summary_parts.append("📉 Bearish trend below 20-day MA")

            # RSI
            if rsi > 70:
                summary_parts.append("⚠️ RSI indicates overbought conditions")
            elif rsi < 30:
                summary_parts.append("⚠️ RSI indicates oversold conditions")
            else:
                summary_parts.append("✅ RSI in neutral range")

            # MACD
            if macd_signal == "Bullish":
                summary_parts.append("🚀 MACD shows bullish momentum")
            else:
                summary_parts.append("📉 MACD shows bearish momentum")

            return " | ".join(summary_parts)

        except Exception as e:
            logger.error("Failed to generate analysis summary", error=str(e))
            return "Technical analysis completed"


class GPTAnalysisService:
    """Main service for GPT-based analysis."""

    def __init__(self):
        self.chatgpt_analyzer = ChatGPTAnalyzer()
        self.technical_analyzer = TechnicalAnalysisService()
        self.client = None
        self.last_request_time = None

    async def analyze_news_event(self, news_item: Dict[str, Any]) -> str:
        """Analyze a news event using GPT."""
        try:
            analysis = await self.chatgpt_analyzer.analyze_news(news_item)

            # Escape markdown for Telegram
            escaped_analysis = escape_markdown_v2(analysis)

            logger.info("News event analysis completed", event=news_item.get('event', 'N/A'))
            return escaped_analysis

        except Exception as e:
            logger.error("Failed to analyze news event", error=str(e))
            return f"Analysis error: {str(e)}"

    async def analyze_market_conditions(self, symbol: str, period_days: int = 30) -> Dict[str, Any]:
        """Analyze market conditions using technical analysis."""
        try:
            analysis = await self.technical_analyzer.analyze_price_data(symbol, period_days)

            logger.info("Market conditions analysis completed", symbol=symbol)
            return analysis

        except Exception as e:
            logger.error("Failed to analyze market conditions", symbol=symbol, error=str(e))
            return {"error": str(e)}

    async def generate_comprehensive_analysis(self, news_item: Dict[str, Any], symbol: str) -> Dict[str, Any]:
        """Generate comprehensive analysis combining news and technical analysis."""
        try:
            # Get news analysis
            news_analysis = await self.analyze_news_event(news_item)

            # Get technical analysis
            technical_analysis = await self.analyze_market_conditions(symbol)

            # Combine analyses
            comprehensive = {
                "news_analysis": news_analysis,
                "technical_analysis": technical_analysis,
                "analysis_timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "event": news_item.get('event', 'N/A')
            }

            logger.info("Comprehensive analysis completed", symbol=symbol, event=news_item.get('event', 'N/A'))
            return comprehensive

        except Exception as e:
            logger.error("Failed to generate comprehensive analysis", symbol=symbol, error=str(e))
            return {"error": str(e)}

    async def health_check(self) -> Dict[str, Any]:
        """Check the health of the GPT analysis service."""
        try:
            if not self.client:
                return {
                    "status": "unhealthy",
                    "openai": "not_initialized"
                }

            # Test OpenAI connection
            try:
                await self.client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": "test"}],
                    max_tokens=1
                )
                openai_status = "connected"
                status = "healthy"
            except Exception:
                openai_status = "error"
                status = "unhealthy"

            return {
                "status": status,
                "openai": openai_status,
                "technical_analyzer_available": True,
                "rate_limit_active": True,
                "last_gpt_calls": len(_LAST_GPT_CALLS)
            }

        except Exception as e:
            logger.error("GPT analysis service health check failed", error=str(e))
            return {
                "status": "unhealthy",
                "error": str(e)
            }

    async def initialize(self):
        """Initialize the GPT service."""
        try:
            import openai
            self.client = openai.AsyncOpenAI(api_key=settings.api.openai_api_key)
            logger.info("GPT service initialized successfully")
        except Exception as e:
            logger.error("Failed to initialize GPT service", error=str(e))
            raise AnalysisError(f"Failed to initialize GPT service: {e}")

    async def analyze_news_event(self, news_data) -> str:
        """Analyze news event using GPT."""
        try:
            if not self.client:
                raise AnalysisError("GPT client not initialized")

            # Check rate limit
            if not await self._check_rate_limit():
                raise AnalysisError("Rate limit exceeded")

            # Create analysis prompt
            prompt = f"Analyze the following forex news event: {news_data}"

            # Call GPT API
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500
            )

            analysis = response.choices[0].message.content
            self.last_request_time = datetime.now()

            logger.info("News event analysis completed")
            return analysis

        except Exception as e:
            logger.error("Failed to analyze news event", error=str(e))
            raise ExternalAPIError(f"Failed to analyze news event: {e}")

    async def analyze_price_data(self, price_data: Dict[str, Any]) -> str:
        """Analyze price data using GPT."""
        try:
            if not self.client:
                raise AnalysisError("GPT client not initialized")

            # Check rate limit
            if not await self._check_rate_limit():
                raise AnalysisError("Rate limit exceeded")

            # Create analysis prompt
            prompt = await self._format_analysis_prompt(price_data, "price_analysis")

            # Call GPT API
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500
            )

            analysis = response.choices[0].message.content
            self.last_request_time = datetime.now()

            logger.info("Price data analysis completed")
            return analysis

        except Exception as e:
            logger.error("Failed to analyze price data", error=str(e))
            raise ExternalAPIError(f"Failed to analyze price data: {e}")

    async def analyze_market_sentiment(self, market_data: Dict[str, Any]) -> str:
        """Analyze market sentiment using GPT."""
        try:
            if not self.client:
                raise AnalysisError("GPT client not initialized")

            # Check rate limit
            if not await self._check_rate_limit():
                raise AnalysisError("Rate limit exceeded")

            # Create analysis prompt
            prompt = await self._format_analysis_prompt(market_data, "sentiment_analysis")

            # Call GPT API
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500
            )

            analysis = response.choices[0].message.content
            self.last_request_time = datetime.now()

            logger.info("Market sentiment analysis completed")
            return analysis

        except Exception as e:
            logger.error("Failed to analyze market sentiment", error=str(e))
            raise ExternalAPIError(f"Failed to analyze market sentiment: {e}")

    async def generate_trading_signals(self, analysis_data: Dict[str, Any]) -> str:
        """Generate trading signals using GPT."""
        try:
            if not self.client:
                raise AnalysisError("GPT client not initialized")

            # Check rate limit
            if not await self._check_rate_limit():
                raise AnalysisError("Rate limit exceeded")

            # Create analysis prompt
            prompt = await self._format_analysis_prompt(analysis_data, "trading_signals")

            # Call GPT API
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500
            )

            signals = response.choices[0].message.content
            self.last_request_time = datetime.now()

            logger.info("Trading signals generated")
            return signals

        except Exception as e:
            logger.error("Failed to generate trading signals", error=str(e))
            raise ExternalAPIError(f"Failed to generate trading signals: {e}")

    async def calculate_technical_indicators(self, price_data) -> Dict[str, Any]:
        """Calculate technical indicators."""
        try:
            # Handle different input formats
            if isinstance(price_data, list):
                prices = price_data
                if len(prices) < 5:  # Minimum for basic indicators
                    raise AnalysisError("Insufficient price data for technical analysis")
            elif isinstance(price_data, dict):
                prices = price_data.get('prices', [])
                if len(prices) < 20:
                    raise AnalysisError("Insufficient price data for technical analysis")
            else:
                raise AnalysisError("Invalid price data format")

            df = pd.DataFrame(prices)

            # Ensure we have numeric data
            for col in ['close', 'high', 'low']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            # For ATR calculation, if we don't have high/low, use close price
            if 'high' not in df.columns or 'low' not in df.columns:
                df['high'] = df['close'] * 1.001  # Simulate high
                df['low'] = df['close'] * 0.999   # Simulate low

            # Calculate indicators
            indicators = {
                'rsi': await self.calculate_rsi(df['close']),
                'ema_20': await self.calculate_ema(df['close'], 20),
                'ema_50': await self.calculate_ema(df['close'], 50),
                'macd': await self.calculate_macd(df['close']),
                'atr': await self.calculate_atr(df)
            }

            logger.info("Technical indicators calculated")
            return indicators

        except Exception as e:
            logger.error("Failed to calculate technical indicators", error=str(e))
            raise AnalysisError(f"Failed to calculate technical indicators: {e}")

    async def calculate_rsi(self, prices) -> float:
        """Calculate RSI indicator."""
        try:
            if isinstance(prices, list):
                prices = pd.Series(prices)
            rsi_values = _rsi(prices)
            result = float(rsi_values.iloc[-1])
            # Handle NaN values
            if pd.isna(result):
                return 50.0  # Default neutral RSI
            return result
        except Exception as e:
            logger.error("Failed to calculate RSI", error=str(e))
            raise AnalysisError(f"Failed to calculate RSI: {e}")

    async def calculate_ema(self, prices, period: int) -> float:
        """Calculate EMA indicator."""
        try:
            if isinstance(prices, list):
                prices = pd.Series(prices)
            ema_values = _ema(prices, period)
            return float(ema_values.iloc[-1])
        except Exception as e:
            logger.error("Failed to calculate EMA", error=str(e))
            raise AnalysisError(f"Failed to calculate EMA: {e}")

    async def calculate_macd(self, prices) -> Dict[str, float]:
        """Calculate MACD indicator."""
        try:
            if isinstance(prices, list):
                prices = pd.Series(prices)
            macd_line, signal_line, histogram = _macd(prices)
            return {
                'macd': float(macd_line.iloc[-1]),
                'signal': float(signal_line.iloc[-1]),
                'histogram': float(histogram.iloc[-1])
            }
        except Exception as e:
            logger.error("Failed to calculate MACD", error=str(e))
            raise AnalysisError(f"Failed to calculate MACD: {e}")

    async def calculate_atr(self, price_data) -> float:
        """Calculate ATR indicator."""
        try:
            if isinstance(price_data, dict):
                price_data = pd.DataFrame(price_data)
            elif isinstance(price_data, list):
                price_data = pd.DataFrame(price_data)
            atr_values = _atr(price_data['high'], price_data['low'], price_data['close'])
            result = float(atr_values.iloc[-1])
            # Handle NaN values
            if pd.isna(result):
                return 0.001  # Default small ATR value
            return result
        except Exception as e:
            logger.error("Failed to calculate ATR", error=str(e))
            raise AnalysisError(f"Failed to calculate ATR: {e}")

    async def _check_rate_limit(self) -> bool:
        """Check if rate limit is exceeded."""
        try:
            if not self.last_request_time:
                return True

            time_since_last = datetime.now() - self.last_request_time
            return time_since_last.total_seconds() >= 10  # 10 second rate limit

        except Exception as e:
            logger.error("Failed to check rate limit", error=str(e))
            return False

    async def _format_analysis_prompt(self, data: Dict[str, Any], prompt_type: str) -> str:
        """Format analysis prompt based on data and type."""
        try:
            if prompt_type == "price_analysis":
                return f"Analyze the following price data: {data}"
            elif prompt_type == "sentiment_analysis":
                return f"Analyze market sentiment for: {data}"
            elif prompt_type == "trading_signals":
                return f"Generate trading signals based on: {data}"
            elif prompt_type == "news_analysis":
                return f"Analyze the following forex news: {data}"
            else:
                raise AnalysisError(f"Invalid prompt type: {prompt_type}")

        except Exception as e:
            logger.error("Failed to format analysis prompt", error=str(e))
            raise AnalysisError(f"Failed to format analysis prompt: {e}")

    async def close(self):
        """Close the GPT service."""
        try:
            if self.client:
                await self.client.close()
                self.client = None
            logger.info("GPT service closed successfully")
        except Exception as e:
            logger.error("Failed to close GPT service", error=str(e))


# Global GPT analysis service instance
gpt_analysis_service = GPTAnalysisService()
