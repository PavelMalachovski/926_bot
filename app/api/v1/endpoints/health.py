"""Health check endpoints for monitoring and production readiness."""

from datetime import datetime
from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.database.connection import get_database
from app.core.config import settings
from app.core.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/health", tags=["health"])
async def health_check() -> Dict[str, Any]:
    """
    Basic health check endpoint.

    Returns:
        Dict containing health status and timestamp
    """
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": settings.app_version,
        "environment": settings.environment
    }


@router.get("/health/detailed", tags=["health"])
async def detailed_health_check(db: AsyncSession = Depends(get_database)) -> Dict[str, Any]:
    """
    Detailed health check including database connectivity.

    Args:
        db: Database session dependency

    Returns:
        Dict containing detailed health status

    Raises:
        HTTPException: If any component is unhealthy
    """
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": settings.app_version,
        "environment": settings.environment,
        "components": {}
    }

    # Check database connectivity
    try:
        await db.execute(text("SELECT 1"))
        health_status["components"]["database"] = {
            "status": "healthy",
            "message": "Database connection successful"
        }
    except Exception as e:
        logger.error("Database health check failed", error=str(e))
        health_status["components"]["database"] = {
            "status": "unhealthy",
            "message": f"Database connection failed: {str(e)}"
        }
        health_status["status"] = "unhealthy"

    # Check Redis connectivity (if configured)
    try:
        if settings.redis.url:
            import redis.asyncio as redis
            redis_client = redis.from_url(settings.redis.url)
            await redis_client.ping()
            await redis_client.close()
            health_status["components"]["redis"] = {
                "status": "healthy",
                "message": "Redis connection successful"
            }
        else:
            health_status["components"]["redis"] = {
                "status": "not_configured",
                "message": "Redis not configured"
            }
    except Exception as e:
        logger.error("Redis health check failed", error=str(e))
        health_status["components"]["redis"] = {
            "status": "unhealthy",
            "message": f"Redis connection failed: {str(e)}"
        }
        # Don't fail overall health check for Redis issues in development
        if settings.environment == "development":
            logger.warning("Redis unavailable in development, continuing with healthy status")
        else:
            health_status["status"] = "unhealthy"

    # Check external APIs (if configured)
    try:
        if settings.api.openai_api_key:
            health_status["components"]["openai"] = {
                "status": "configured",
                "message": "OpenAI API key configured"
            }
        else:
            health_status["components"]["openai"] = {
                "status": "not_configured",
                "message": "OpenAI API key not configured"
            }
    except Exception as e:
        logger.error("OpenAI health check failed", error=str(e))
        health_status["components"]["openai"] = {
            "status": "error",
            "message": f"OpenAI check failed: {str(e)}"
        }

    # Return appropriate status code
    if health_status["status"] == "unhealthy":
        raise HTTPException(status_code=503, detail=health_status)

    return health_status


@router.get("/health/ready", tags=["health"])
async def readiness_check(db: AsyncSession = Depends(get_database)) -> Dict[str, Any]:
    """
    Kubernetes readiness probe endpoint.

    Args:
        db: Database session dependency

    Returns:
        Dict containing readiness status

    Raises:
        HTTPException: If not ready
    """
    try:
        # Check database
        await db.execute(text("SELECT 1"))

        # Check Redis (if configured)
        if settings.redis.url:
            try:
                import redis.asyncio as redis
                redis_client = redis.from_url(settings.redis.url)
                await redis_client.ping()
                await redis_client.close()
            except Exception as redis_error:
                if settings.environment == "development":
                    logger.warning("Redis unavailable in development, continuing readiness check")
                else:
                    raise redis_error

        return {
            "status": "ready",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error("Readiness check failed", error=str(e))
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e)
            }
        )


@router.get("/health/live", tags=["health"])
async def liveness_check() -> Dict[str, Any]:
    """
    Kubernetes liveness probe endpoint.

    Returns:
        Dict containing liveness status
    """
    return {
        "status": "alive",
        "timestamp": datetime.utcnow().isoformat(),
        "uptime": "running"
    }


@router.get("/health/metrics", tags=["health"])
async def metrics_endpoint() -> Dict[str, Any]:
    """
    Basic metrics endpoint for monitoring.

    Returns:
        Dict containing basic application metrics
    """
    try:
        # Import prometheus_client only if available
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

        return {
            "metrics": generate_latest().decode('utf-8'),
            "content_type": CONTENT_TYPE_LATEST
        }
    except ImportError:
        return {
            "status": "metrics_not_available",
            "message": "Prometheus client not installed",
            "timestamp": datetime.utcnow().isoformat()
        }
