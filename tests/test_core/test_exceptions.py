"""Tests for core exceptions module."""

import pytest
from app.core.exceptions import (
    ForexBotException,
    ConfigurationError,
    DatabaseError,
    DataFetchError,
    ChartGenerationError,
    TelegramError,
    ValidationError,
    AuthenticationError,
    AuthorizationError,
    RateLimitError,
    ExternalAPIError,
    ScrapingError,
    NotificationError,
)


class TestForexBotException:
    """Test base ForexBotException."""

    def test_basic_exception(self):
        """Test basic exception creation."""
        exc = ForexBotException("Test error")

        assert str(exc) == "Test error"
        assert exc.message == "Test error"
        assert exc.error_code is None
        assert exc.details == {}

    def test_exception_with_error_code(self):
        """Test exception with error code."""
        exc = ForexBotException("Test error", error_code="TEST_001")

        assert exc.message == "Test error"
        assert exc.error_code == "TEST_001"
        assert exc.details == {}

    def test_exception_with_details(self):
        """Test exception with details."""
        details = {"field": "value", "code": 123}
        exc = ForexBotException("Test error", details=details)

        assert exc.message == "Test error"
        assert exc.error_code is None
        assert exc.details == details

    def test_exception_with_all_params(self):
        """Test exception with all parameters."""
        details = {"field": "value"}
        exc = ForexBotException(
            "Test error",
            error_code="TEST_001",
            details=details
        )

        assert exc.message == "Test error"
        assert exc.error_code == "TEST_001"
        assert exc.details == details


class TestSpecificExceptions:
    """Test specific exception types."""

    def test_configuration_error(self):
        """Test ConfigurationError."""
        exc = ConfigurationError("Config error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Config error"

    def test_database_error(self):
        """Test DatabaseError."""
        exc = DatabaseError("Database error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Database error"

    def test_data_fetch_error(self):
        """Test DataFetchError."""
        exc = DataFetchError("Data fetch error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Data fetch error"

    def test_chart_generation_error(self):
        """Test ChartGenerationError."""
        exc = ChartGenerationError("Chart generation error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Chart generation error"

    def test_telegram_error(self):
        """Test TelegramError."""
        exc = TelegramError("Telegram error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Telegram error"

    def test_validation_error(self):
        """Test ValidationError."""
        exc = ValidationError("Validation error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Validation error"

    def test_authentication_error(self):
        """Test AuthenticationError."""
        exc = AuthenticationError("Authentication error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Authentication error"

    def test_authorization_error(self):
        """Test AuthorizationError."""
        exc = AuthorizationError("Authorization error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Authorization error"

    def test_rate_limit_error(self):
        """Test RateLimitError."""
        exc = RateLimitError("Rate limit exceeded")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Rate limit exceeded"

    def test_external_api_error(self):
        """Test ExternalAPIError."""
        exc = ExternalAPIError("External API error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "External API error"

    def test_scraping_error(self):
        """Test ScrapingError."""
        exc = ScrapingError("Scraping error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Scraping error"

    def test_notification_error(self):
        """Test NotificationError."""
        exc = NotificationError("Notification error")

        assert isinstance(exc, ForexBotException)
        assert str(exc) == "Notification error"


class TestExceptionInheritance:
    """Test exception inheritance and behavior."""

    def test_exception_raising(self):
        """Test that exceptions can be raised and caught."""
        with pytest.raises(ConfigurationError) as exc_info:
            raise ConfigurationError("Test config error")

        assert str(exc_info.value) == "Test config error"
        assert isinstance(exc_info.value, ForexBotException)

    def test_exception_chaining(self):
        """Test exception chaining."""
        try:
            raise ValueError("Original error")
        except ValueError as e:
            with pytest.raises(DatabaseError) as exc_info:
                raise DatabaseError("Database error") from e

            assert str(exc_info.value) == "Database error"
            assert exc_info.value.__cause__ is e
