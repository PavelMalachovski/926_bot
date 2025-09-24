"""SQLAlchemy database models."""

from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, JSON, Float, ForeignKey
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

Base = declarative_base()


class UserModel(Base):
    """User database model."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    language_code = Column(String(10), nullable=True)
    is_bot = Column(Boolean, default=False)
    is_premium = Column(Boolean, default=False)

    # User preferences
    preferred_currencies = Column(JSON, default=list)
    impact_levels = Column(JSON, default=["high", "medium"])
    analysis_required = Column(Boolean, default=True)
    digest_time = Column(String(8), default="08:00:00")  # HH:MM:SS format
    timezone = Column(String(50), default="Europe/Prague")

    # Notification settings
    notifications_enabled = Column(Boolean, default=False)
    notification_minutes = Column(Integer, default=30)
    notification_impact_levels = Column(JSON, default=["high"])

    # Chart settings
    charts_enabled = Column(Boolean, default=False)
    chart_type = Column(String(20), default="single")
    chart_window_hours = Column(Integer, default=2)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_active = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)

    # Relationships
    notifications = relationship("NotificationModel", back_populates="user")


class ForexNewsModel(Base):
    """Forex news database model."""

    __tablename__ = "forex_news"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime(timezone=True), nullable=False, index=True)
    time = Column(String(8), nullable=False)  # HH:MM:SS format
    currency = Column(String(10), nullable=False, index=True)
    event = Column(String(255), nullable=False)
    actual = Column(String(50), nullable=True)
    forecast = Column(String(50), nullable=True)
    previous = Column(String(50), nullable=True)
    impact_level = Column(String(20), nullable=False, index=True)
    analysis = Column(Text, nullable=True)

    # Additional metadata
    source = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    event_type = Column(String(50), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    notifications = relationship("NotificationModel", back_populates="forex_news")


class NotificationModel(Base):
    """Notification database model."""

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("forex_news.id"), nullable=True, index=True)

    notification_type = Column(String(50), nullable=False)  # event_reminder, digest, etc.
    message = Column(Text, nullable=False)
    scheduled_time = Column(DateTime(timezone=True), nullable=False, index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    status = Column(String(20), default="pending", index=True)  # pending, sent, failed
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    # Additional metadata
    extra_data = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("UserModel", back_populates="notifications")
    forex_news = relationship("ForexNewsModel", back_populates="notifications")


class ChartModel(Base):
    """Chart database model."""

    __tablename__ = "charts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("forex_news.id"), nullable=True, index=True)

    currency = Column(String(10), nullable=False, index=True)
    chart_type = Column(String(20), nullable=False)  # single, multi
    window_hours = Column(Integer, nullable=False)

    # Chart data
    chart_data = Column(JSON, nullable=True)  # OHLC data
    chart_image_path = Column(String(500), nullable=True)

    # Metadata
    event_time = Column(DateTime(timezone=True), nullable=True)
    generated_at = Column(DateTime(timezone=True), server_default=func.now())

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class APILogModel(Base):
    """API request logging model."""

    __tablename__ = "api_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    method = Column(String(10), nullable=False)
    path = Column(String(500), nullable=False)
    status_code = Column(Integer, nullable=False, index=True)
    response_time = Column(Float, nullable=True)  # in seconds

    # Request/Response data
    request_data = Column(JSON, nullable=True)
    response_data = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    # Client info
    ip_address = Column(String(45), nullable=True)  # IPv6 compatible
    user_agent = Column(String(500), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
