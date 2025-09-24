"""Notification-related Pydantic models."""

from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator


class NotificationBase(BaseModel):
    """Base notification model."""

    user_id: int = Field(description="User ID")
    event_id: Optional[int] = Field(default=None, description="Event ID")
    notification_type: str = Field(description="Notification type")
    message: str = Field(description="Notification message")
    scheduled_time: datetime = Field(description="Scheduled time")

    @field_validator("notification_type")
    @classmethod
    def validate_notification_type(cls, v):
        """Validate notification type."""
        valid_types = {"event_reminder", "digest", "system", "alert"}
        if v not in valid_types:
            raise ValueError(f"Invalid notification type: {v}")
        return v


class NotificationCreate(NotificationBase):
    """Notification creation model."""

    pass


class NotificationUpdate(BaseModel):
    """Notification update model."""

    status: Optional[str] = None
    sent_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: Optional[int] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        """Validate notification status."""
        valid_statuses = {"pending", "sent", "failed", "cancelled"}
        if v not in valid_statuses:
            raise ValueError(f"Invalid notification status: {v}")
        return v


class Notification(NotificationBase):
    """Notification model."""

    id: int
    status: str = Field(default="pending")
    sent_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: int = Field(default=0)
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class NotificationResponse(Notification):
    """Notification response model."""

    pass
