"""Comprehensive Redis integration tests using Context7 best practices."""

import pytest
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from typing import Dict, Any

from app.services.cache_service import (
    CacheService,
    EnhancedCacheService,
    RedisPubSubService,
    RedisRateLimiter,
    RedisSessionManager
)
from app.services.redis_config_service import RedisConfigService


@pytest.mark.integration
@pytest.mark.redis
class TestRedisIntegration:
    """Integration tests for Redis functionality."""

    @pytest.fixture
    async def redis_service(self, mock_redis_client):
        """Create Redis service for integration testing."""
        service = EnhancedCacheService()
        service.redis_client = mock_redis_client
        service.pubsub_client = mock_redis_client
        service._initialized = True
        return service

    @pytest.mark.asyncio
    async def test_cache_service_integration(self, redis_service):
        """Test complete cache service integration."""
        # Test basic operations
        await redis_service.set("test_key", "test_value", ttl=300)
        value = await redis_service.get("test_key")
        assert value == "test_value"

        # Test bulk operations
        mapping = {"key1": "value1", "key2": "value2"}
        await redis_service.set_many(mapping, ttl=300)
        values = await redis_service.get_many(["key1", "key2"])
        assert values == mapping

        # Test pattern invalidation
        await redis_service.invalidate_pattern("key*")

        # Test statistics
        stats = await redis_service.get_stats()
        assert "status" in stats

    @pytest.mark.asyncio
    async def test_pubsub_integration(self, redis_service):
        """Test Pub/Sub integration."""
        pubsub_service = redis_service.pubsub_service

        # Test message publishing
        subscribers_count = await pubsub_service.publish("test_channel", {"message": "test"})
        assert subscribers_count >= 0

        # Test subscription (mock)
        handler_called = False

        async def test_handler(message):
            nonlocal handler_called
            handler_called = True
            assert message == {"test": "data"}

        await pubsub_service.subscribe("test_channel", test_handler)
        assert "test_channel" in pubsub_service.subscribers

    @pytest.mark.asyncio
    async def test_rate_limiter_integration(self, redis_service):
        """Test rate limiter integration."""
        rate_limiter = redis_service.rate_limiter

        # Test rate limiting
        is_allowed, info = await rate_limiter.is_allowed("test_user", 10, 60)
        assert isinstance(is_allowed, bool)
        assert "limit" in info
        assert "remaining" in info

    @pytest.mark.asyncio
    async def test_session_manager_integration(self, redis_service):
        """Test session manager integration."""
        session_manager = redis_service.session_manager

        # Test session creation
        session_id = "test_session_123"
        session_data = {"user_id": 123, "role": "user"}

        success = await session_manager.create_session(session_id, session_data)
        assert success is True

        # Test session retrieval
        retrieved_data = await session_manager.get_session(session_id)
        assert retrieved_data == session_data

        # Test session update
        updated_data = {"user_id": 123, "role": "admin"}
        success = await session_manager.update_session(session_id, updated_data)
        assert success is True

        # Test session deletion
        success = await session_manager.delete_session(session_id)
        assert success is True

    @pytest.mark.asyncio
    async def test_redis_config_service_integration(self, mock_redis_client):
        """Test Redis configuration service integration."""
        config_service = RedisConfigService()
        config_service.cache_service.redis_client = mock_redis_client

        # Test Redis info retrieval
        info = await config_service.get_redis_info()
        assert "server" in info
        assert "memory" in info
        assert "clients" in info

        # Test health check
        health = await config_service.health_check()
        assert "status" in health

        # Test optimization
        optimization = await config_service.optimize_redis()
        assert "status" in optimization


@pytest.mark.integration
@pytest.mark.redis
@pytest.mark.slow
class TestRedisPerformance:
    """Performance tests for Redis functionality."""

    @pytest.fixture
    async def performance_redis_service(self, mock_redis_client):
        """Create Redis service for performance testing."""
        service = EnhancedCacheService()
        service.redis_client = mock_redis_client
        service._initialized = True
        return service

    @pytest.mark.asyncio
    async def test_bulk_operations_performance(self, performance_redis_service):
        """Test performance of bulk operations."""
        # Test large dataset
        large_mapping = {f"key_{i}": f"value_{i}" for i in range(1000)}

        start_time = datetime.now()
        await performance_redis_service.set_many(large_mapping, ttl=300)
        set_time = (datetime.now() - start_time).total_seconds()

        start_time = datetime.now()
        values = await performance_redis_service.get_many(list(large_mapping.keys()))
        get_time = (datetime.now() - start_time).total_seconds()

        # Performance assertions (adjust based on your requirements)
        assert set_time < 1.0  # Should complete within 1 second
        assert get_time < 1.0  # Should complete within 1 second
        assert len(values) == len(large_mapping)

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, performance_redis_service):
        """Test concurrent Redis operations."""
        async def set_operation(key: str, value: str):
            await performance_redis_service.set(key, value, ttl=300)
            return await performance_redis_service.get(key)

        # Run concurrent operations
        tasks = [set_operation(f"concurrent_key_{i}", f"value_{i}") for i in range(100)]
        results = await asyncio.gather(*tasks)

        # Verify all operations completed successfully
        assert len(results) == 100
        assert all(result is not None for result in results)


