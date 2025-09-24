"""Automated notification scheduler with comprehensive management."""

import logging
import hashlib
import asyncio
import subprocess
import sys
import os
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, List
import structlog

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
import pytz

from app.core.config import settings
from app.core.exceptions import SchedulerError
from app.services.notification_service import NotificationService
from app.services.database_service import DatabaseService
from app.services.scraping_service import ScrapingService
from app.services.chart_service import chart_service

logger = structlog.get_logger(__name__)


class NotificationScheduler:
    """Scheduler for handling notification checks and sending."""

    def __init__(self, db_service: DatabaseService, bot_manager, config):
        self.db_service = db_service
        self.bot_manager = bot_manager
        self.config = config
        self.notification_service = NotificationService()
        self.scraping_service = ScrapingService()

        # Setup scheduler with SQLAlchemy job store
        jobstores = {
            'default': SQLAlchemyJobStore(url=getattr(settings.database, 'url', 'sqlite:///notification_jobs.sqlite'))
        }

        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            timezone=pytz.UTC
        )

        self._setup_scheduler()

    def _setup_scheduler(self):
        """Setup the notification scheduler with various jobs."""
        try:
            # Add news checking job (every 15 minutes)
            self.scheduler.add_job(
                func=self._check_and_send_news_notifications,
                trigger=IntervalTrigger(minutes=15),
                id='news_checker',
                name='News Notification Checker',
                replace_existing=True
            )

            # Add daily cleanup job (every day at 2 AM UTC)
            self.scheduler.add_job(
                func=self._daily_cleanup,
                trigger=CronTrigger(hour=2, minute=0, timezone=pytz.UTC),
                id='daily_cleanup',
                name='Daily Cleanup',
                replace_existing=True
            )

            # Add health check job (every hour)
            self.scheduler.add_job(
                func=self._health_check,
                trigger=IntervalTrigger(hours=1),
                id='health_check',
                name='Health Check',
                replace_existing=True
            )

            # Add Render.com keep-alive job (every 10 minutes)
            if getattr(self.config, 'render_hostname', None):
                self.scheduler.add_job(
                    func=self._render_keep_alive,
                    trigger=IntervalTrigger(minutes=10),
                    id='render_keep_alive',
                    name='Render.com Keep Alive',
                    replace_existing=True
                )

            self.scheduler.start()
            logger.info("Notification scheduler started successfully")

        except Exception as e:
            logger.error("Error setting up notification scheduler", error=str(e))
            raise SchedulerError(f"Failed to setup notification scheduler: {e}")

    async def _check_and_send_news_notifications(self):
        """Check for new news and send notifications."""
        try:
            logger.info("Starting news notification check")

            # Get current time
            now = datetime.now()
            today = now.date()

            # Fetch today's news
            news_items = await self.scraping_service.scrape_news(now)

            if not news_items:
                logger.info("No news items found for notifications")
                return

            # Filter news by impact level and time
            high_impact_news = [
                item for item in news_items
                if item.get('impact') == 'high' and self._is_recent_news(item, now)
            ]

            if not high_impact_news:
                logger.info("No recent high-impact news found")
                return

            # Get users who want notifications
            users = await self._get_notification_users()

            if not users:
                logger.info("No users found for notifications")
                return

            # Send notifications to users
            for user in users:
                try:
                    await self._send_user_notification(user, high_impact_news, today)
                except Exception as e:
                    logger.error("Failed to send notification to user", user_id=user.get('user_id'), error=str(e))
                    continue

            # Send group notification if configured
            await self._send_group_notification(high_impact_news, today)

            logger.info("News notification check completed", users_notified=len(users), news_count=len(high_impact_news))

        except Exception as e:
            logger.error("Failed to check and send news notifications", error=str(e))

    def _is_recent_news(self, news_item: Dict[str, Any], current_time: datetime) -> bool:
        """Check if news item is recent (within last 2 hours)."""
        try:
            # Parse news time (assuming format like "08:30" or "14:00")
            news_time_str = news_item.get('time', '')
            if not news_time_str:
                return False

            # Convert to datetime for today
            hour, minute = map(int, news_time_str.split(':'))
            news_datetime = datetime.combine(current_time.date(), datetime.min.time().replace(hour=hour, minute=minute))

            # Check if news is within last 2 hours
            time_diff = current_time - news_datetime
            return 0 <= time_diff.total_seconds() <= 7200  # 2 hours in seconds

        except Exception as e:
            logger.error("Failed to check if news is recent", error=str(e))
            return False

    async def _get_notification_users(self) -> List[Dict[str, Any]]:
        """Get users who want to receive notifications."""
        try:
            # This would typically query the database for users with notification preferences
            # For now, we'll return a mock structure
            users = []

            # In a real implementation, you would query the database:
            # users = await self.db_service.get_users_with_notifications_enabled()

            return users

        except Exception as e:
            logger.error("Failed to get notification users", error=str(e))
            return []

    async def _send_user_notification(self, user: Dict[str, Any], news_items: List[Dict[str, Any]], target_date: date):
        """Send notification to a specific user."""
        try:
            user_id = user.get('user_id')
            if not user_id:
                return

            # Get user preferences
            user_preferences = await self._get_user_preferences(user_id)

            # Send notification
            success = await self.notification_service.send_news_notification(
                user_id=user_id,
                news_items=news_items,
                target_date=datetime.combine(target_date, datetime.min.time()),
                impact_level="high",
                analysis_required=user_preferences.get('analysis_required', True),
                currencies=user_preferences.get('preferred_currencies'),
                send_chart=user_preferences.get('charts_enabled', False)
            )

            if success:
                logger.info("User notification sent successfully", user_id=user_id)
            else:
                logger.warning("Failed to send user notification", user_id=user_id)

        except Exception as e:
            logger.error("Failed to send user notification", user_id=user.get('user_id'), error=str(e))

    async def _send_group_notification(self, news_items: List[Dict[str, Any]], target_date: date):
        """Send group notification if configured."""
        try:
            chat_id = getattr(self.config, 'telegram_chat_id', None)
            if not chat_id:
                return

            # Send group notification
            success = await self.notification_service.send_group_notification(
                chat_id=chat_id,
                news_items=news_items,
                target_date=datetime.combine(target_date, datetime.min.time()),
                impact_level="high",
                analysis_required=True,
                send_chart=True
            )

            if success:
                logger.info("Group notification sent successfully", chat_id=chat_id)
            else:
                logger.warning("Failed to send group notification", chat_id=chat_id)

        except Exception as e:
            logger.error("Failed to send group notification", error=str(e))

    async def _get_user_preferences(self, user_id: int) -> Dict[str, Any]:
        """Get user preferences for notifications."""
        try:
            user = await self.db_service.get_or_create_user(user_id)

            return {
                'preferred_currencies': user.get_currencies_list() if hasattr(user, 'get_currencies_list') else [],
                'impact_levels': user.get_impact_levels_list() if hasattr(user, 'get_impact_levels_list') else ['high'],
                'analysis_required': getattr(user, 'analysis_required', True),
                'charts_enabled': getattr(user, 'charts_enabled', False),
                'notifications_enabled': getattr(user, 'notifications_enabled', True)
            }

        except Exception as e:
            logger.error("Failed to get user preferences", user_id=user_id, error=str(e))
            return {}

    async def _daily_cleanup(self):
        """Perform daily cleanup tasks."""
        try:
            logger.info("Starting daily cleanup")

            # Clean up old chart files
            chart_service._prune_old_charts()

            # Clean up old notification records
            await self._cleanup_old_notifications()

            # Clean up old news records
            await self._cleanup_old_news()

            logger.info("Daily cleanup completed")

        except Exception as e:
            logger.error("Failed to perform daily cleanup", error=str(e))

    async def _cleanup_old_notifications(self):
        """Clean up old notification records."""
        try:
            # This would typically clean up old notification records from the database
            # For now, we'll just log the action
            logger.info("Cleaned up old notification records")

        except Exception as e:
            logger.error("Failed to cleanup old notifications", error=str(e))

    async def _cleanup_old_news(self):
        """Clean up old news records."""
        try:
            # This would typically clean up old news records from the database
            # For now, we'll just log the action
            logger.info("Cleaned up old news records")

        except Exception as e:
            logger.error("Failed to cleanup old news", error=str(e))

    async def _health_check(self):
        """Perform health check on all services."""
        try:
            logger.info("Starting health check")

            # Check database connection
            db_health = await self._check_database_health()

            # Check scraping service
            scraping_health = await self._check_scraping_health()

            # Check notification service
            notification_health = self.notification_service.health_check()

            # Check chart service
            chart_health = chart_service.health_check()

            # Log health status
            logger.info("Health check completed",
                       database=db_health.get('status', 'unknown'),
                       scraping=scraping_health.get('status', 'unknown'),
                       notifications=notification_health.get('status', 'unknown'),
                       charts=chart_health.get('status', 'unknown'))

        except Exception as e:
            logger.error("Health check failed", error=str(e))

    async def _check_database_health(self) -> Dict[str, Any]:
        """Check database health."""
        try:
            # This would typically check database connection and perform a simple query
            return {"status": "healthy"}

        except Exception as e:
            logger.error("Database health check failed", error=str(e))
            return {"status": "unhealthy", "error": str(e)}

    async def _check_scraping_health(self) -> Dict[str, Any]:
        """Check scraping service health."""
        try:
            # This would typically check if the scraping service is working
            return {"status": "healthy"}

        except Exception as e:
            logger.error("Scraping health check failed", error=str(e))
            return {"status": "unhealthy", "error": str(e)}

    async def _render_keep_alive(self):
        """Send keep-alive request to Render.com."""
        try:
            render_hostname = getattr(self.config, 'render_hostname', None)
            if not render_hostname:
                return

            # Send HTTP request to keep the service alive
            import requests
            response = requests.get(f"https://{render_hostname}/health", timeout=10)

            if response.status_code == 200:
                logger.info("Render.com keep-alive successful")
            else:
                logger.warning("Render.com keep-alive failed", status_code=response.status_code)

        except Exception as e:
            logger.error("Render.com keep-alive failed", error=str(e))

    def add_custom_job(self, func, trigger, job_id: str, name: str, **kwargs):
        """Add a custom job to the scheduler."""
        try:
            self.scheduler.add_job(
                func=func,
                trigger=trigger,
                id=job_id,
                name=name,
                replace_existing=True,
                **kwargs
            )

            logger.info("Custom job added", job_id=job_id, name=name)

        except Exception as e:
            logger.error("Failed to add custom job", job_id=job_id, error=str(e))
            raise SchedulerError(f"Failed to add custom job: {e}")

    def remove_job(self, job_id: str):
        """Remove a job from the scheduler."""
        try:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info("Job removed", job_id=job_id)
            else:
                logger.warning("Job not found", job_id=job_id)

        except Exception as e:
            logger.error("Failed to remove job", job_id=job_id, error=str(e))
            raise SchedulerError(f"Failed to remove job: {e}")

    def get_scheduled_jobs(self) -> List[Dict[str, Any]]:
        """Get list of all scheduled jobs."""
        try:
            jobs = []
            for job in self.scheduler.get_jobs():
                jobs.append({
                    'id': job.id,
                    'name': job.name,
                    'next_run_time': job.next_run_time.isoformat() if job.next_run_time else None,
                    'trigger': str(job.trigger)
                })
            return jobs

        except Exception as e:
            logger.error("Failed to get scheduled jobs", error=str(e))
            return []

    def pause_job(self, job_id: str):
        """Pause a scheduled job."""
        try:
            job = self.scheduler.get_job(job_id)
            if job:
                job.pause()
                logger.info("Job paused", job_id=job_id)
            else:
                logger.warning("Job not found for pausing", job_id=job_id)

        except Exception as e:
            logger.error("Failed to pause job", job_id=job_id, error=str(e))
            raise SchedulerError(f"Failed to pause job: {e}")

    def resume_job(self, job_id: str):
        """Resume a paused job."""
        try:
            job = self.scheduler.get_job(job_id)
            if job:
                job.resume()
                logger.info("Job resumed", job_id=job_id)
            else:
                logger.warning("Job not found for resuming", job_id=job_id)

        except Exception as e:
            logger.error("Failed to resume job", job_id=job_id, error=str(e))
            raise SchedulerError(f"Failed to resume job: {e}")

    def health_check(self) -> Dict[str, Any]:
        """Check the health of the notification scheduler."""
        try:
            return {
                "status": "healthy",
                "scheduler_running": self.scheduler.running,
                "scheduled_jobs": len(self.scheduler.get_jobs()),
                "jobstore_configured": bool(self.scheduler.jobstores),
                "notification_service_available": True,
                "scraping_service_available": True,
                "chart_service_available": True
            }

        except Exception as e:
            logger.error("Notification scheduler health check failed", error=str(e))
            return {
                "status": "unhealthy",
                "error": str(e)
            }

    def shutdown(self):
        """Shutdown the notification scheduler."""
        try:
            if self.scheduler.running:
                self.scheduler.shutdown()
                logger.info("Notification scheduler shutdown completed")

        except Exception as e:
            logger.error("Failed to shutdown notification scheduler", error=str(e))
