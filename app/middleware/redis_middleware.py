"""Redis-based middleware for rate limiting and session management."""

import time
from typing import Callable, Optional
from fastapi import Request, Response, HTTPException, status
from fastapi.responses import JSONResponse
import structlog

from app.services.cache_service import cache_service
from app.core.exceptions import RateLimitError

logger = structlog.get_logger(__name__)


class RedisRateLimitMiddleware:
    """Redis-based rate limiting middleware."""

    def __init__(
        self,
        requests_per_minute: int = 60,
        requests_per_hour: int = 1000,
        requests_per_day: int = 10000,
        key_func: Optional[Callable[[Request], str]] = None
    ):
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.requests_per_day = requests_per_day
        self.key_func = key_func or self._default_key_func

    def _default_key_func(self, request: Request) -> str:
        """Default function to generate rate limit key."""
        # Use client IP as default key
        client_ip = request.client.host if request.client else "unknown"
        return f"rate_limit:{client_ip}"

    async def __call__(self, request: Request, call_next: Callable) -> Response:
        """Process request with rate limiting."""
        if not cache_service.rate_limiter:
            # If rate limiter is not available, continue without rate limiting
            return await call_next(request)

        try:
            # Generate rate limit key
            key = self.key_func(request)

            # Check rate limits in order of strictness
            limits = [
                (self.requests_per_minute, 60, "minute"),
                (self.requests_per_hour, 3600, "hour"),
                (self.requests_per_day, 86400, "day")
            ]

            for limit, window, period in limits:
                is_allowed, info = await cache_service.rate_limiter.is_allowed(
                    f"{key}:{period}", limit, window
                )

                if not is_allowed:
                    logger.warning(
                        "Rate limit exceeded",
                        key=key,
                        period=period,
                        limit=limit,
                        current_count=info.get("current_count", 0)
                    )

                    return JSONResponse(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        content={
                            "error": "Rate limit exceeded",
                            "message": f"Too many requests per {period}",
                            "limit": limit,
                            "remaining": info.get("remaining", 0),
                            "reset_time": info.get("reset_time", 0)
                        },
                        headers={
                            "X-RateLimit-Limit": str(limit),
                            "X-RateLimit-Remaining": str(info.get("remaining", 0)),
                            "X-RateLimit-Reset": str(info.get("reset_time", 0)),
                            "Retry-After": str(window)
                        }
                    )

            # Process request
            response = await call_next(request)

            # Add rate limit headers to response
            response.headers["X-RateLimit-Limit-Minute"] = str(self.requests_per_minute)
            response.headers["X-RateLimit-Limit-Hour"] = str(self.requests_per_hour)
            response.headers["X-RateLimit-Limit-Day"] = str(self.requests_per_day)

            return response

        except Exception as e:
            logger.error("Rate limit middleware error", error=str(e))
            # Continue without rate limiting on error
            return await call_next(request)


class RedisSessionMiddleware:
    """Redis-based session middleware."""

    def __init__(
        self,
        session_cookie_name: str = "session_id",
        session_ttl: int = 3600,  # 1 hour
        auto_extend: bool = True
    ):
        self.session_cookie_name = session_cookie_name
        self.session_ttl = session_ttl
        self.auto_extend = auto_extend

    async def __call__(self, request: Request, call_next: Callable) -> Response:
        """Process request with session management."""
        if not cache_service.session_manager:
            # If session manager is not available, continue without session management
            return await call_next(request)

        try:
            # Get session ID from cookie
            session_id = request.cookies.get(self.session_cookie_name)

            # Create new session if none exists
            if not session_id:
                session_id = self._generate_session_id()
                await cache_service.session_manager.create_session(
                    session_id, {}, self.session_ttl
                )

            # Get session data
            session_data = await cache_service.session_manager.get_session(session_id)

            # Add session to request state
            request.state.session_id = session_id
            request.state.session_data = session_data or {}

            # Process request
            response = await call_next(request)

            # Set session cookie
            response.set_cookie(
                self.session_cookie_name,
                session_id,
                max_age=self.session_ttl,
                httponly=True,
                secure=True,
                samesite="lax"
            )

            # Auto-extend session if enabled
            if self.auto_extend and session_id:
                await cache_service.session_manager.extend_session(session_id, self.session_ttl)

            return response

        except Exception as e:
            logger.error("Session middleware error", error=str(e))
            # Continue without session management on error
            return await call_next(request)

    def _generate_session_id(self) -> str:
        """Generate a unique session ID."""
        import secrets
        return secrets.token_urlsafe(32)


class RedisCacheMiddleware:
    """Redis-based caching middleware for API responses."""

    def __init__(
        self,
        cache_ttl: int = 300,  # 5 minutes
        cache_key_func: Optional[Callable[[Request], str]] = None,
        skip_cache_func: Optional[Callable[[Request], bool]] = None
    ):
        self.cache_ttl = cache_ttl
        self.cache_key_func = cache_key_func or self._default_cache_key_func
        self.skip_cache_func = skip_cache_func or self._default_skip_cache_func

    def _default_cache_key_func(self, request: Request) -> str:
        """Default function to generate cache key."""
        # Include method, path, and query parameters
        key_parts = [
            request.method,
            request.url.path,
            str(sorted(request.query_params.items()))
        ]
        return f"cache:{':'.join(key_parts)}"

    def _default_skip_cache_func(self, request: Request) -> bool:
        """Default function to determine if request should skip cache."""
        # Skip cache for POST, PUT, DELETE requests
        return request.method in ["POST", "PUT", "DELETE", "PATCH"]

    async def __call__(self, request: Request, call_next: Callable) -> Response:
        """Process request with caching."""
        if not cache_service._initialized:
            # If cache is not available, continue without caching
            return await call_next(request)

        try:
            # Skip cache for certain requests
            if self.skip_cache_func(request):
                return await call_next(request)

            # Generate cache key
            cache_key = self.cache_key_func(request)

            # Try to get cached response
            cached_response = await cache_service.get(cache_key, namespace="response_cache")

            if cached_response:
                logger.debug("Cache hit for API response", key=cache_key)
                return JSONResponse(content=cached_response)

            # Process request
            response = await call_next(request)

            # Cache successful GET responses
            if (request.method == "GET" and
                response.status_code == 200 and
                hasattr(response, 'body')):

                try:
                    # Get response body
                    response_body = response.body.decode() if hasattr(response.body, 'decode') else str(response.body)

                    # Cache the response
                    await cache_service.set(
                        cache_key,
                        response_body,
                        ttl=self.cache_ttl,
                        namespace="response_cache"
                    )

                    logger.debug("Cached API response", key=cache_key, ttl=self.cache_ttl)

                except Exception as e:
                    logger.warning("Failed to cache response", key=cache_key, error=str(e))

            return response

        except Exception as e:
            logger.error("Cache middleware error", error=str(e))
            # Continue without caching on error
            return await call_next(request)
