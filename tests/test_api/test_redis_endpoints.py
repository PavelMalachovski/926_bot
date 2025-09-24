"""Comprehensive tests for Redis API endpoints using Context7 best practices."""

import pytest
import json
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services.cache_service import cache_service


@pytest.mark.integration
@pytest.mark.redis
class TestRedisEndpoints:
    """Test Redis management endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    @pytest.fixture
    async def async_client(self):
        """Create async test client."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    def test_get_redis_stats(self, client):
        """Test getting Redis statistics."""
        with patch.object(cache_service, 'get_stats', return_value={
            "status": "healthy",
            "connected_clients": 1,
            "used_memory": "1MB",
            "keyspace_hits": 100,
            "keyspace_misses": 50
        }):
            response = client.get("/api/v1/redis/stats")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert "connected_clients" in data
            assert "used_memory" in data

    def test_get_redis_stats_error(self, client):
        """Test Redis stats endpoint with error."""
        with patch.object(cache_service, 'get_stats', side_effect=Exception("Redis error")):
            response = client.get("/api/v1/redis/stats")

            assert response.status_code == 500
            data = response.json()
            assert "detail" in data

    def test_redis_health_check(self, client):
        """Test Redis health check."""
        with patch.object(cache_service, '_initialized', True), \
             patch.object(cache_service, 'redis_client') as mock_client:
            mock_client.ping = AsyncMock(return_value=True)

            response = client.get("/api/v1/redis/health")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert data["initialized"] is True

    def test_redis_health_check_unhealthy(self, client):
        """Test Redis health check when unhealthy."""
        with patch.object(cache_service, '_initialized', False):
            response = client.get("/api/v1/redis/health")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "unhealthy"

    def test_invalidate_cache_pattern(self, client):
        """Test cache pattern invalidation."""
        with patch.object(cache_service, 'invalidate_pattern', return_value=5):
            response = client.post("/api/v1/redis/cache/invalidate?pattern=test:*")

            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 5
            assert data["pattern"] == "test:*"

    def test_invalidate_cache_pattern_error(self, client):
        """Test cache pattern invalidation with error."""
        with patch.object(cache_service, 'invalidate_pattern', side_effect=Exception("Redis error")):
            response = client.post("/api/v1/redis/cache/invalidate?pattern=test:*")

            assert response.status_code == 500
            data = response.json()
            assert "detail" in data

    def test_get_cache_hit_ratio(self, client):
        """Test getting cache hit ratio."""
        with patch.object(cache_service, 'get_hit_ratio', return_value=0.8):
            response = client.get("/api/v1/redis/cache/hit-ratio")

            assert response.status_code == 200
            data = response.json()
            assert data["hit_ratio"] == 0.8
            assert data["percentage"] == 80.0

    def test_get_cache_hit_ratio_error(self, client):
        """Test cache hit ratio endpoint with error."""
        with patch.object(cache_service, 'get_hit_ratio', side_effect=Exception("Redis error")):
            response = client.get("/api/v1/redis/cache/hit-ratio")

            assert response.status_code == 500
            data = response.json()
            assert "detail" in data

    def test_check_rate_limit(self, client):
        """Test rate limit checking."""
        with patch.object(cache_service, 'rate_limiter') as mock_limiter:
            mock_limiter.is_allowed = AsyncMock(return_value=(True, {
                "limit": 100,
                "remaining": 95,
                "reset_time": 1234567890,
                "current_count": 5
            }))

            response = client.post("/api/v1/redis/rate-limit/check?key=test_user&limit=100&window=3600")

            assert response.status_code == 200
            data = response.json()
            assert data["allowed"] is True
            assert data["rate_limit_info"]["limit"] == 100
            assert data["rate_limit_info"]["remaining"] == 95

    def test_check_rate_limit_exceeded(self, client):
        """Test rate limit checking when exceeded."""
        with patch.object(cache_service, 'rate_limiter') as mock_limiter:
            mock_limiter.is_allowed = AsyncMock(return_value=(False, {
                "limit": 100,
                "remaining": 0,
                "reset_time": 1234567890,
                "current_count": 100
            }))

            response = client.post("/api/v1/redis/rate-limit/check?key=test_user&limit=100&window=3600")

            assert response.status_code == 429
            data = response.json()
            assert data["allowed"] is False
            assert data["rate_limit_info"]["remaining"] == 0

    def test_check_rate_limit_no_limiter(self, client):
        """Test rate limit checking when limiter not available."""
        with patch.object(cache_service, 'rate_limiter', None):
            response = client.post("/api/v1/redis/rate-limit/check?key=test_user&limit=100&window=3600")

            assert response.status_code == 503
            data = response.json()
            assert "detail" in data

    def test_publish_message(self, client):
        """Test publishing a message."""
        with patch.object(cache_service, 'pubsub_service') as mock_pubsub:
            mock_pubsub.publish = AsyncMock(return_value=3)

            message_data = {"message": "test", "data": "value"}
            response = client.post(
                "/api/v1/redis/pubsub/publish?channel=test_channel",
                json=message_data
            )

            assert response.status_code == 200
            data = response.json()
            assert data["subscribers_notified"] == 3
            assert data["channel"] == "test_channel"

    def test_publish_message_no_pubsub(self, client):
        """Test publishing when Pub/Sub not available."""
        with patch.object(cache_service, 'pubsub_service', None):
            message_data = {"message": "test"}
            response = client.post(
                "/api/v1/redis/pubsub/publish?channel=test_channel",
                json=message_data
            )

            assert response.status_code == 503
            data = response.json()
            assert "detail" in data

    def test_get_session(self, client):
        """Test getting session data."""
        with patch.object(cache_service, 'session_manager') as mock_manager:
            mock_manager.get_session = AsyncMock(return_value={"user_id": 123, "role": "user"})

            response = client.get("/api/v1/redis/session/test_session_123")

            assert response.status_code == 200
            data = response.json()
            assert data["session_id"] == "test_session_123"
            assert data["data"]["user_id"] == 123

    def test_get_session_not_found(self, client):
        """Test getting non-existent session."""
        with patch.object(cache_service, 'session_manager') as mock_manager:
            mock_manager.get_session = AsyncMock(return_value=None)

            response = client.get("/api/v1/redis/session/nonexistent_session")

            assert response.status_code == 404
            data = response.json()
            assert "detail" in data

    def test_get_session_no_manager(self, client):
        """Test getting session when manager not available."""
        with patch.object(cache_service, 'session_manager', None):
            response = client.get("/api/v1/redis/session/test_session")

            assert response.status_code == 503
            data = response.json()
            assert "detail" in data

    def test_delete_session(self, client):
        """Test deleting session."""
        with patch.object(cache_service, 'session_manager') as mock_manager:
            mock_manager.delete_session = AsyncMock(return_value=True)

            response = client.delete("/api/v1/redis/session/test_session_123")

            assert response.status_code == 200
            data = response.json()
            assert "deleted successfully" in data["message"]

    def test_delete_session_not_found(self, client):
        """Test deleting non-existent session."""
        with patch.object(cache_service, 'session_manager') as mock_manager:
            mock_manager.delete_session = AsyncMock(return_value=False)

            response = client.delete("/api/v1/redis/session/nonexistent_session")

            assert response.status_code == 404
            data = response.json()
            assert "detail" in data

    def test_delete_session_no_manager(self, client):
        """Test deleting session when manager not available."""
        with patch.object(cache_service, 'session_manager', None):
            response = client.delete("/api/v1/redis/session/test_session")

            assert response.status_code == 503
            data = response.json()
            assert "detail" in data


