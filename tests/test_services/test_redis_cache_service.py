"""Comprehensive tests for Redis cache service."""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta
import json

from app.services.cache_service import (
    CacheService,
    EnhancedCacheService,
    RedisPubSubService,
    RedisRateLimiter,
    RedisSessionManager
)


class TestCacheService:
    """Test cases for CacheService."""

    @pytest.fixture
    def cache_service(self):
        return CacheService()

    @pytest.fixture
    def mock_redis_client(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        mock_client.config_set = AsyncMock(return_value=True)
        mock_client.info = AsyncMock(return_value={
            "connected_clients": 1,
            "used_memory_human": "1MB",
            "keyspace_hits": 100,
            "keyspace_misses": 50,
            "total_commands_processed": 1000
        })
        mock_client.get = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)
        mock_client.delete = AsyncMock(return_value=1)
        mock_client.exists = AsyncMock(return_value=True)
        mock_client.mget = AsyncMock(return_value=[])
        mock_client.keys = AsyncMock(return_value=[])
        # Create a proper pipeline context manager
        class MockPipelineContext:
            def __init__(self):
                self.pipeline = AsyncMock()
                self.pipeline.execute = AsyncMock(return_value=[])
                self.pipeline.set = AsyncMock()
                self.pipeline.get = AsyncMock()
                self.pipeline.zremrangebyscore = AsyncMock()
                self.pipeline.zcard = AsyncMock()
                self.pipeline.zadd = AsyncMock()
                self.pipeline.expire = AsyncMock()

            async def __aenter__(self):
                return self.pipeline

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        mock_client.pipeline = MockPipelineContext
        mock_client.close = AsyncMock()
        return mock_client

    @pytest.mark.asyncio
    async def test_initialize_success(self, cache_service, mock_redis_client):
        """Test successful Redis initialization."""
        with patch('app.services.cache_service.redis.from_url') as mock_from_url, \
             patch('app.services.cache_service.ConnectionPool.from_url') as mock_pool:

            mock_pool.return_value = AsyncMock()
            mock_from_url.return_value = mock_redis_client

            await cache_service.initialize()

            assert cache_service._initialized is True
            assert cache_service.redis_client is not None

    @pytest.mark.asyncio
    async def test_initialize_no_redis_url(self, cache_service):
        """Test initialization when Redis URL is not configured."""
        with patch('app.core.config.settings.redis.url', None):
            await cache_service.initialize()

            assert cache_service._initialized is False
            assert cache_service.redis_client is None

    @pytest.mark.asyncio
    async def test_serialize_deserialize_value(self, cache_service):
        """Test value serialization and deserialization."""
        # Test simple types
        test_values = [
            "string",
            123,
            45.67,
            True,
            None,
            ["list", "of", "items"],
            {"key": "value", "nested": {"data": 123}}
        ]

        for value in test_values:
            serialized = cache_service._serialize_value(value)
            deserialized = cache_service._deserialize_value(serialized)
            assert deserialized == value

    @pytest.mark.asyncio
    async def test_get_set_delete(self, cache_service, mock_redis_client):
        """Test basic cache operations."""
        cache_service.redis_client = mock_redis_client
        cache_service._initialized = True

        # Test set
        mock_redis_client.set.return_value = True
        result = await cache_service.set("test_key", "test_value", ttl=300)
        assert result is True

        # Test get
        mock_redis_client.get.return_value = cache_service._serialize_value("test_value")
        result = await cache_service.get("test_key")
        assert result == "test_value"

        # Test delete
        mock_redis_client.delete.return_value = 1
        result = await cache_service.delete("test_key")
        assert result is True

    @pytest.mark.asyncio
    async def test_get_many_set_many(self, cache_service, mock_redis_client):
        """Test bulk operations."""
        cache_service.redis_client = mock_redis_client
        cache_service._initialized = True

        # Test set_many
        mapping = {"key1": "value1", "key2": "value2"}
        mock_pipeline = AsyncMock()
        mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
        mock_pipeline.__aexit__ = AsyncMock(return_value=None)
        mock_pipeline.execute = AsyncMock(return_value=[True, True])
        mock_pipeline.set = AsyncMock()

        # Create a proper context manager
        class MockPipelineContext:
            def __init__(self, pipeline):
                self.pipeline = pipeline

            async def __aenter__(self):
                return self.pipeline

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        mock_redis_client.pipeline.return_value = MockPipelineContext(mock_pipeline)
        result = await cache_service.set_many(mapping, ttl=300)
        assert result is True

        # Test get_many
        mock_redis_client.mget.return_value = [
            cache_service._serialize_value("value1"),
            cache_service._serialize_value("value2")
        ]
        result = await cache_service.get_many(["key1", "key2"])
        assert result == {"key1": "value1", "key2": "value2"}

    @pytest.mark.asyncio
    async def test_get_or_set(self, cache_service, mock_redis_client):
        """Test get_or_set functionality."""
        cache_service.redis_client = mock_redis_client
        cache_service._initialized = True

        # Test cache miss
        mock_redis_client.get.return_value = None
        mock_redis_client.set.return_value = True

        async def factory_func():
            return "computed_value"

        result = await cache_service.get_or_set("test_key", factory_func, ttl=300)
        assert result == "computed_value"

        # Test cache hit
        mock_redis_client.get.return_value = cache_service._serialize_value("cached_value")
        result = await cache_service.get_or_set("test_key", factory_func, ttl=300)
        assert result == "cached_value"

    @pytest.mark.asyncio
    async def test_invalidate_pattern(self, cache_service, mock_redis_client):
        """Test pattern invalidation."""
        cache_service.redis_client = mock_redis_client
        cache_service._initialized = True

        mock_redis_client.keys.return_value = ["key1", "key2", "key3"]
        mock_redis_client.delete.return_value = 3

        result = await cache_service.invalidate_pattern("test:*")
        assert result == 3

    @pytest.mark.asyncio
    async def test_get_stats(self, cache_service, mock_redis_client):
        """Test statistics retrieval."""
        cache_service.redis_client = mock_redis_client
        cache_service._initialized = True

        result = await cache_service.get_stats()
        assert result["status"] == "healthy"
        assert "connected_clients" in result
        assert "used_memory" in result


