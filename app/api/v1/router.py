"""Main API router for version 1."""

from fastapi import APIRouter

from .endpoints import users, forex_news, charts, notifications, telegram, health, redis

# Create main API router
api_router = APIRouter()

# Include endpoint routers
api_router.include_router(health.router, tags=["health"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(forex_news.router, prefix="/forex-news", tags=["forex-news"])
api_router.include_router(charts.router, prefix="/charts", tags=["charts"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(telegram.router, prefix="/telegram", tags=["telegram"])
api_router.include_router(redis.router, prefix="/redis", tags=["redis"])