@pytest.mark.integration
@pytest.mark.redis
class TestRedisEndpointValidation:
    """Test Redis endpoint validation and error handling."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_invalidate_pattern_validation(self, client):
        """Test cache pattern validation."""
        # Test missing pattern parameter
        response = client.post("/api/v1/redis/cache/invalidate")
        assert response.status_code == 422

        # Test empty pattern
        response = client.post("/api/v1/redis/cache/invalidate?pattern=")
        assert response.status_code == 422

    def test_rate_limit_validation(self, client):
        """Test rate limit parameter validation."""
        # Test missing required parameters
        response = client.post("/api/v1/redis/rate-limit/check")
        assert response.status_code == 422

        # Test invalid limit (negative)
        response = client.post("/api/v1/redis/rate-limit/check?key=test&limit=-1&window=60")
        assert response.status_code == 422

        # Test invalid window (zero)
        response = client.post("/api/v1/redis/rate-limit/check?key=test&limit=100&window=0")
        assert response.status_code == 422

    def test_publish_message_validation(self, client):
        """Test message publishing validation."""
        # Test missing channel parameter
        response = client.post("/api/v1/redis/pubsub/publish", json={"message": "test"})
        assert response.status_code == 422

        # Test empty channel
        response = client.post("/api/v1/redis/pubsub/publish?channel=", json={"message": "test"})
        assert response.status_code == 422

        # Test missing message body
        response = client.post("/api/v1/redis/pubsub/publish?channel=test")
        assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.redis
@pytest.mark.slow
class TestRedisEndpointPerformance:
    """Test Redis endpoint performance."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_stats_endpoint_performance(self, client):
        """Test stats endpoint performance."""
        with patch.object(cache_service, 'get_stats', return_value={"status": "healthy"}):
            import time
            start_time = time.time()

            # Make multiple requests
            for _ in range(100):
                response = client.get("/api/v1/redis/stats")
                assert response.status_code == 200

            end_time = time.time()
            duration = end_time - start_time

            # Should complete within reasonable time
            assert duration < 5.0  # Adjust based on your requirements

    def test_health_check_performance(self, client):
        """Test health check endpoint performance."""
        with patch.object(cache_service, '_initialized', True), \
             patch.object(cache_service, 'redis_client') as mock_client:
            mock_client.ping = AsyncMock(return_value=True)

            import time
            start_time = time.time()

            # Make multiple requests
            for _ in range(50):
                response = client.get("/api/v1/redis/health")
                assert response.status_code == 200

            end_time = time.time()
            duration = end_time - start_time

            # Should complete within reasonable time
            assert duration < 3.0  # Adjust based on your requirements


