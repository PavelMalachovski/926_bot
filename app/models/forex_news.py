"""Forex news-related Pydantic models."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class ForexNewsBase(BaseModel):
    """Base forex news model."""

    date: datetime = Field(description="Event date")
    time: str = Field(description="Event time (HH:MM:SS)")
    currency: str = Field(description="Currency code")
    event: str = Field(description="Event name")
    actual: Optional[str] = Field(default=None, description="Actual value")
    forecast: Optional[str] = Field(default=None, description="Forecast value")
    previous: Optional[str] = Field(default=None, description="Previous value")
    impact_level: str = Field(description="Impact level")
    analysis: Optional[str] = Field(default=None, description="AI analysis")

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v):
        """Validate currency code."""
        valid_currencies = {
            "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
            "CNY", "INR", "BRL", "RUB", "KRW", "MXN", "SGD", "HKD",
            "XAU", "BTC", "ETH"
        }
        if v not in valid_currencies:
            raise ValueError(f"Invalid currency: {v}")
        return v

    @field_validator("impact_level")
    @classmethod
    def validate_impact_level(cls, v):
        """Validate impact level."""
        valid_levels = {"high", "medium", "low"}
        if v not in valid_levels:
            raise ValueError(f"Invalid impact level: {v}")
        return v


class ForexNewsCreate(ForexNewsBase):
    """Forex news creation model."""

    pass


class ForexNewsUpdate(BaseModel):
    """Forex news update model."""

    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None
    analysis: Optional[str] = None


class ForexNews(ForexNewsBase):
    """Forex news model."""

    id: int
    source: Optional[str] = None
    country: Optional[str] = None
    event_type: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ForexNewsResponse(ForexNews):
    """Forex news response model."""

    pass
