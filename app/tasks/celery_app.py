"""Celery application configuration for background tasks."""

from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Create Celery app
celery_app = Celery(
    "forex_bot",
    broker=settings.redis.url or "redis://localhost:6379/0",
    backend=settings.redis.url or "redis://localhost:6379/0",
    include=[
        "app.tasks.notification_tasks",
        "app.tasks.scraping_tasks",
        "app.tasks.chart_tasks"
    ]
)

# Celery configuration
celery_app.conf.update(
    # Task routing
    task_routes={
        "app.tasks.notification_tasks.*": {"queue": "notifications"},
        "app.tasks.scraping_tasks.*": {"queue": "scraping"},
        "app.tasks.chart_tasks.*": {"queue": "charts"},
    },

    # Queue configuration
    task_default_queue="default",
    task_queues=(
        Queue("default", routing_key="default"),
        Queue("notifications", routing_key="notifications"),
        Queue("scraping", routing_key="scraping"),
        Queue("charts", routing_key="charts"),
    ),

    # Task execution
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Task time limits
    task_soft_time_limit=300,  # 5 minutes
    task_time_limit=600,       # 10 minutes

    # Retry configuration
    task_acks_late=True,
    worker_prefetch_multiplier=1,

    # Result backend
    result_expires=3600,  # 1 hour

    # Monitoring
    worker_send_task_events=True,
    task_send_sent_event=True,
)

# Periodic tasks (Celery Beat)
celery_app.conf.beat_schedule = {
    # Daily forex news scraping at 6 AM UTC
    "scrape-daily-forex-news": {
        "task": "app.tasks.scraping_tasks.scrape_daily_forex_news",
        "schedule": crontab(hour=6, minute=0),
        "options": {"queue": "scraping"}
    },

    # Send daily digest at 8 AM UTC
    "send-daily-digest": {
        "task": "app.tasks.notification_tasks.send_daily_digest",
        "schedule": crontab(hour=8, minute=0),
        "options": {"queue": "notifications"}
    },

    # Clean up old notifications every hour
    "cleanup-old-notifications": {
        "task": "app.tasks.notification_tasks.cleanup_old_notifications",
        "schedule": crontab(minute=0),
        "options": {"queue": "notifications"}
    },

    # Generate charts for upcoming events every 30 minutes
    "generate-upcoming-charts": {
        "task": "app.tasks.chart_tasks.generate_upcoming_charts",
        "schedule": crontab(minute="*/30"),
        "options": {"queue": "charts"}
    },

    # Health check every 5 minutes
    "health-check": {
        "task": "app.tasks.notification_tasks.health_check",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "default"}
    },
}

# Task error handling
@celery_app.task(bind=True)
def debug_task(self):
    """Debug task for testing Celery setup."""
    logger.info(f"Request: {self.request!r}")
    return "Celery is working!"


# Startup event
@celery_app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    """Setup periodic tasks after Celery configuration."""
    logger.info("Celery periodic tasks configured")

    # Add any additional periodic tasks here
    # sender.add_periodic_task(
    #     crontab(hour=0, minute=0),
    #     cleanup_old_data.s(),
    #     name='cleanup-old-data'
    # )


if __name__ == "__main__":
    celery_app.start()
