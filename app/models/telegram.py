"""Telegram-related Pydantic models."""

from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class TelegramUser(BaseModel):
    """Telegram user model."""

    id: int = Field(description="User ID")
    is_bot: bool = Field(description="Is bot")
    first_name: str = Field(description="First name")
    last_name: Optional[str] = Field(default=None, description="Last name")
    username: Optional[str] = Field(default=None, description="Username")
    language_code: Optional[str] = Field(default=None, description="Language code")
    is_premium: Optional[bool] = Field(default=None, description="Is premium")


class TelegramChat(BaseModel):
    """Telegram chat model."""

    id: int = Field(description="Chat ID")
    type: str = Field(description="Chat type")
    title: Optional[str] = Field(default=None, description="Chat title")
    username: Optional[str] = Field(default=None, description="Chat username")
    first_name: Optional[str] = Field(default=None, description="First name")
    last_name: Optional[str] = Field(default=None, description="Last name")


class TelegramMessage(BaseModel):
    """Telegram message model."""

    message_id: int = Field(description="Message ID")
    from_user: Optional[TelegramUser] = Field(default=None, description="From user")
    chat: TelegramChat = Field(description="Chat")
    date: int = Field(description="Message date")
    text: Optional[str] = Field(default=None, description="Message text")
    entities: Optional[list] = Field(default=None, description="Message entities")


class TelegramCallbackQuery(BaseModel):
    """Telegram callback query model."""

    id: str = Field(description="Callback query ID")
    from_user: TelegramUser = Field(description="From user")
    message: Optional[TelegramMessage] = Field(default=None, description="Message")
    data: Optional[str] = Field(default=None, description="Callback data")


class TelegramUpdate(BaseModel):
    """Telegram update model."""

    update_id: int = Field(description="Update ID")
    message: Optional[TelegramMessage] = Field(default=None, description="Message")
    callback_query: Optional[TelegramCallbackQuery] = Field(default=None, description="Callback query")

    class Config:
        extra = "allow"  # Allow additional fields from Telegram API