class TestRedisPubSubService:
    """Test cases for RedisPubSubService."""

    @pytest.fixture
    def pubsub_service(self):
        cache_service = CacheService()

        # Create a proper pubsub client mock
        mock_pubsub_client = AsyncMock()
        mock_pubsub_client.publish = AsyncMock(return_value=2)

        # Create a pubsub instance mock
        mock_pubsub_instance = AsyncMock()
        mock_pubsub_instance.subscribe = AsyncMock()
        mock_pubsub_instance.close = AsyncMock()
        mock_pubsub_instance.listen = AsyncMock()
        mock_pubsub_instance.listen.return_value.__aiter__ = AsyncMock(return_value=iter([]))

        # Make pubsub() method return the instance
        mock_pubsub_client.pubsub = lambda: mock_pubsub_instance

        cache_service.pubsub_client = mock_pubsub_client
        cache_service.redis_client = AsyncMock()
        return RedisPubSubService(cache_service)

    @pytest.mark.asyncio
    async def test_publish_message(self, pubsub_service):
        """Test message publishing."""
        pubsub_service.cache_service.pubsub_client.publish.return_value = 2

        result = await pubsub_service.publish("test_channel", {"message": "test"})
        assert result == 2

    @pytest.mark.asyncio
    async def test_subscribe_handler(self, pubsub_service):
        """Test subscription with handler."""
        handler_called = False

        async def test_handler(message):
            nonlocal handler_called
            handler_called = True
            assert message == {"test": "data"}

        await pubsub_service.subscribe("test_channel", test_handler)

        # Simulate message
        await pubsub_service.publish("test_channel", {"test": "data"})

        # Note: In real implementation, this would be handled by the subscriber loop
        assert "test_channel" in pubsub_service.subscribers


