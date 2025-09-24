"""Background tasks for notifications."""

from datetime import datetime, timedelta
from typing import List, Dict, Any
from celery import current_task
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.celery_app import celery_app
from app.database.connection import db_manager
from app.services.notification_service import NotificationService
from app.services.user_service import UserService
from app.services.forex_service import ForexService
from app.services.telegram_service import TelegramService
from app.core.logging import get_logger
from app.core.exceptions import NotificationError

logger = get_logger(__name__)


@celery_app.task(bind=True, name="app.tasks.notification_tasks.send_daily_digest")
async def send_daily_digest(self):
    """
    Send daily digest to all users.

    Returns:
        Dict with task results
    """
    task_id = self.request.id
    logger.info("Starting daily digest task", task_id=task_id)

    try:
        # Initialize database connection
        await db_manager.initialize()

        async with db_manager.get_session_async() as db:
            # Get all users with digest enabled
            user_service = UserService()
            users = await user_service.get_all_users(db, limit=1000)

            # Get today's forex news
            forex_service = ForexService()
            today_news = await forex_service.get_todays_forex_news(db)

            # Filter news by user preferences
            digest_results = []
            for user in users:
                if not user.preferences.notifications_enabled:
                    continue

                # Filter news by user's preferred currencies and impact levels
                user_news = [
                    news for news in today_news
                    if news.currency in user.preferences.preferred_currencies
                    and news.impact_level in user.preferences.notification_impact_levels
                ]

                if user_news:
                    # Send digest to user
                    telegram_service = TelegramService()
                    message = _format_daily_digest(user_news, user.preferences.timezone)

                    try:
                        await telegram_service.send_message(
                            chat_id=user.telegram_id,
                            message=message
                        )
                        digest_results.append({
                            "user_id": user.telegram_id,
                            "status": "sent",
                            "news_count": len(user_news)
                        })
                    except Exception as e:
                        logger.error("Failed to send digest", user_id=user.telegram_id, error=str(e))
                        digest_results.append({
                            "user_id": user.telegram_id,
                            "status": "failed",
                            "error": str(e)
                        })

            logger.info("Daily digest task completed",
                       task_id=task_id,
                       users_processed=len(digest_results))

            return {
                "status": "completed",
                "users_processed": len(digest_results),
                "results": digest_results
            }

    except Exception as e:
        logger.error("Daily digest task failed", task_id=task_id, error=str(e))
        raise NotificationError(f"Daily digest task failed: {e}")

    finally:
        await db_manager.close()


@celery_app.task(bind=True, name="app.tasks.notification_tasks.send_event_reminder")
async def send_event_reminder(self, user_id: int, event_id: int, minutes_before: int = 30):
    """
    Send event reminder to specific user.

    Args:
        user_id: User's Telegram ID
        event_id: Forex event ID
        minutes_before: Minutes before event to send reminder

    Returns:
        Dict with task results
    """
    task_id = self.request.id
    logger.info("Starting event reminder task",
                task_id=task_id, user_id=user_id, event_id=event_id)

    try:
        # Initialize database connection
        await db_manager.initialize()

        async with db_manager.get_session_async() as db:
            # Get user and event
            user_service = UserService()
            forex_service = ForexService()

            user = await user_service.get_user_by_telegram_id(db, user_id)
            event = await forex_service.get_forex_news_by_id(db, event_id)

            if not user or not event:
                logger.warning("User or event not found", user_id=user_id, event_id=event_id)
                return {"status": "skipped", "reason": "user_or_event_not_found"}

            # Check if user wants notifications for this event
            if (not user.preferences.notifications_enabled or
                event.currency not in user.preferences.preferred_currencies or
                event.impact_level not in user.preferences.notification_impact_levels):
                logger.info("User preferences don't match event", user_id=user_id, event_id=event_id)
                return {"status": "skipped", "reason": "preferences_dont_match"}

            # Send reminder
            telegram_service = TelegramService()
            message = _format_event_reminder(event, minutes_before, user.preferences.timezone)

            await telegram_service.send_message(
                chat_id=user_id,
                message=message
            )

            logger.info("Event reminder sent", task_id=task_id, user_id=user_id, event_id=event_id)

            return {
                "status": "sent",
                "user_id": user_id,
                "event_id": event_id,
                "minutes_before": minutes_before
            }

    except Exception as e:
        logger.error("Event reminder task failed",
                    task_id=task_id, user_id=user_id, event_id=event_id, error=str(e))
        raise NotificationError(f"Event reminder task failed: {e}")

    finally:
        await db_manager.close()


