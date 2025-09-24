"""User-related Pydantic models."""

from datetime import datetime, time
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


class UserPreferences(BaseModel):
    """User preferences model."""

    preferred_currencies: List[str] = Field(default=[], description="Preferred currencies")
    impact_levels: List[str] = Field(default=["high", "medium"], description="Impact levels to follow")
    analysis_required: bool = Field(default=True, description="Require AI analysis")
    digest_time: time = Field(default=time(8, 0), description="Daily digest time")
    timezone: str = Field(default="Europe/Prague", description="User timezone")

    # Notification settings
    notifications_enabled: bool = Field(default=False, description="Enable notifications")
    notification_minutes: int = Field(default=30, description="Notification minutes before event")
    notification_impact_levels: List[str] = Field(default=["high"], description="Notification impact levels")

    # Chart settings
    charts_enabled: bool = Field(default=False, description="Enable charts")
    chart_type: str = Field(default="single", description="Chart type")
    chart_window_hours: int = Field(default=2, description="Chart window hours")

    @field_validator("preferred_currencies")
    @classmethod
    def validate_currencies(cls, v):
        """Validate currency codes."""
        valid_currencies = {
            "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
            "CNY", "INR", "BRL", "RUB", "KRW", "MXN", "SGD", "HKD",
            "XAU", "BTC", "ETH"
        }
        for currency in v:
            if currency not in valid_currencies:
                raise ValueError(f"Invalid currency: {currency}")
        return v

    @field_validator("impact_levels")
    @classmethod
    def validate_impact_levels(cls, v):
        """Validate impact levels."""
        valid_levels = {"high", "medium", "low"}
        for level in v:
            if level not in valid_levels:
                raise ValueError(f"Invalid impact level: {level}")
        return v

    @field_validator("notification_minutes")
    @classmethod
    def validate_notification_minutes(cls, v):
        """Validate notification minutes."""
        if v not in [15, 30, 60]:
            raise ValueError("Notification minutes must be 15, 30, or 60")
        return v

    @field_validator("chart_window_hours")
    @classmethod
    def validate_chart_window_hours(cls, v):
        """Validate chart window hours."""
        if not 1 <= v <= 24:
            raise ValueError("Chart window must be between 1 and 24 hours")
        return v


class UserBase(BaseModel):
    """Base user model."""

    telegram_id: int = Field(description="Telegram user ID")
    username: Optional[str] = Field(default=None, description="Telegram username")
    first_name: Optional[str] = Field(default=None, description="First name")
    last_name: Optional[str] = Field(default=None, description="Last name")
    language_code: Optional[str] = Field(default=None, description="Language code")
    is_bot: bool = Field(default=False, description="Is bot user")
    is_premium: bool = Field(default=False, description="Is premium user")


class UserCreate(UserBase):
    """User creation model."""

    preferences: UserPreferences = Field(default_factory=UserPreferences)


class UserUpdate(BaseModel):
    """User update model."""

    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language_code: Optional[str] = None
    is_premium: Optional[bool] = None
    preferences: Optional[UserPreferences] = None


class User(UserBase):
    """User model with preferences."""

    id: int
    created_at: datetime
    updated_at: datetime
    last_active: Optional[datetime] = None
    is_active: bool = True

    # Individual preference fields (matching SQLAlchemy model)
    preferred_currencies: List[str] = Field(default=[], description="Preferred currencies")
    impact_levels: List[str] = Field(default=["high", "medium"], description="Impact levels to follow")
    analysis_required: bool = Field(default=True, description="Require AI analysis")
    digest_time: str = Field(default="08:00:00", description="Daily digest time")
    timezone: str = Field(default="Europe/Prague", description="User timezone")

    # Notification settings
    notifications_enabled: bool = Field(default=False, description="Enable notifications")
    notification_minutes: int = Field(default=30, description="Notification minutes before event")
    notification_impact_levels: List[str] = Field(default=["high"], description="Notification impact levels")

    # Chart settings
    charts_enabled: bool = Field(default=False, description="Enable charts")
    chart_type: str = Field(default="single", description="Chart type")
    chart_window_hours: int = Field(default=2, description="Chart window hours")

    class Config:
        from_attributes = True


class UserResponse(User):
    """User response model."""

    pass