class TestRedisRateLimiter:
    """Test cases for RedisRateLimiter."""

    @pytest.fixture
    def rate_limiter(self):
        cache_service = CacheService()

        # Create a shared pipeline mock that can be configured
        shared_pipeline = AsyncMock()
        shared_pipeline.execute = AsyncMock(return_value=[0, 5, True, True])  # Default values
        shared_pipeline.set = AsyncMock()
        shared_pipeline.get = AsyncMock()
        shared_pipeline.zremrangebyscore = AsyncMock()
        shared_pipeline.zcard = AsyncMock()
        shared_pipeline.zadd = AsyncMock()
        shared_pipeline.expire = AsyncMock()

        # Create a proper pipeline context manager that returns the shared pipeline
        class MockPipelineContext:
            def __init__(self, pipeline):
                self.pipeline = pipeline

            async def __aenter__(self):
                return self.pipeline

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        mock_client = AsyncMock()
        mock_client.pipeline = lambda: MockPipelineContext(shared_pipeline)
        cache_service.redis_client = mock_client

        return RedisRateLimiter(cache_service)

    @pytest.mark.asyncio
    async def test_rate_limit_allowed(self, rate_limiter):
        """Test rate limit when request is allowed."""
        # Set up the pipeline mock to return the expected values
        pipeline_instance = rate_limiter.cache_service.redis_client.pipeline().pipeline
        pipeline_instance.execute.return_value = [0, 5, True, True]

        is_allowed, info = await rate_limiter.is_allowed("test_key", 10, 60)

        assert is_allowed is True
        assert info["limit"] == 10
        assert info["remaining"] == 4

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, rate_limiter):
        """Test rate limit when request is not allowed."""
        # Set up the pipeline mock to return the expected values for exceeded limit
        # Access the shared pipeline and configure it
        pipeline_context = rate_limiter.cache_service.redis_client.pipeline()
        pipeline_context.pipeline.execute.return_value = [0, 10, True, True]  # current_count = 10, limit = 10

        is_allowed, info = await rate_limiter.is_allowed("test_key", 10, 60)

        assert is_allowed is False
        assert info["limit"] == 10
        assert info["remaining"] == 0


class TestRedisSessionManager:
    """Test cases for RedisSessionManager."""

    @pytest.fixture
    def session_manager(self):
        cache_service = CacheService()
        cache_service.redis_client = AsyncMock()
        cache_service.set = AsyncMock(return_value=True)
        cache_service.get = AsyncMock()
        cache_service.delete = AsyncMock(return_value=True)
        return RedisSessionManager(cache_service)

    @pytest.mark.asyncio
    async def test_create_session(self, session_manager):
        """Test session creation."""
        session_manager.cache_service.set.return_value = True

        result = await session_manager.create_session("session123", {"user_id": 1})

        assert result is True
        session_manager.cache_service.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_session(self, session_manager):
        """Test session retrieval."""
        session_data = {"created_at": "2024-01-01T00:00:00", "data": {"user_id": 1}}
        session_manager.cache_service.get.return_value = session_data

        result = await session_manager.get_session("session123")

        assert result == {"user_id": 1}

    @pytest.mark.asyncio
    async def test_update_session(self, session_manager):
        """Test session update."""
        existing_session = {"created_at": "2024-01-01T00:00:00", "data": {"user_id": 1}}
        session_manager.cache_service.get.return_value = existing_session
        session_manager.cache_service.set.return_value = True

        result = await session_manager.update_session("session123", {"user_id": 2})

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_session(self, session_manager):
        """Test session deletion."""
        session_manager.cache_service.delete.return_value = True

        result = await session_manager.delete_session("session123")

        assert result is True


