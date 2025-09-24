"""Chart-related Pydantic models."""

from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator


class OHLCData(BaseModel):
    """OHLC data model."""

    timestamp: datetime = Field(description="Data timestamp")
    open: float = Field(description="Open price", gt=0)
    high: float = Field(description="High price", gt=0)
    low: float = Field(description="Low price", gt=0)
    close: float = Field(description="Close price", gt=0)
    volume: Optional[float] = Field(default=None, description="Volume")

    @field_validator("high")
    @classmethod
    def validate_high(cls, v, info):
        """Validate high price."""
        if "low" in info.data and v < info.data["low"]:
            raise ValueError("High price must be >= low price")
        return v

    @field_validator("close")
    @classmethod
    def validate_close(cls, v, info):
        """Validate close price."""
        if "low" in info.data and v < info.data["low"]:
            raise ValueError("Close price must be >= low price")
        if "high" in info.data and v > info.data["high"]:
            raise ValueError("Close price must be <= high price")
        return v


class ChartData(BaseModel):
    """Chart data model."""

    currency: str = Field(description="Currency pair")
    data: List[OHLCData] = Field(description="OHLC data points")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Chart metadata")


class ChartRequest(BaseModel):
    """Chart generation request model."""

    currency: str = Field(description="Currency pair")
    event_time: datetime = Field(description="Event time")
    event_name: str = Field(description="Event name")
    impact_level: str = Field(description="Impact level")
    window_hours: int = Field(description="Time window in hours", ge=1, le=24)
    chart_type: str = Field(default="single", description="Chart type")

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

    @field_validator("chart_type")
    @classmethod
    def validate_chart_type(cls, v):
        """Validate chart type."""
        valid_types = {"single", "multi"}
        if v not in valid_types:
            raise ValueError(f"Invalid chart type: {v}")
        return v


class ChartResponse(BaseModel):
    """Chart generation response model."""

    success: bool = Field(description="Generation success")
    chart_data: Optional[ChartData] = Field(default=None, description="Chart data")
    chart_image: Optional[bytes] = Field(default=None, description="Chart image bytes")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Response metadata")
    error_message: Optional[str] = Field(default=None, description="Error message if failed")
