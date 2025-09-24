"""Advanced Redis caching service with comprehensive features."""

import json
import asyncio
import pickle
import hashlib
from typing import Any, Optional, Union, Callable, Dict, List, Tuple
from functools import wraps
from datetime import timedelta, datetime
import redis.asyncio as redis
from redis.exceptions import RedisError, ConnectionError, TimeoutError
from redis.asyncio import ConnectionPool
import structlog

from app.core.config import settings
from app.core.exceptions import ExternalAPIError, CacheError

logger = structlog.get_logger(__name__)


class CacheService:
    """Advanced Redis-based caching service with comprehensive features."""

    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self.pubsub_client: Optional[redis.Redis] = None
        self.connection_pool: Optional[ConnectionPool] = None
        self._initialized = False
        self._stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletes": 0,
            "errors": 0
        }

    async def initialize(self) -> None:
        """Initialize Redis connection with advanced configuration."""
        if self._initialized:
            return

        try:
            if not settings.redis.url:
                logger.warning("Redis URL not configured, caching disabled")
                return

            # Create connection pool for better performance
            self.connection_pool = ConnectionPool.from_url(
                settings.redis.url,
                max_connections=settings.redis.max_connections,
                retry_on_timeout=True,
                socket_keepalive=True,
                socket_keepalive_options={},
                health_check_interval=30,
                decode_responses=True
            )

            # Create main Redis client
            self.redis_client = redis.Redis(
                connection_pool=self.connection_pool,
                protocol=3,  # Use RESP3 for better performance
                retry_on_timeout=True
            )

            # Create separate client for Pub/Sub
            self.pubsub_client = redis.Redis(
                connection_pool=self.connection_pool,
                protocol=3
            )

            # Test connection with timeout
            await asyncio.wait_for(self.redis_client.ping(), timeout=5.0)

            # Set up Redis configuration for optimal performance
            await self._configure_redis()

            self._initialized = True
            logger.info("Redis cache service initialized successfully",
                       max_connections=settings.redis.max_connections)

        except Exception as e:
            logger.error("Failed to initialize Redis cache service", error=str(e), exc_info=True)
            self.redis_client = None
            self.pubsub_client = None
            self.connection_pool = None

    async def _configure_redis(self) -> None:
        """Configure Redis for optimal performance."""
        try:
            # Set memory policy to allkeys-lru for better cache management
            await self.redis_client.config_set("maxmemory-policy", "allkeys-lru")

            # Enable keyspace notifications for cache invalidation
            await self.redis_client.config_set("notify-keyspace-events", "Ex")

            logger.info("Redis configuration optimized for caching")
        except Exception as e:
            logger.warning("Failed to configure Redis", error=str(e))

    async def close(self) -> None:
        """Close Redis connection."""
        if self.redis_client:
            await self.redis_client.close()
            self._initialized = False
            logger.info("Redis cache service closed")

    def _serialize_value(self, value: Any) -> bytes:
        """Serialize value for storage with intelligent format selection."""
        try:
            # Try JSON first for simple types
            if isinstance(value, (str, int, float, bool, list, dict, type(None))):
                return json.dumps(value).encode('utf-8')
            else:
                # Use pickle for complex objects
                return pickle.dumps(value)
        except (TypeError, ValueError):
            # Fallback to string representation
            return str(value).encode('utf-8')

    def _deserialize_value(self, value: bytes) -> Any:
        """Deserialize value from storage with format detection."""
        if not value:
            return None

        try:
            # Try JSON first
            return json.loads(value.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                # Try pickle
                return pickle.loads(value)
            except (pickle.PickleError, EOFError):
                # Fallback to string
                return value.decode('utf-8', errors='ignore')

    def _build_cache_key(self, key: str, namespace: str = "default") -> str:
        """Build namespaced cache key."""
        return f"{namespace}:{key}"

    def _get_key_hash(self, key: str) -> str:
        """Get hash of key for consistent hashing."""
        return hashlib.md5(key.encode()).hexdigest()[:16]

    async def get(self, key: str, namespace: str = "default") -> Optional[Any]:
        """
        Get value from cache with namespace support.

        Args:
            key: Cache key
            namespace: Cache namespace

        Returns:
            Cached value or None if not found
        """
        if not self._initialized or not self.redis_client:
            self._stats["misses"] += 1
            return None

        try:
            cache_key = self._build_cache_key(key, namespace)
            value = await self.redis_client.get(cache_key)

            if value is not None:
                self._stats["hits"] += 1
                logger.debug("Cache hit", key=cache_key)
                return self._deserialize_value(value)

            self._stats["misses"] += 1
            logger.debug("Cache miss", key=cache_key)
            return None

        except (RedisError, ConnectionError, TimeoutError) as e:
            self._stats["errors"] += 1
            logger.error("Redis get error", key=cache_key, error=str(e))
            return None

    async def get_many(self, keys: List[str], namespace: str = "default") -> Dict[str, Any]:
        """
        Get multiple values from cache in a single operation.

        Args:
            keys: List of cache keys
            namespace: Cache namespace

        Returns:
            Dictionary mapping keys to values
        """
        if not self._initialized or not self.redis_client or not keys:
            return {}

        try:
            cache_keys = [self._build_cache_key(key, namespace) for key in keys]
            values = await self.redis_client.mget(cache_keys)

            result = {}
            for i, value in enumerate(values):
                if value is not None:
                    result[keys[i]] = self._deserialize_value(value)
                    self._stats["hits"] += 1
                else:
                    self._stats["misses"] += 1

            logger.debug("Cache mget", keys=len(keys), hits=len(result))
            return result

        except (RedisError, ConnectionError, TimeoutError) as e:
            self._stats["errors"] += len(keys)
            logger.error("Redis mget error", keys=keys, error=str(e))
            return {}

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[Union[int, timedelta]] = None,
        namespace: str = "default",
        nx: bool = False,
        xx: bool = False
    ) -> bool:
        """
        Set value in cache with advanced options.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time to live in seconds or timedelta
            namespace: Cache namespace
            nx: Only set if key doesn't exist
            xx: Only set if key exists

        Returns:
            True if successful, False otherwise
        """
        if not self._initialized or not self.redis_client:
            return False

        try:
            cache_key = self._build_cache_key(key, namespace)
            serialized_value = self._serialize_value(value)

            if isinstance(ttl, timedelta):
                ttl = int(ttl.total_seconds())

            # Use appropriate Redis command based on options
            if nx:
                result = await self.redis_client.set(cache_key, serialized_value, ex=ttl, nx=True)
            elif xx:
                result = await self.redis_client.set(cache_key, serialized_value, ex=ttl, xx=True)
            else:
                result = await self.redis_client.set(cache_key, serialized_value, ex=ttl)

            if result:
                self._stats["sets"] += 1
                logger.debug("Cache set", key=cache_key, ttl=ttl, nx=nx, xx=xx)
                return True
            else:
                logger.debug("Cache set failed", key=cache_key, nx=nx, xx=xx)
                return False

        except (RedisError, ConnectionError, TimeoutError) as e:
            self._stats["errors"] += 1
            logger.error("Redis set error", key=cache_key, error=str(e))
            return False

    async def set_many(
        self,
        mapping: Dict[str, Any],
        ttl: Optional[Union[int, timedelta]] = None,
        namespace: str = "default"
    ) -> bool:
        """
        Set multiple values in cache in a single operation.

        Args:
            mapping: Dictionary of key-value pairs
            ttl: Time to live in seconds or timedelta
            namespace: Cache namespace

        Returns:
            True if successful, False otherwise
        """
        if not self._initialized or not self.redis_client or not mapping:
            return False

        try:
            cache_mapping = {}
            for key, value in mapping.items():
                cache_key = self._build_cache_key(key, namespace)
                cache_mapping[cache_key] = self._serialize_value(value)

            if isinstance(ttl, timedelta):
                ttl = int(ttl.total_seconds())

            # Use pipeline for atomic operation
            async with self.redis_client.pipeline() as pipe:
                for cache_key, serialized_value in cache_mapping.items():
                    pipe.set(cache_key, serialized_value, ex=ttl)
                await pipe.execute()

            self._stats["sets"] += len(mapping)
            logger.debug("Cache mset", keys=len(mapping), ttl=ttl)
            return True

        except (RedisError, ConnectionError, TimeoutError) as e:
            self._stats["errors"] += len(mapping)
            logger.error("Redis mset error", keys=list(mapping.keys()), error=str(e))
            return False

    async def delete(self, key: str) -> bool:
        """
        Delete value from cache.

        Args:
            key: Cache key

        Returns:
            True if successful, False otherwise
        """
        if not self._initialized or not self.redis_client:
            return False

        try:
            result = await self.redis_client.delete(key)
            logger.debug("Cache delete", key=key, deleted=bool(result))
            return bool(result)
        except RedisError as e:
            logger.error("Redis delete error", key=key, error=str(e))
            return False

    async def exists(self, key: str) -> bool:
        """
        Check if key exists in cache.

        Args:
            key: Cache key

        Returns:
            True if key exists, False otherwise
        """
        if not self._initialized or not self.redis_client:
            return False

        try:
            result = await self.redis_client.exists(key)
            return bool(result)
        except RedisError as e:
            logger.error("Redis exists error", key=key, error=str(e))
            return False

    async def get_or_set(
        self,
        key: str,
        func: Callable,
        ttl: Optional[Union[int, timedelta]] = None,
        *args,
        **kwargs
    ) -> Any:
        """
        Get value from cache or set it using provided function.

        Args:
            key: Cache key
            func: Function to call if cache miss
            ttl: Time to live in seconds or timedelta
            *args: Arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Cached or computed value
        """
        # Try to get from cache first
        cached_value = await self.get(key)
        if cached_value is not None:
            return cached_value

        # Cache miss, compute value
        try:
            if asyncio.iscoroutinefunction(func):
                value = await func(*args, **kwargs)
            else:
                value = func(*args, **kwargs)

            # Store in cache
            await self.set(key, value, ttl)
            return value

        except Exception as e:
            logger.error("Error computing cached value", key=key, error=str(e))
            raise ExternalAPIError(f"Failed to compute cached value: {e}")

    async def invalidate_pattern(self, pattern: str) -> int:
        """
        Invalidate all keys matching pattern.

        Args:
            pattern: Redis key pattern (e.g., "user:*")

        Returns:
            Number of keys deleted
        """
        if not self._initialized or not self.redis_client:
            return 0

        try:
            keys = await self.redis_client.keys(pattern)
            if keys:
                result = await self.redis_client.delete(*keys)
                logger.info("Cache pattern invalidated", pattern=pattern, count=result)
                return result
            return 0
        except RedisError as e:
            logger.error("Redis pattern invalidation error", pattern=pattern, error=str(e))
            return 0

    async def health_check(self) -> dict:
        """
        Check Redis cache health.

        Returns:
            Dict containing health status
        """
        if not self._initialized or not self.redis_client:
            return {"status": "not_initialized", "redis": "not_connected"}

        try:
            await self.redis_client.ping()
            return {"status": "healthy", "redis": "connected"}
        except RedisError as e:
            logger.error("Redis health check error", error=str(e))
            return {"status": "unhealthy", "redis": "error", "error": str(e)}

    async def clear_cache(self) -> bool:
        """
        Clear all cache data.

        Returns:
            True if successful, False otherwise
        """
        if not self._initialized or not self.redis_client:
            return False

        try:
            await self.redis_client.flushdb()
            logger.info("Cache cleared successfully")
            return True
        except RedisError as e:
            logger.error("Cache clear error", error=str(e))
            return False

    async def get_cache_stats(self) -> dict:
        """
        Get cache statistics (alias for get_stats for backward compatibility).

        Returns:
            Dict containing cache statistics
        """
        return await self.get_stats()

    async def get_stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dict containing cache statistics
        """
        if not self._initialized or not self.redis_client:
            return {"status": "not_initialized"}

        try:
            info = await self.redis_client.info()
            return {
                "status": "healthy",
                "connected_clients": info.get("connected_clients", 0),
                "used_memory": info.get("used_memory_human", "0B"),
                "keyspace_hits": info.get("keyspace_hits", 0),
                "keyspace_misses": info.get("keyspace_misses", 0),
                "total_commands_processed": info.get("total_commands_processed", 0)
            }
        except RedisError as e:
            logger.error("Redis stats error", error=str(e))
            return {"status": "error", "error": str(e)}

    def cache_result(
        self,
        ttl: Optional[Union[int, timedelta]] = None,
        key_prefix: str = "",
        key_builder: Optional[Callable] = None
    ):
        """
        Instance method decorator for caching function results.

        Args:
            ttl: Time to live in seconds or timedelta
            key_prefix: Prefix for cache key
            key_builder: Custom key builder function

        Returns:
            Decorated function
        """
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                # Build cache key
                if key_builder:
                    cache_key = key_builder(*args, **kwargs)
                else:
                    # Simple key building based on function name and arguments
                    key_parts = [key_prefix, func.__name__]
                    key_parts.extend(str(arg) for arg in args)
                    key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
                    cache_key = ":".join(key_parts)

                # Try to get from cache
                cached_result = await self.get(cache_key)
                if cached_result is not None:
                    return cached_result

                # Execute function and cache result
                result = await func(*args, **kwargs)
                await self.set(cache_key, result, ttl=ttl)
                return result

            return wrapper
        return decorator


# Global enhanced cache service instance (will be defined at end of file)


def cache_result(
    ttl: Optional[Union[int, timedelta]] = None,
    key_prefix: str = "",
    key_builder: Optional[Callable] = None
):
    """
    Decorator for caching function results.

    Args:
        ttl: Time to live in seconds or timedelta
        key_prefix: Prefix for cache key
        key_builder: Custom key builder function

    Returns:
        Decorated function
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Build cache key
            if key_builder:
                cache_key = key_builder(func.__name__, *args, **kwargs)
            else:
                # Simple key building
                key_parts = [key_prefix, func.__name__]
                if args:
                    key_parts.extend(str(arg) for arg in args)
                if kwargs:
                    key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
                cache_key = ":".join(key_parts)

            return await cache_service.get_or_set(cache_key, func, ttl, *args, **kwargs)

        return wrapper
    return decorator