@celery_app.task(bind=True, name="app.tasks.notification_tasks.cleanup_old_notifications")
async def cleanup_old_notifications(self, days_old: int = 30):
    """
    Clean up old notifications.

    Args:
        days_old: Delete notifications older than this many days

    Returns:
        Dict with cleanup results
    """
    task_id = self.request.id
    logger.info("Starting notification cleanup task", task_id=task_id, days_old=days_old)

    try:
        # Initialize database connection
        await db_manager.initialize()

        async with db_manager.get_session_async() as db:
            notification_service = NotificationService()

            # Get cutoff date
            cutoff_date = datetime.utcnow() - timedelta(days=days_old)

            # Delete old notifications
            deleted_count = await notification_service.cleanup_old_notifications(db, cutoff_date)

            logger.info("Notification cleanup completed",
                       task_id=task_id, deleted_count=deleted_count)

            return {
                "status": "completed",
                "deleted_count": deleted_count,
                "cutoff_date": cutoff_date.isoformat()
            }

    except Exception as e:
        logger.error("Notification cleanup task failed", task_id=task_id, error=str(e))
        raise NotificationError(f"Notification cleanup task failed: {e}")

    finally:
        await db_manager.close()


@celery_app.task(bind=True, name="app.tasks.notification_tasks.health_check")
async def health_check(self):
    """
    Health check task for monitoring.

    Returns:
        Dict with health status
    """
    task_id = self.request.id
    logger.info("Starting health check task", task_id=task_id)

    try:
        # Initialize database connection
        await db_manager.initialize()

        async with db_manager.get_session_async() as db:
            # Test database connection
            from sqlalchemy import text
            await db.execute(text("SELECT 1"))

            # Test Redis connection
            from app.services.cache_service import cache_service
            await cache_service.initialize()
            await cache_service.set("health_check", "ok", ttl=60)
            health_value = await cache_service.get("health_check")

            logger.info("Health check completed", task_id=task_id)

            return {
                "status": "healthy",
                "database": "connected",
                "redis": "connected" if health_value == "ok" else "error",
                "timestamp": datetime.utcnow().isoformat()
            }

    except Exception as e:
        logger.error("Health check task failed", task_id=task_id, error=str(e))
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }

    finally:
        await db_manager.close()


def _format_daily_digest(news_items: List, timezone: str) -> str:
    """Format daily digest message."""
    message = f"📊 Daily Forex Digest ({timezone})\n\n"

    if not news_items:
        message += "No significant events today."
        return message

    # Group by currency
    by_currency = {}
    for news in news_items:
        if news.currency not in by_currency:
            by_currency[news.currency] = []
        by_currency[news.currency].append(news)

    for currency, events in by_currency.items():
        message += f"🇺🇸 {currency} Events:\n"
        for event in events:
            impact_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(event.impact_level, "⚪")
            message += f"{impact_emoji} {event.time} - {event.event}\n"
        message += "\n"

    return message


def _format_event_reminder(event, minutes_before: int, timezone: str) -> str:
    """Format event reminder message."""
    impact_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(event.impact_level, "⚪")

    message = f"⏰ Event Reminder ({timezone})\n\n"
    message += f"{impact_emoji} {event.currency} - {event.event}\n"
    message += f"📅 {event.date} at {event.time}\n"
    message += f"⏱️ Starting in {minutes_before} minutes\n"

    if event.forecast:
        message += f"📈 Forecast: {event.forecast}\n"
    if event.previous:
        message += f"📉 Previous: {event.previous}\n"

    return message
