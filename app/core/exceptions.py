"""Application exceptions."""

from typing import Any, Dict, Optional


class ForexBotException(Exception):
    """Base exception with optional error code and details."""

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(self.message)


class ConfigurationError(ForexBotException):
    """Invalid or missing configuration."""


class DataFetchError(ForexBotException):
    """Failed to fetch market data (Binance/OANDA)."""


class TelegramError(ForexBotException):
    """Telegram API operation failed."""
