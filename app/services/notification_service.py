"""Advanced notification service with deduplication, rate limiting, and comprehensive functionality."""

import logging
import hashlib
import asyncio
import threading
import html
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Set
from io import BytesIO
import structlog

from app.core.config import settings
from app.core.exceptions import NotificationError
from app.services.database_service import DatabaseService
from app.services.chart_service import chart_service
from app.services.telegram_service import TelegramService

logger = structlog.get_logger(__name__)


class NotificationDeduplicationService:
    """Service to handle notification deduplication and tracking."""

    def __init__(self):
        self.sent_notifications: Dict[str, datetime] = {}
        # Track group notifications with timestamps for cleanup
        self.group_notifications: Dict[str, datetime] = {}
        # Track last chart sent per target (user/channel) to rate-limit charts
        self.last_chart_sent: Dict[str, datetime] = {}
        self.cleanup_interval = timedelta(hours=24)  # Clean up old notifications every 24 hours
        self.last_cleanup = datetime.now()
        self._lock = threading.Lock()

    def _generate_notification_id(self, event_type: str, **kwargs) -> str:
        """Generate a unique ID for a notification based on its parameters."""
        # Create a string representation of the notification parameters
        params_str = f"{event_type}:{':'.join(f'{k}={v}' for k, v in sorted(kwargs.items()))}"
        return hashlib.md5(params_str.encode()).hexdigest()

    def _cleanup_old_notifications(self):
        """Remove notifications older than the cleanup interval."""
        now = datetime.now()
        if now - self.last_cleanup > self.cleanup_interval:
            cutoff_time = now - self.cleanup_interval
            old_notifications = [
                notification_id for notification_id, timestamp in self.sent_notifications.items()
                if timestamp < cutoff_time
            ]
            for notification_id in old_notifications:
                del self.sent_notifications[notification_id]

            # Clean up group notifications
            old_group_notifications = [
                notification_id for notification_id, timestamp in self.group_notifications.items()
                if timestamp < cutoff_time
            ]
            for notification_id in old_group_notifications:
                del self.group_notifications[notification_id]

            # Clean up chart timestamps
            old_chart_timestamps = [
                target for target, timestamp in self.last_chart_sent.items()
                if timestamp < cutoff_time
            ]
            for target in old_chart_timestamps:
                del self.last_chart_sent[target]

            self.last_cleanup = now
            logger.info("Cleaned up old notifications",
                       notifications=len(old_notifications),
                       group_notifications=len(old_group_notifications),
                       chart_timestamps=len(old_chart_timestamps))

    def is_notification_sent(self, event_type: str, **kwargs) -> bool:
        """Check if a notification has already been sent."""
        with self._lock:
            self._cleanup_old_notifications()
            notification_id = self._generate_notification_id(event_type, **kwargs)
            return notification_id in self.sent_notifications

    def mark_notification_sent(self, event_type: str, **kwargs):
        """Mark a notification as sent."""
        with self._lock:
            notification_id = self._generate_notification_id(event_type, **kwargs)
            self.sent_notifications[notification_id] = datetime.now()

    def is_group_notification_sent(self, group_key: str) -> bool:
        """Check if a group notification has already been sent."""
        with self._lock:
            self._cleanup_old_notifications()
            return group_key in self.group_notifications

    def mark_group_notification_sent(self, group_key: str):
        """Mark a group notification as sent."""
        with self._lock:
            self.group_notifications[group_key] = datetime.now()

    def can_send_chart(self, target: str, min_interval_minutes: int = 5) -> bool:
        """Check if enough time has passed since the last chart was sent to this target."""
        with self._lock:
            if target not in self.last_chart_sent:
                return True

            last_sent = self.last_chart_sent[target]
            time_since_last = datetime.now() - last_sent
            return time_since_last >= timedelta(minutes=min_interval_minutes)

    def mark_chart_sent(self, target: str):
        """Mark that a chart was sent to this target."""
        with self._lock:
            self.last_chart_sent[target] = datetime.now()


# Global deduplication service instance
notification_deduplication = NotificationDeduplicationService()