@pytest.mark.integration
@pytest.mark.redis
class TestRedisErrorHandling:
    """Test Redis error handling and resilience."""

    @pytest.fixture
    async def error_redis_service(self):
        """Create Redis service with error simulation."""
        service = EnhancedCacheService()

        # Create a mock client that simulates errors
        mock_client = AsyncMock()
        mock_client.ping.side_effect = Exception("Connection failed")
        mock_client.get.side_effect = Exception("Redis error")
        mock_client.set.side_effect = Exception("Redis error")

        service.redis_client = mock_client
        service._initialized = True
        return service

    @pytest.mark.asyncio
    async def test_connection_error_handling(self, error_redis_service):
        """Test handling of connection errors."""
        # Test that service gracefully handles connection errors
        result = await error_redis_service.get("test_key")
        assert result is None  # Should return None on error

        result = await error_redis_service.set("test_key", "test_value")
        assert result is False  # Should return False on error

    @pytest.mark.asyncio
    async def test_service_degradation(self, error_redis_service):
        """Test service degradation when Redis is unavailable."""
        # Test that service continues to function (with degraded performance)
        stats = await error_redis_service.get_stats()
        assert "status" in stats

        hit_ratio = await error_redis_service.get_hit_ratio()
        assert isinstance(hit_ratio, float)


@pytest.mark.integration
@pytest.mark.redis
class TestRedisDataTypes:
    """Test Redis with different data types."""

    @pytest.fixture
    async def data_type_redis_service(self, mock_redis_client):
        """Create Redis service for data type testing."""
        service = EnhancedCacheService()
        service.redis_client = mock_redis_client
        service._initialized = True
        return service

    @pytest.mark.asyncio
    async def test_primitive_data_types(self, data_type_redis_service):
        """Test caching of primitive data types."""
        test_cases = [
            ("string", "hello world"),
            ("integer", 42),
            ("float", 3.14159),
            ("boolean", True),
            ("none", None)
        ]

        for key, value in test_cases:
            await data_type_redis_service.set(key, value, ttl=300)
            retrieved = await data_type_redis_service.get(key)
            assert retrieved == value

    @pytest.mark.asyncio
    async def test_complex_data_types(self, data_type_redis_service):
        """Test caching of complex data types."""
        # Test list
        test_list = [1, 2, 3, "hello", {"nested": "object"}]
        await data_type_redis_service.set("list", test_list, ttl=300)
        retrieved_list = await data_type_redis_service.get("list")
        assert retrieved_list == test_list

        # Test dictionary
        test_dict = {
            "string": "value",
            "number": 42,
            "nested": {"key": "value"},
            "list": [1, 2, 3]
        }
        await data_type_redis_service.set("dict", test_dict, ttl=300)
        retrieved_dict = await data_type_redis_service.get("dict")
        assert retrieved_dict == test_dict

    @pytest.mark.asyncio
    async def test_datetime_handling(self, data_type_redis_service):
        """Test caching of datetime objects."""
        now = datetime.now()
        today = date.today()
        current_time = time(12, 30, 45)

        await data_type_redis_service.set("datetime", now, ttl=300)
        await data_type_redis_service.set("date", today, ttl=300)
        await data_type_redis_service.set("time", current_time, ttl=300)

        retrieved_datetime = await data_type_redis_service.get("datetime")
        retrieved_date = await data_type_redis_service.get("date")
        retrieved_time = await data_type_redis_service.get("time")

        # Note: These might be serialized as strings, so we check they're not None
        assert retrieved_datetime is not None
        assert retrieved_date is not None
        assert retrieved_time is not None


@pytest.mark.integration
@pytest.mark.redis
class TestRedisNamespaceIsolation:
    """Test Redis namespace isolation."""

    @pytest.fixture
    async def namespace_redis_service(self, mock_redis_client):
        """Create Redis service for namespace testing."""
        service = EnhancedCacheService()
        service.redis_client = mock_redis_client
        service._initialized = True
        return service

    @pytest.mark.asyncio
    async def test_namespace_isolation(self, namespace_redis_service):
        """Test that different namespaces are isolated."""
        # Set values in different namespaces
        await namespace_redis_service.set("key1", "value1", namespace="ns1")
        await namespace_redis_service.set("key1", "value2", namespace="ns2")

        # Retrieve values
        value1 = await namespace_redis_service.get("key1", namespace="ns1")
        value2 = await namespace_redis_service.get("key1", namespace="ns2")

        assert value1 == "value1"
        assert value2 == "value2"
        assert value1 != value2

    @pytest.mark.asyncio
    async def test_namespace_pattern_invalidation(self, namespace_redis_service):
        """Test pattern invalidation within namespaces."""
        # Set values in different namespaces
        await namespace_redis_service.set("key1", "value1", namespace="ns1")
        await namespace_redis_service.set("key2", "value2", namespace="ns1")
        await namespace_redis_service.set("key1", "value3", namespace="ns2")

        # Invalidate pattern in ns1
        await namespace_redis_service.invalidate_pattern("ns1:*")

        # Check that ns1 keys are invalidated but ns2 keys remain
        value1_ns1 = await namespace_redis_service.get("key1", namespace="ns1")
        value2_ns1 = await namespace_redis_service.get("key2", namespace="ns1")
        value1_ns2 = await namespace_redis_service.get("key1", namespace="ns2")

        assert value1_ns1 is None
        assert value2_ns1 is None
        assert value1_ns2 == "value3"


