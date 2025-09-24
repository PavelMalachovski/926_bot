"""Tests for GPT analysis service."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta

from app.services.gpt_analysis_service import GPTAnalysisService
from app.core.exceptions import AnalysisError, ExternalAPIError
from tests.factories import ForexNewsCreateFactory


@pytest.fixture
def gpt_service():
    """Create GPT analysis service instance."""
    return GPTAnalysisService()


@pytest.fixture
def mock_openai_client():
    """Create mock OpenAI client."""
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = "This is a test analysis."
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


class TestGPTAnalysisService:
    """Test cases for GPTAnalysisService."""

    @pytest.mark.asyncio
    async def test_initialize_success(self, gpt_service):
        """Test successful GPT service initialization."""
        # Arrange
        with patch('openai.AsyncOpenAI') as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            # Act
            await gpt_service.initialize()

            # Assert
            assert gpt_service.client is not None

    @pytest.mark.asyncio
    async def test_initialize_error(self, gpt_service):
        """Test GPT service initialization with error."""
        # Arrange
        with patch('openai.AsyncOpenAI', side_effect=Exception("OpenAI error")):
            # Act & Assert
            with pytest.raises(AnalysisError):
                await gpt_service.initialize()

    @pytest.mark.asyncio
    async def test_analyze_news_event_success(self, gpt_service, mock_openai_client):
        """Test successful news event analysis."""
        # Arrange
        gpt_service.client = mock_openai_client
        news_data = ForexNewsCreateFactory.build()

        # Act
        result = await gpt_service.analyze_news_event(news_data)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0
        mock_openai_client.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_news_event_error(self, gpt_service, mock_openai_client):
        """Test news event analysis with error."""
        # Arrange
        gpt_service.client = mock_openai_client
        news_data = ForexNewsCreateFactory.build()
        mock_openai_client.chat.completions.create.side_effect = Exception("OpenAI API error")

        # Act & Assert
        with pytest.raises(ExternalAPIError):
            await gpt_service.analyze_news_event(news_data)

    @pytest.mark.asyncio
    async def test_analyze_price_data_success(self, gpt_service, mock_openai_client):
        """Test successful price data analysis."""
        # Arrange
        gpt_service.client = mock_openai_client
        price_data = {
            "symbol": "EURUSD",
            "timeframe": "1h",
            "data": [
                {"timestamp": "2024-01-15T10:00:00Z", "open": 1.0850, "high": 1.0860, "low": 1.0840, "close": 1.0855},
                {"timestamp": "2024-01-15T11:00:00Z", "open": 1.0855, "high": 1.0865, "low": 1.0845, "close": 1.0860},
            ]
        }

        # Act
        result = await gpt_service.analyze_price_data(price_data)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0
        mock_openai_client.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_price_data_error(self, gpt_service, mock_openai_client):
        """Test price data analysis with error."""
        # Arrange
        gpt_service.client = mock_openai_client
        price_data = {"symbol": "EURUSD", "data": []}
        mock_openai_client.chat.completions.create.side_effect = Exception("OpenAI API error")

        # Act & Assert
        with pytest.raises(ExternalAPIError):
            await gpt_service.analyze_price_data(price_data)

    @pytest.mark.asyncio
    async def test_analyze_market_sentiment_success(self, gpt_service, mock_openai_client):
        """Test successful market sentiment analysis."""
        # Arrange
        gpt_service.client = mock_openai_client
        market_data = {
            "currency": "USD",
            "news_events": [ForexNewsCreateFactory.build() for _ in range(3)],
            "price_movement": "bullish"
        }

        # Act
        result = await gpt_service.analyze_market_sentiment(market_data)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0
        mock_openai_client.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_market_sentiment_error(self, gpt_service, mock_openai_client):
        """Test market sentiment analysis with error."""
        # Arrange
        gpt_service.client = mock_openai_client
        market_data = {"currency": "USD", "news_events": [], "price_movement": "neutral"}
        mock_openai_client.chat.completions.create.side_effect = Exception("OpenAI API error")

        # Act & Assert
        with pytest.raises(ExternalAPIError):
            await gpt_service.analyze_market_sentiment(market_data)

    @pytest.mark.asyncio
    async def test_generate_trading_signals_success(self, gpt_service, mock_openai_client):
        """Test successful trading signals generation."""
        # Arrange
        gpt_service.client = mock_openai_client
        analysis_data = {
            "symbol": "EURUSD",
            "technical_indicators": {"rsi": 65, "macd": 0.001, "ema_20": 1.0850},
            "news_impact": "high",
            "market_sentiment": "bullish"
        }

        # Act
        result = await gpt_service.generate_trading_signals(analysis_data)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0
        mock_openai_client.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_trading_signals_error(self, gpt_service, mock_openai_client):
        """Test trading signals generation with error."""
        # Arrange
        gpt_service.client = mock_openai_client
        analysis_data = {"symbol": "EURUSD", "technical_indicators": {}}
        mock_openai_client.chat.completions.create.side_effect = Exception("OpenAI API error")

        # Act & Assert
        with pytest.raises(ExternalAPIError):
            await gpt_service.generate_trading_signals(analysis_data)

    @pytest.mark.asyncio
    async def test_calculate_technical_indicators_success(self, gpt_service):
        """Test successful technical indicators calculation."""
        # Arrange
        price_data = [
            {"close": 1.0850}, {"close": 1.0855}, {"close": 1.0860}, {"close": 1.0855}, {"close": 1.0865}
        ]

        # Act
        result = await gpt_service.calculate_technical_indicators(price_data)

        # Assert
        assert "rsi" in result
        assert "ema_20" in result
        assert "macd" in result
        assert "atr" in result

    @pytest.mark.asyncio
    async def test_calculate_technical_indicators_insufficient_data(self, gpt_service):
        """Test technical indicators calculation with insufficient data."""
        # Arrange
        price_data = [{"close": 1.0850}]  # Only one data point

        # Act & Assert
        with pytest.raises(AnalysisError):
            await gpt_service.calculate_technical_indicators(price_data)

    @pytest.mark.asyncio
    async def test_calculate_rsi_success(self, gpt_service):
        """Test successful RSI calculation."""
        # Arrange
        prices = [1.0850, 1.0855, 1.0860, 1.0855, 1.0865, 1.0870, 1.0865, 1.0875]

        # Act
        result = await gpt_service.calculate_rsi(prices)

        # Assert
        assert isinstance(result, float)
        assert 0 <= result <= 100

    @pytest.mark.asyncio
    async def test_calculate_ema_success(self, gpt_service):
        """Test successful EMA calculation."""
        # Arrange
        prices = [1.0850, 1.0855, 1.0860, 1.0855, 1.0865, 1.0870, 1.0865, 1.0875]
        period = 5

        # Act
        result = await gpt_service.calculate_ema(prices, period)

        # Assert
        assert isinstance(result, float)
        assert result > 0

    @pytest.mark.asyncio
    async def test_calculate_macd_success(self, gpt_service):
        """Test successful MACD calculation."""
        # Arrange
        prices = [1.0850, 1.0855, 1.0860, 1.0855, 1.0865, 1.0870, 1.0865, 1.0875]

        # Act
        result = await gpt_service.calculate_macd(prices)

        # Assert
        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result
        assert isinstance(result["macd"], float)

    @pytest.mark.asyncio
    async def test_calculate_atr_success(self, gpt_service):
        """Test successful ATR calculation."""
        # Arrange
        price_data = [
            {"high": 1.0860, "low": 1.0840, "close": 1.0850},
            {"high": 1.0865, "low": 1.0845, "close": 1.0855},
            {"high": 1.0870, "low": 1.0850, "close": 1.0860},
        ]

        # Act
        result = await gpt_service.calculate_atr(price_data)

        # Assert
        assert isinstance(result, float)
        assert result > 0

    @pytest.mark.asyncio
    async def test_rate_limit_check_success(self, gpt_service):
        """Test successful rate limit check."""
        # Arrange
        gpt_service.last_request_time = datetime.now() - timedelta(seconds=20)

        # Act
        result = await gpt_service._check_rate_limit()

        # Assert
        assert result is True

    @pytest.mark.asyncio
    async def test_rate_limit_check_failure(self, gpt_service):
        """Test rate limit check failure."""
        # Arrange
        gpt_service.last_request_time = datetime.now() - timedelta(seconds=5)

        # Act
        result = await gpt_service._check_rate_limit()

        # Assert
        assert result is False

    @pytest.mark.asyncio
    async def test_format_analysis_prompt_success(self, gpt_service):
        """Test successful analysis prompt formatting."""
        # Arrange
        news_data = ForexNewsCreateFactory.build()
        prompt_type = "news_analysis"

        # Act
        result = await gpt_service._format_analysis_prompt(news_data, prompt_type)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0
        assert news_data.event in result

    @pytest.mark.asyncio
    async def test_format_analysis_prompt_invalid_type(self, gpt_service):
        """Test analysis prompt formatting with invalid type."""
        # Arrange
        news_data = ForexNewsCreateFactory.build()
        prompt_type = "invalid_type"

        # Act & Assert
        with pytest.raises(AnalysisError):
            await gpt_service._format_analysis_prompt(news_data, prompt_type)

    @pytest.mark.asyncio
    async def test_health_check_success(self, gpt_service, mock_openai_client):
        """Test successful health check."""
        # Arrange
        gpt_service.client = mock_openai_client

        # Act
        result = await gpt_service.health_check()

        # Assert
        assert result["status"] == "healthy"
        assert result["openai"] == "connected"

    @pytest.mark.asyncio
    async def test_health_check_no_client(self, gpt_service):
        """Test health check when client is not initialized."""
        # Arrange
        gpt_service.client = None

        # Act
        result = await gpt_service.health_check()

        # Assert
        assert result["status"] == "unhealthy"
        assert result["openai"] == "not_initialized"

    @pytest.mark.asyncio
    async def test_health_check_error(self, gpt_service, mock_openai_client):
        """Test health check with error."""
        # Arrange
        gpt_service.client = mock_openai_client
        mock_openai_client.chat.completions.create.side_effect = Exception("OpenAI API error")

        # Act
        result = await gpt_service.health_check()

        # Assert
        assert result["status"] == "unhealthy"
        assert result["openai"] == "error"

    @pytest.mark.asyncio
    async def test_close_success(self, gpt_service, mock_openai_client):
        """Test successful GPT service close."""
        # Arrange
        gpt_service.client = mock_openai_client

        # Act
        await gpt_service.close()

        # Assert
        mock_openai_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_no_client(self, gpt_service):
        """Test GPT service close when client is not initialized."""
        # Arrange
        gpt_service.client = None

        # Act
        await gpt_service.close()

        # Assert
        # Should not raise any exception