class NotificationService:
    """Advanced notification service with comprehensive functionality."""

    def __init__(self):
        self.db_service = DatabaseService()
        self.telegram_service = TelegramService()
        self.deduplication = notification_deduplication

    async def send_news_notification(
        self,
        user_id: int,
        news_items: List[Dict[str, Any]],
        target_date: datetime,
        impact_level: str = "high",
        analysis_required: bool = True,
        currencies: Optional[List[str]] = None,
        send_chart: bool = False
    ) -> bool:
        """Send news notification to a user with deduplication."""
        try:
            # Check if notification was already sent
            notification_key = f"news_{user_id}_{target_date.date()}_{impact_level}"
            if self.deduplication.is_notification_sent("news_notification",
                                                      user_id=user_id,
                                                      date=target_date.date(),
                                                      impact_level=impact_level):
                logger.info("News notification already sent", user_id=user_id, date=target_date.date())
                return True

            # Format news message
            message = await self.telegram_service.format_news_message(
                news_items, target_date, impact_level, analysis_required, currencies
            )

            # Send message
            success = await self.telegram_service.send_long_message(user_id, message)

            if success:
                # Mark notification as sent
                self.deduplication.mark_notification_sent("news_notification",
                                                        user_id=user_id,
                                                        date=target_date.date(),
                                                        impact_level=impact_level)

                # Send chart if requested and rate limit allows
                if send_chart and self.deduplication.can_send_chart(str(user_id)):
                    await self._send_news_chart(user_id, news_items, target_date)

                logger.info("News notification sent successfully", user_id=user_id)
                return True
            else:
                logger.error("Failed to send news notification", user_id=user_id)
                return False

        except Exception as e:
            logger.error("Failed to send news notification", user_id=user_id, error=str(e), exc_info=True)
            return False

    async def send_group_notification(
        self,
        chat_id: int,
        news_items: List[Dict[str, Any]],
        target_date: datetime,
        impact_level: str = "high",
        analysis_required: bool = True,
        currencies: Optional[List[str]] = None,
        send_chart: bool = False
    ) -> bool:
        """Send group notification with deduplication."""
        try:
            # Check if group notification was already sent
            group_key = f"group_{chat_id}_{target_date.date()}_{impact_level}"
            if self.deduplication.is_group_notification_sent(group_key):
                logger.info("Group notification already sent", chat_id=chat_id, date=target_date.date())
                return True

            # Format news message
            message = await self.telegram_service.format_news_message(
                news_items, target_date, impact_level, analysis_required, currencies
            )

            # Send message
            success = await self.telegram_service.send_long_message(chat_id, message)

            if success:
                # Mark group notification as sent
                self.deduplication.mark_group_notification_sent(group_key)

                # Send chart if requested and rate limit allows
                if send_chart and self.deduplication.can_send_chart(str(chat_id)):
                    await self._send_news_chart(chat_id, news_items, target_date)

                logger.info("Group notification sent successfully", chat_id=chat_id)
                return True
            else:
                logger.error("Failed to send group notification", chat_id=chat_id)
                return False

        except Exception as e:
            logger.error("Failed to send group notification", chat_id=chat_id, error=str(e), exc_info=True)
            return False

    async def _send_news_chart(
        self,
        target_id: int,
        news_items: List[Dict[str, Any]],
        target_date: datetime
    ) -> bool:
        """Send chart for news items."""
        try:
            # Get the first high-impact news item for chart generation
            high_impact_items = [item for item in news_items if item.get('impact') == 'high']
            if not high_impact_items:
                return False

            item = high_impact_items[0]
            currency = item.get('currency', 'USD')
            event_name = item.get('event', 'News Event')

            # Generate chart
            chart_image = await chart_service.create_event_chart(
                currency=currency,
                event_time=target_date,
                event_name=event_name,
                window_hours=2
            )

            if chart_image:
                # Send chart
                success = await self.telegram_service.bot_manager.send_photo(
                    chat_id=target_id,
                    photo=chart_image,
                    caption=f"📊 Chart for {currency} - {event_name}"
                )

                if success:
                    # Mark chart as sent
                    self.deduplication.mark_chart_sent(str(target_id))
                    logger.info("News chart sent successfully", target_id=target_id, currency=currency)
                    return True

            return False

        except Exception as e:
            logger.error("Failed to send news chart", target_id=target_id, error=str(e), exc_info=True)
            return False

    async def send_daily_digest(
        self,
        user_id: int,
        news_items: List[Dict[str, Any]],
        target_date: datetime,
        user_preferences: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Send daily digest to a user."""
        try:
            # Check if digest was already sent
            digest_key = f"digest_{user_id}_{target_date.date()}"
            if self.deduplication.is_notification_sent("daily_digest",
                                                    user_id=user_id,
                                                    date=target_date.date()):
                logger.info("Daily digest already sent", user_id=user_id, date=target_date.date())
                return True

            # Format digest message
            message = await self._format_digest_message(news_items, target_date, user_preferences)

            # Send message
            success = await self.telegram_service.send_long_message(user_id, message)

            if success:
                # Mark digest as sent
                self.deduplication.mark_notification_sent("daily_digest",
                                                        user_id=user_id,
                                                        date=target_date.date())

                logger.info("Daily digest sent successfully", user_id=user_id)
                return True
            else:
                logger.error("Failed to send daily digest", user_id=user_id)
                return False

        except Exception as e:
            logger.error("Failed to send daily digest", user_id=user_id, error=str(e), exc_info=True)
            return False

    async def _format_digest_message(
        self,
        news_items: List[Dict[str, Any]],
        target_date: datetime,
        user_preferences: Optional[Dict[str, Any]] = None
    ) -> str:
        """Format daily digest message."""
        date_str = target_date.strftime("%d.%m.%Y")

        # Filter by user preferences
        if user_preferences:
            preferred_currencies = user_preferences.get('preferred_currencies', [])
            impact_levels = user_preferences.get('impact_levels', ['high', 'medium', 'low'])

            if preferred_currencies:
                news_items = [item for item in news_items if item.get('currency') in preferred_currencies]

            if impact_levels != ['high', 'medium', 'low']:
                news_items = [item for item in news_items if item.get('impact') in impact_levels]

        if not news_items:
            return f"📅 Daily Digest for {date_str}\n\n✅ No news events for your preferences today."

        # Group by currency
        grouped = {}
        for item in news_items:
            currency = item.get('currency', 'Unknown')
            if currency not in grouped:
                grouped[currency] = []
            grouped[currency].append(item)

        # Format message
        message_parts = [f"📅 Daily Digest for {date_str}\n"]

        for currency, items in sorted(grouped.items()):
            message_parts.append(f"\n💎 {currency}")

            for item in items:
                impact_emoji = {
                    'high': '🔴',
                    'medium': '🟠',
                    'low': '🟡',
                    'tentative': '⏳',
                    'none': '⚪️',
                    'unknown': '❓',
                }.get(item.get('impact', 'unknown'), '❓')

                event = str(item.get('event', 'N/A')).replace('\\', '')
                time_str = item.get('time', 'N/A')

                message_parts.append(f"⏰ {time_str} {impact_emoji} {event}")

        return "".join(message_parts)

    async def send_error_notification(
        self,
        user_id: int,
        error_message: str,
        error_type: str = "general"
    ) -> bool:
        """Send error notification to a user."""
        try:
            # Check if error notification was already sent (rate limit)
            error_key = f"error_{user_id}_{error_type}"
            if self.deduplication.is_notification_sent("error_notification",
                                                    user_id=user_id,
                                                    error_type=error_type):
                logger.info("Error notification already sent recently", user_id=user_id, error_type=error_type)
                return True

            message = f"⚠️ Error Notification\n\n{error_message}\n\nPlease try again later or contact support."

            success = await self.telegram_service.bot_manager.send_message(user_id, message)

            if success:
                self.deduplication.mark_notification_sent("error_notification",
                                                        user_id=user_id,
                                                        error_type=error_type)
                logger.info("Error notification sent", user_id=user_id)
                return True

            return False

        except Exception as e:
            logger.error("Failed to send error notification", user_id=user_id, error=str(e), exc_info=True)
            return False

    async def send_system_notification(
        self,
        user_id: int,
        message: str,
        notification_type: str = "system"
    ) -> bool:
        """Send system notification to a user."""
        try:
            # Check if system notification was already sent
            if self.deduplication.is_notification_sent("system_notification",
                                                      user_id=user_id,
                                                      message_hash=hashlib.md5(message.encode()).hexdigest()):
                logger.info("System notification already sent", user_id=user_id)
                return True

            formatted_message = f"🔔 System Notification\n\n{message}"

            success = await self.telegram_service.send_long_message(user_id, formatted_message)

            if success:
                self.deduplication.mark_notification_sent("system_notification",
                                                        user_id=user_id,
                                                        message_hash=hashlib.md5(message.encode()).hexdigest())
                logger.info("System notification sent", user_id=user_id)
                return True

            return False

        except Exception as e:
            logger.error("Failed to send system notification", user_id=user_id, error=str(e), exc_info=True)
            return False

    async def get_notification_stats(self) -> Dict[str, Any]:
        """Get notification statistics."""
        try:
            with self.deduplication._lock:
                return {
                    "sent_notifications": len(self.deduplication.sent_notifications),
                    "group_notifications": len(self.deduplication.group_notifications),
                    "chart_timestamps": len(self.deduplication.last_chart_sent),
                    "last_cleanup": self.deduplication.last_cleanup.isoformat(),
                    "cleanup_interval_hours": self.deduplication.cleanup_interval.total_seconds() / 3600
                }
        except Exception as e:
            logger.error("Failed to get notification stats", error=str(e))
            return {"error": str(e)}

    def health_check(self) -> Dict[str, Any]:
        """Check the health of the notification service."""
        try:
            return {
                "status": "healthy",
                "deduplication_active": True,
                "telegram_service_available": True,
                "chart_service_available": True,
                "stats": asyncio.run(self.get_notification_stats())
            }
        except Exception as e:
            logger.error("Notification service health check failed", error=str(e))
            return {
                "status": "unhealthy",
                "error": str(e)
            }