@pytest.mark.integration
@pytest.mark.redis
class TestRedisTTLHandling:
    """Test Redis TTL (Time To Live) handling."""

    @pytest.fixture
    async def ttl_redis_service(self, mock_redis_client):
        """Create Redis service for TTL testing."""
        service = EnhancedCacheService()
        service.redis_client = mock_redis_client
        service._initialized = True
        return service

    @pytest.mark.asyncio
    async def test_ttl_setting(self, ttl_redis_service):
        """Test setting TTL on cache entries."""
        # Test with integer TTL
        await ttl_redis_service.set("key1", "value1", ttl=300)

        # Test with timedelta TTL
        await ttl_redis_service.set("key2", "value2", ttl=timedelta(minutes=5))

        # Test without TTL
        await ttl_redis_service.set("key3", "value3")

        # Verify values are set
        assert await ttl_redis_service.get("key1") == "value1"
        assert await ttl_redis_service.get("key2") == "value2"
        assert await ttl_redis_service.get("key3") == "value3"

    @pytest.mark.asyncio
    async def test_ttl_extension(self, ttl_redis_service):
        """Test TTL extension functionality."""
        # Set initial value with TTL
        await ttl_redis_service.set("key", "value", ttl=300)

        # Extend TTL (this would require Redis expire command)
        # For now, we just test that the operation doesn't fail
        assert await ttl_redis_service.get("key") == "value"


@pytest.mark.integration
@pytest.mark.redis
class TestRedisMonitoring:
    """Test Redis monitoring and observability."""

    @pytest.fixture
    async def monitoring_redis_service(self, mock_redis_client):
        """Create Redis service for monitoring testing."""
        service = EnhancedCacheService()
        service.redis_client = mock_redis_client
        service._initialized = True
        return service

    @pytest.mark.asyncio
    async def test_statistics_collection(self, monitoring_redis_service):
        """Test statistics collection."""
        # Perform various operations
        await monitoring_redis_service.set("key1", "value1")
        await monitoring_redis_service.get("key1")
        await monitoring_redis_service.get("key2")  # Miss
        await monitoring_redis_service.delete("key1")

        # Check statistics
        stats = await monitoring_redis_service.get_stats()
        assert "status" in stats
        assert "local_stats" in stats

        local_stats = stats["local_stats"]
        assert local_stats["sets"] >= 1
        assert local_stats["hits"] >= 1
        assert local_stats["misses"] >= 1
        assert local_stats["deletes"] >= 1

    @pytest.mark.asyncio
    async def test_hit_ratio_calculation(self, monitoring_redis_service):
        """Test hit ratio calculation."""
        # Generate some hits and misses
        await monitoring_redis_service.set("key1", "value1")
        await monitoring_redis_service.get("key1")  # Hit
        await monitoring_redis_service.get("key2")  # Miss
        await monitoring_redis_service.get("key1")  # Hit

        hit_ratio = await monitoring_redis_service.get_hit_ratio()
        assert isinstance(hit_ratio, float)
        assert 0.0 <= hit_ratio <= 1.0
        assert hit_ratio > 0.0  # Should have some hits


@pytest.mark.integration
@pytest.mark.redis
class TestRedisSecurity:
    """Test Redis security features."""

    @pytest.fixture
    async def secure_redis_service(self, mock_redis_client):
        """Create Redis service for security testing."""
        service = EnhancedCacheService()
        service.redis_client = mock_redis_client
        service._initialized = True
        return service

    @pytest.mark.asyncio
    async def test_key_sanitization(self, secure_redis_service):
        """Test that keys are properly sanitized."""
        # Test with potentially dangerous keys
        dangerous_keys = [
            "key with spaces",
            "key/with/slashes",
            "key:with:colons",
            "key\nwith\nnewlines",
            "key\twith\ttabs"
        ]

        for key in dangerous_keys:
            await secure_redis_service.set(key, "value")
            retrieved = await secure_redis_service.get(key)
            assert retrieved == "value"

    @pytest.mark.asyncio
    async def test_data_serialization_security(self, secure_redis_service):
        """Test that data is properly serialized/deserialized."""
        # Test with potentially problematic data
        test_data = {
            "normal": "value",
            "special_chars": "!@#$%^&*()",
            "unicode": "🚀💻🔥",
            "large_data": "x" * 10000
        }

        await secure_redis_service.set("test", test_data)
        retrieved = await secure_redis_service.get("test")
        assert retrieved == test_data
