"""Redis management and monitoring endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status, Query, Body
from fastapi.responses import JSONResponse
from typing import Dict, Any, Optional
import structlog

from app.services.cache_service import cache_service
from app.core.exceptions import CacheError

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/stats", summary="Get Redis Statistics")
async def get_redis_stats():
    """Get comprehensive Redis statistics."""
    try:
        stats = await cache_service.get_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error("Failed to get Redis stats", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve Redis statistics"
        )


@router.get("/health", summary="Redis Health Check")
async def redis_health_check():
    """Check Redis health status."""
    try:
        if not cache_service._initialized:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "reason": "Redis not initialized"}
            )

        # Test connection
        await cache_service.redis_client.ping()

        return JSONResponse(content={
            "status": "healthy",
            "initialized": cache_service._initialized,
            "connection_pool_size": cache_service.connection_pool.size if cache_service.connection_pool else 0
        })
    except Exception as e:
        logger.error("Redis health check failed", error=str(e))
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "reason": str(e)}
        )


@router.post("/cache/invalidate", summary="Invalidate Cache Pattern")
async def invalidate_cache_pattern(pattern: str = Query(..., description="Redis key pattern to invalidate")):
    """Invalidate all cache keys matching a pattern."""
    try:
        count = await cache_service.invalidate_pattern(pattern)
        return JSONResponse(content={
            "message": f"Invalidated {count} keys matching pattern '{pattern}'",
            "pattern": pattern,
            "count": count
        })
    except Exception as e:
        logger.error("Failed to invalidate cache pattern", pattern=pattern, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to invalidate cache pattern: {e}"
        )


@router.get("/cache/hit-ratio", summary="Get Cache Hit Ratio")
async def get_cache_hit_ratio():
    """Get current cache hit ratio."""
    try:
        hit_ratio = await cache_service.get_hit_ratio()
        return JSONResponse(content={
            "hit_ratio": hit_ratio,
            "percentage": round(hit_ratio * 100, 2)
        })
    except Exception as e:
        logger.error("Failed to get cache hit ratio", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve cache hit ratio"
        )


@router.post("/rate-limit/check", summary="Check Rate Limit")
async def check_rate_limit(
    key: str = Query(..., description="Rate limit key"),
    limit: int = Query(100, description="Request limit"),
    window: int = Query(3600, description="Time window in seconds")
):
    """Check if request is within rate limit."""
    try:
        if not cache_service.rate_limiter:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiter not available"
            )

        is_allowed, info = await cache_service.rate_limiter.is_allowed(key, limit, window)

        status_code = status.HTTP_200_OK if is_allowed else status.HTTP_429_TOO_MANY_REQUESTS

        return JSONResponse(
            status_code=status_code,
            content={
                "allowed": is_allowed,
                "rate_limit_info": info
            }
        )
    except Exception as e:
        logger.error("Rate limit check failed", key=key, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rate limit check failed: {e}"
        )


@router.post("/pubsub/publish", summary="Publish Message")
async def publish_message(
    channel: str = Query(..., description="Channel name"),
    message: Dict[str, Any] = Body(..., description="Message to publish")
):
    """Publish a message to a Redis channel."""
    try:
        if not cache_service.pubsub_service:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pub/Sub service not available"
            )

        subscribers_count = await cache_service.pubsub_service.publish(channel, message)

        return JSONResponse(content={
            "message": f"Message published to channel '{channel}'",
            "channel": channel,
            "subscribers_notified": subscribers_count
        })
    except Exception as e:
        logger.error("Failed to publish message", channel=channel, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to publish message: {e}"
        )


@router.get("/session/{session_id}", summary="Get Session Data")
async def get_session(session_id: str):
    """Get session data by ID."""
    try:
        if not cache_service.session_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Session manager not available"
            )

        session_data = await cache_service.session_manager.get_session(session_id)

        if session_data is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )

        return JSONResponse(content={
            "session_id": session_id,
            "data": session_data
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get session", session_id=session_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve session: {e}"
        )


@router.delete("/session/{session_id}", summary="Delete Session")
async def delete_session(session_id: str):
    """Delete session by ID."""
    try:
        if not cache_service.session_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Session manager not available"
            )

        success = await cache_service.session_manager.delete_session(session_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )

        return JSONResponse(content={
            "message": f"Session '{session_id}' deleted successfully"
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete session", session_id=session_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete session: {e}"
        )