# Cache key builders
def user_cache_key(user_id: int) -> str:
    """Build cache key for user data."""
    return f"user:{user_id}"


def forex_news_cache_key(date_str: str, currency: str = None) -> str:
    """Build cache key for forex news."""
    if currency:
        return f"forex_news:{date_str}:{currency}"
    return f"forex_news:{date_str}"


def chart_cache_key(currency: str, event_time: str, window_hours: int) -> str:
    """Build cache key for chart data."""
    return f"chart:{currency}:{event_time}:{window_hours}h"


class RedisPubSubService:
    """Redis Pub/Sub service for real-time features."""

    def __init__(self, cache_service: CacheService):
        self.cache_service = cache_service
        self.subscribers: Dict[str, List[Callable]] = {}
        self._running = False

    async def publish(self, channel: str, message: Any) -> int:
        """Publish message to channel."""
        if not self.cache_service.pubsub_client:
            return 0

        try:
            serialized_message = json.dumps(message, default=str)
            subscribers_count = await self.cache_service.pubsub_client.publish(channel, serialized_message)
            logger.debug("Message published", channel=channel, subscribers=subscribers_count)
            return subscribers_count
        except Exception as e:
            logger.error("Failed to publish message", channel=channel, error=str(e))
            return 0

    async def subscribe(self, channel: str, handler: Callable) -> None:
        """Subscribe to channel with handler."""
        if channel not in self.subscribers:
            self.subscribers[channel] = []
        self.subscribers[channel].append(handler)

        if not self._running:
            await self._start_subscriber()

    async def _start_subscriber(self) -> None:
        """Start the subscriber loop."""
        if self._running or not self.cache_service.pubsub_client:
            return

        self._running = True
        pubsub = self.cache_service.pubsub_client.pubsub()

        try:
            # Subscribe to all channels
            for channel in self.subscribers.keys():
                await pubsub.subscribe(channel)

            # Start listening loop
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    channel = message['channel']
                    data = json.loads(message['data'])

                    # Call all handlers for this channel
                    for handler in self.subscribers.get(channel, []):
                        try:
                            await handler(data)
                        except Exception as e:
                            logger.error("Handler error", channel=channel, error=str(e))

        except Exception as e:
            logger.error("Subscriber error", error=str(e))
        finally:
            await pubsub.close()
            self._running = False


