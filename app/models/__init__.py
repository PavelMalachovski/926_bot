"""Pydantic models for the Forex Bot application."""

from .chart import ChartData, ChartRequest, ChartResponse, OHLCData
from .forex_news import (ForexNews, ForexNewsCreate, ForexNewsResponse,
                         ForexNewsUpdate)
from .notification import (Notification, NotificationCreate,
                           NotificationResponse)
from .telegram import (TelegramCallbackQuery, TelegramMessage, TelegramUpdate,
                       TelegramUser)
from .user import User, UserCreate, UserPreferences, UserResponse, UserUpdate

__all__ = [
    # User models
    "User",
    "UserCreate",
    "UserUpdate",
    "UserPreferences",
    "UserResponse",
    # Forex news models
    "ForexNews",
    "ForexNewsCreate",
    "ForexNewsUpdate",
    "ForexNewsResponse",
    # Chart models
    "ChartRequest",
    "ChartResponse",
    "ChartData",
    "OHLCData",
    # Notification models
    "Notification",
    "NotificationCreate",
    "NotificationResponse",
    # Telegram models
    "TelegramUpdate",
    "TelegramMessage",
    "TelegramUser",
    "TelegramCallbackQuery",
]