class TestEnhancedCacheService:
    """Test cases for EnhancedCacheService."""

    @pytest.fixture
    def enhanced_cache_service(self):
        return EnhancedCacheService()

    @pytest.fixture
    def mock_redis_client(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        mock_client.config_set = AsyncMock(return_value=True)
        mock_client.info = AsyncMock(return_value={
            "connected_clients": 1,
            "used_memory_human": "1MB",
            "keyspace_hits": 100,
            "keyspace_misses": 50,
            "total_commands_processed": 1000
        })
        mock_client.get = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)
        mock_client.delete = AsyncMock(return_value=1)
        mock_client.exists = AsyncMock(return_value=True)
        mock_client.mget = AsyncMock(return_value=[])
        mock_client.keys = AsyncMock(return_value=[])
        # Create a proper pipeline context manager
        class MockPipelineContext:
            def __init__(self):
                self.pipeline = AsyncMock()
                self.pipeline.execute = AsyncMock(return_value=[])
                self.pipeline.set = AsyncMock()
                self.pipeline.get = AsyncMock()
                self.pipeline.zremrangebyscore = AsyncMock()
                self.pipeline.zcard = AsyncMock()
                self.pipeline.zadd = AsyncMock()
                self.pipeline.expire = AsyncMock()

            async def __aenter__(self):
                return self.pipeline

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        mock_client.pipeline = MockPipelineContext
        mock_client.close = AsyncMock()
        return mock_client

    @pytest.mark.asyncio
    async def test_initialize_enhanced(self, enhanced_cache_service, mock_redis_client):
        """Test enhanced cache service initialization."""
        with patch('app.services.cache_service.redis.from_url') as mock_from_url, \
             patch('app.services.cache_service.ConnectionPool.from_url') as mock_pool:

            mock_pool.return_value = AsyncMock()
            mock_from_url.return_value = mock_redis_client

            await enhanced_cache_service.initialize()

            assert enhanced_cache_service._initialized is True
            assert enhanced_cache_service.pubsub_service is not None
            assert enhanced_cache_service.rate_limiter is not None
            assert enhanced_cache_service.session_manager is not None

    @pytest.mark.asyncio
    async def test_get_hit_ratio(self, enhanced_cache_service):
        """Test hit ratio calculation."""
        enhanced_cache_service._stats = {"hits": 80, "misses": 20}

        hit_ratio = await enhanced_cache_service.get_hit_ratio()

        assert hit_ratio == 0.8

    @pytest.mark.asyncio
    async def test_get_stats_enhanced(self, enhanced_cache_service, mock_redis_client):
        """Test enhanced statistics."""
        enhanced_cache_service.redis_client = mock_redis_client
        enhanced_cache_service._initialized = True
        enhanced_cache_service._stats = {"hits": 80, "misses": 20}

        stats = await enhanced_cache_service.get_stats()

        assert "hit_ratio" in stats
        assert "local_stats" in stats
        assert stats["hit_ratio"] == 0.8


class TestCacheDecorators:
    """Test cases for cache decorators."""

    @pytest.mark.asyncio
    async def test_cache_result_decorator(self):
        """Test cache_result decorator."""
        cache_service = CacheService()

        # Mock Redis client
        mock_client = AsyncMock()
        mock_client.ping.return_value = True
        mock_client.get.return_value = None
        mock_client.set.return_value = True

        cache_service.redis_client = mock_client
        cache_service._initialized = True

        call_count = 0

        @cache_service.cache_result(ttl=300)
        async def expensive_function(param):
            nonlocal call_count
            call_count += 1
            return f"result_{param}"

        # First call should execute function
        result = await expensive_function("test")
        assert result == "result_test"
        assert call_count == 1

        # Second call should also execute function (since cache returns None)
        result = await expensive_function("test")
        assert result == "result_test"
        assert call_count == 2  # Should increment since cache miss


class TestRedisIntegration:
    """Integration tests for Redis functionality."""

    @pytest.mark.asyncio
    async def test_full_cache_workflow(self):
        """Test complete cache workflow."""
        cache_service = CacheService()

        # Mock Redis client
        mock_client = AsyncMock()
        mock_client.ping.return_value = True
        mock_client.config_set.return_value = True
        mock_client.set.return_value = True
        mock_client.get.return_value = None
        mock_client.delete.return_value = 1

        with patch('app.services.cache_service.ConnectionPool.from_url') as mock_pool, \
             patch('app.services.cache_service.redis.Redis') as mock_redis_class:

            mock_pool.return_value = AsyncMock()
            mock_redis_class.return_value = mock_client

            # Initialize
            await cache_service.initialize()

            # Set value
            result = await cache_service.set("test_key", "test_value", ttl=300)
            assert result is True

            # Get value (cache miss)
            result = await cache_service.get("test_key")
            assert result is None  # Mock returns None for cache miss

            # Delete value
            result = await cache_service.delete("test_key")
            assert result is True

            # Close
            await cache_service.close()
            assert cache_service._initialized is False