class RedisRateLimiter:
    """Redis-based rate limiter using sliding window algorithm."""

    def __init__(self, cache_service: CacheService):
        self.cache_service = cache_service

    async def is_allowed(
        self,
        key: str,
        limit: int,
        window_seconds: int,
        namespace: str = "rate_limit"
    ) -> Tuple[bool, Dict[str, int]]:
        """
        Check if request is allowed within rate limit.

        Args:
            key: Rate limit key (e.g., user_id, ip_address)
            limit: Maximum requests allowed
            window_seconds: Time window in seconds
            namespace: Cache namespace

        Returns:
            Tuple of (is_allowed, rate_limit_info)
        """
        if not self.cache_service.redis_client:
            return True, {"limit": limit, "remaining": limit, "reset_time": 0}

        try:
            cache_key = f"{namespace}:{key}"
            now = int(datetime.now().timestamp())
            window_start = now - window_seconds

            # Use Redis pipeline for atomic operations
            async with self.cache_service.redis_client.pipeline() as pipe:
                # Remove expired entries
                pipe.zremrangebyscore(cache_key, 0, window_start)

                # Count current requests
                pipe.zcard(cache_key)

                # Add current request
                pipe.zadd(cache_key, {str(now): now})

                # Set expiration
                pipe.expire(cache_key, window_seconds)

                results = await pipe.execute()

            current_count = results[1]
            is_allowed = current_count < limit
            remaining = max(0, limit - current_count - 1)

            return is_allowed, {
                "limit": limit,
                "remaining": remaining,
                "reset_time": now + window_seconds,
                "current_count": current_count
            }

        except Exception as e:
            logger.error("Rate limit check error", key=key, error=str(e))
            return True, {"limit": limit, "remaining": limit, "reset_time": 0}