@pytest.mark.integration
@pytest.mark.redis
class TestRedisEndpointSecurity:
    """Test Redis endpoint security."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_pattern_injection_protection(self, client):
        """Test protection against pattern injection."""
        malicious_patterns = [
            "test*; DROP TABLE users; --",
            "test* | cat /etc/passwd",
            "test* && rm -rf /",
            "test* || echo 'hacked'"
        ]

        with patch.object(cache_service, 'invalidate_pattern', return_value=0):
            for pattern in malicious_patterns:
                response = client.post(f"/api/v1/redis/cache/invalidate?pattern={pattern}")
                # Should not cause server errors
                assert response.status_code in [200, 400, 422]

    def test_rate_limit_key_validation(self, client):
        """Test rate limit key validation."""
        malicious_keys = [
            "../../etc/passwd",
            "key; DROP TABLE users; --",
            "key | cat /etc/passwd",
            "key && rm -rf /"
        ]

        with patch.object(cache_service, 'rate_limiter') as mock_limiter:
            mock_limiter.is_allowed = AsyncMock(return_value=(True, {"limit": 100, "remaining": 99}))

            for key in malicious_keys:
                response = client.post(f"/api/v1/redis/rate-limit/check?key={key}&limit=100&window=60")
                # Should not cause server errors
                assert response.status_code in [200, 400, 422]

    def test_session_id_validation(self, client):
        """Test session ID validation."""
        malicious_session_ids = [
            "../../etc/passwd",
            "session; DROP TABLE users; --",
            "session | cat /etc/passwd",
            "session && rm -rf /"
        ]

        with patch.object(cache_service, 'session_manager') as mock_manager:
            mock_manager.get_session = AsyncMock(return_value=None)

            for session_id in malicious_session_ids:
                response = client.get(f"/api/v1/redis/session/{session_id}")
                # Should not cause server errors
                assert response.status_code in [404, 400, 422]


@pytest.mark.integration
@pytest.mark.redis
class TestRedisEndpointIntegration:
    """Test Redis endpoint integration scenarios."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_complete_redis_workflow(self, client):
        """Test complete Redis workflow."""
        # 1. Check Redis health
        with patch.object(cache_service, '_initialized', True), \
             patch.object(cache_service, 'redis_client') as mock_client:
            mock_client.ping = AsyncMock(return_value=True)

            response = client.get("/api/v1/redis/health")
            assert response.status_code == 200

        # 2. Get Redis stats
        with patch.object(cache_service, 'get_stats', return_value={"status": "healthy"}):
            response = client.get("/api/v1/redis/stats")
            assert response.status_code == 200

        # 3. Check rate limit
        with patch.object(cache_service, 'rate_limiter') as mock_limiter:
            mock_limiter.is_allowed = AsyncMock(return_value=(True, {"limit": 100, "remaining": 99}))

            response = client.post("/api/v1/redis/rate-limit/check?key=test_user&limit=100&window=60")
            assert response.status_code == 200

        # 4. Publish message
        with patch.object(cache_service, 'pubsub_service') as mock_pubsub:
            mock_pubsub.publish = AsyncMock(return_value=1)

            response = client.post(
                "/api/v1/redis/pubsub/publish?channel=test",
                json={"message": "test"}
            )
            assert response.status_code == 200

        # 5. Create and manage session
        with patch.object(cache_service, 'session_manager') as mock_manager:
            mock_manager.create_session = AsyncMock(return_value=True)
            mock_manager.get_session = AsyncMock(return_value={"user_id": 123})
            mock_manager.delete_session = AsyncMock(return_value=True)

            # Create session (would need a create endpoint)
            # Get session
            response = client.get("/api/v1/redis/session/test_session")
            assert response.status_code == 200

            # Delete session
            response = client.delete("/api/v1/redis/session/test_session")
            assert response.status_code == 200

        # 6. Invalidate cache
        with patch.object(cache_service, 'invalidate_pattern', return_value=5):
            response = client.post("/api/v1/redis/cache/invalidate?pattern=test:*")
            assert response.status_code == 200

    def test_error_recovery_scenarios(self, client):
        """Test error recovery scenarios."""
        # Test Redis connection failure
        with patch.object(cache_service, '_initialized', False):
            response = client.get("/api/v1/redis/health")
            assert response.status_code == 503

        # Test Redis service unavailable
        with patch.object(cache_service, 'get_stats', side_effect=Exception("Redis unavailable")):
            response = client.get("/api/v1/redis/stats")
            assert response.status_code == 500

        # Test rate limiter unavailable
        with patch.object(cache_service, 'rate_limiter', None):
            response = client.post("/api/v1/redis/rate-limit/check?key=test&limit=100&window=60")
            assert response.status_code == 503

        # Test Pub/Sub unavailable
        with patch.object(cache_service, 'pubsub_service', None):
            response = client.post(
                "/api/v1/redis/pubsub/publish?channel=test",
                json={"message": "test"}
            )
            assert response.status_code == 503

        # Test session manager unavailable
        with patch.object(cache_service, 'session_manager', None):
            response = client.get("/api/v1/redis/session/test")
            assert response.status_code == 503
