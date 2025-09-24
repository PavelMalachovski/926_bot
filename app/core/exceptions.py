"""Custom exceptions for the Forex Bot application."""

from typing import Optional, Dict, Any


class ForexBotException(Exception):
    """Base exception for Forex Bot application."""

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(self.message)


class ConfigurationError(ForexBotException):
    """Configuration-related errors."""
    pass


class DatabaseError(ForexBotException):
    """Database operation errors."""
    pass


class DataFetchError(ForexBotException):
    """Data fetching errors."""
    pass


class ChartGenerationError(ForexBotException):
    """Chart generation errors."""
    pass


class TelegramError(ForexBotException):
    """Telegram bot errors."""
    pass


class ValidationError(ForexBotException):
    """Data validation errors."""
    pass


class AuthenticationError(ForexBotException):
    """Authentication errors."""
    pass


class AuthorizationError(ForexBotException):
    """Authorization errors."""
    pass


class RateLimitError(ForexBotException):
    """Rate limiting errors."""
    pass


class ExternalAPIError(ForexBotException):
    """External API errors."""
    pass


class ScrapingError(ForexBotException):
    """Web scraping errors."""
    pass


class NotificationError(ForexBotException):
    """Notification errors."""
    pass


class UserSettingsError(ForexBotException):
    """User settings errors."""
    pass


class DigestError(ForexBotException):
    """Digest errors."""
    pass


class VisualizationError(ForexBotException):
    """Visualization errors."""
    pass


class AnalysisError(ForexBotException):
    """Analysis errors."""
    pass


class SchedulerError(ForexBotException):
    """Scheduler errors."""
    pass


class CacheError(ForexBotException):
    """Cache operation errors."""
    pass