class RedisSessionManager:
    """Redis-based session management."""

    def __init__(self, cache_service: CacheService):
        self.cache_service = cache_service
        self.default_ttl = 3600  # 1 hour

    async def create_session(
        self,
        session_id: str,
        data: Dict[str, Any],
        ttl: Optional[int] = None
    ) -> bool:
        """Create a new session."""
        ttl = ttl or self.default_ttl
        session_data = {
            "created_at": datetime.now().isoformat(),
            "data": data
        }
        return await self.cache_service.set(
            session_id,
            session_data,
            ttl=ttl,
            namespace="session"
        )

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session data."""
        session_data = await self.cache_service.get(session_id, namespace="session")
        return session_data.get("data") if session_data else None

    async def update_session(
        self,
        session_id: str,
        data: Dict[str, Any],
        ttl: Optional[int] = None
    ) -> bool:
        """Update session data."""
        existing_session = await self.cache_service.get(session_id, namespace="session")
        if not existing_session:
            return False

        ttl = ttl or self.default_ttl
        session_data = {
            "created_at": existing_session.get("created_at"),
            "data": data
        }
        return await self.cache_service.set(
            session_id,
            session_data,
            ttl=ttl,
            namespace="session"
        )

    async def delete_session(self, session_id: str) -> bool:
        """Delete session."""
        return await self.cache_service.delete(session_id, namespace="session")

    async def extend_session(self, session_id: str, ttl: int) -> bool:
        """Extend session TTL."""
        if not self.cache_service.redis_client:
            return False

        try:
            cache_key = self.cache_service._build_cache_key(session_id, "session")
            return await self.cache_service.redis_client.expire(cache_key, ttl)
        except Exception as e:
            logger.error("Session extension error", session_id=session_id, error=str(e))
            return False


# Enhanced cache service with additional features
class EnhancedCacheService(CacheService):
    """Enhanced cache service with Pub/Sub, rate limiting, and session management."""

    def __init__(self):
        super().__init__()
        self.pubsub_service: Optional[RedisPubSubService] = None
        self.rate_limiter: Optional[RedisRateLimiter] = None
        self.session_manager: Optional[RedisSessionManager] = None

    async def initialize(self) -> None:
        """Initialize enhanced cache service."""
        await super().initialize()

        if self._initialized:
            self.pubsub_service = RedisPubSubService(self)
            self.rate_limiter = RedisRateLimiter(self)
            self.session_manager = RedisSessionManager(self)
            logger.info("Enhanced Redis services initialized")

    async def get_hit_ratio(self) -> float:
        """Get cache hit ratio."""
        total_requests = self._stats["hits"] + self._stats["misses"]
        if total_requests == 0:
            return 0.0
        return self._stats["hits"] / total_requests

    async def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive cache statistics."""
        base_stats = await super().get_stats()
        base_stats.update({
            "hit_ratio": await self.get_hit_ratio(),
            "local_stats": self._stats.copy()
        })
        return base_stats


# Global enhanced cache service instance
cache_service = EnhancedCacheService()
