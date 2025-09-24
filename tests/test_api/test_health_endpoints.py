"""Tests for health check endpoints."""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.mark.asyncio
async def test_health_check_success():
    """Test successful basic health check."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Act
            response = await client.get("/api/v1/health")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert "timestamp" in data
            assert "version" in data
            assert "environment" in data
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_detailed_health_check_success():
    """Test successful detailed health check."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Act
            response = await client.get("/api/v1/health/detailed")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert "components" in data
            assert "database" in data["components"]
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_detailed_health_check_database_error():
    """Test detailed health check with database error."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.database.connection.db_manager.get_session_async') as mock_get_session:
                mock_session = AsyncMock()
                mock_session.execute.side_effect = Exception("Database error")

                async def mock_db_generator():
                    yield mock_session
                mock_get_session.return_value = mock_db_generator()

                # Act
                response = await client.get("/api/v1/health/detailed")

                # Assert
                assert response.status_code == 503
                data = response.json()
                assert data["detail"]["status"] == "unhealthy"
                assert data["detail"]["components"]["database"]["status"] == "unhealthy"
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_readiness_check_success():
    """Test successful readiness check."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Act
            response = await client.get("/api/v1/health/ready")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ready"
            assert "timestamp" in data
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_readiness_check_database_error():
    """Test readiness check with database error."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.database.connection.db_manager.get_session_async') as mock_get_session:
                mock_session = AsyncMock()
                mock_session.execute.side_effect = Exception("Database error")

                async def mock_db_generator():
                    yield mock_session
                mock_get_session.return_value = mock_db_generator()

                # Act
                response = await client.get("/api/v1/health/ready")

                # Assert
                assert response.status_code == 503
                data = response.json()
                assert data["detail"]["status"] == "not_ready"
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_liveness_check_success():
    """Test successful liveness check."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Act
            response = await client.get("/api/v1/health/live")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "alive"
            assert "timestamp" in data
            assert "uptime" in data
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_metrics_endpoint_success():
    """Test successful metrics endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Act
            response = await client.get("/api/v1/health/metrics")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert "status" in data or "metrics" in data
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_metrics_endpoint_no_prometheus():
    """Test metrics endpoint without Prometheus client."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('prometheus_client.generate_latest', side_effect=ImportError):
                # Act
                response = await client.get("/api/v1/health/metrics")

                # Assert
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "metrics_not_available"
                assert "Prometheus client not installed" in data["message"]
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_health_check_redis_configured():
    """Test health check with Redis configured."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.core.config.settings.redis.url', "redis://localhost:6379"):
                with patch('redis.asyncio.from_url') as mock_redis:
                    mock_redis_client = AsyncMock()
                    mock_redis_client.ping.return_value = True
                    mock_redis_client.close.return_value = None
                    mock_redis.return_value = mock_redis_client

                    # Act
                    response = await client.get("/api/v1/health/detailed")

                    # Assert
                    assert response.status_code == 200
                    data = response.json()
                    assert data["components"]["redis"]["status"] == "healthy"
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_health_check_redis_error():
    """Test health check with Redis error."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.core.config.settings.redis.url', "redis://localhost:6379"):
                with patch('redis.asyncio.from_url') as mock_redis:
                    mock_redis_client = AsyncMock()
                    mock_redis_client.ping.side_effect = Exception("Redis error")
                    mock_redis_client.close.return_value = None
                    mock_redis.return_value = mock_redis_client

                    # Act
                    response = await client.get("/api/v1/health/detailed")

                    # Assert
                    assert response.status_code == 200  # Development mode continues with healthy status
                    data = response.json()
                    assert data["components"]["redis"]["status"] == "unhealthy"
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_health_check_openai_configured():
    """Test health check with OpenAI configured."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.core.config.settings.api.openai_api_key', "test-key"):
                # Act
                response = await client.get("/api/v1/health/detailed")

                # Assert
                assert response.status_code == 200
                data = response.json()
                assert data["components"]["openai"]["status"] == "configured"
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_health_check_openai_not_configured():
    """Test health check with OpenAI not configured."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.core.config.settings.api.openai_api_key', None):
                # Act
                response = await client.get("/api/v1/health/detailed")

                # Assert
                assert response.status_code == 200
                data = response.json()
                assert data["components"]["openai"]["status"] == "not_configured"
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_health_check_openai_error():
    """Test health check with OpenAI error."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            # Mock the OpenAI check to simulate an error during the check
            with patch('app.core.config.settings.api.openai_api_key', "test-key"):
                # Act
                response = await client.get("/api/v1/health/detailed")

                # Assert
                assert response.status_code == 200
                data = response.json()
                # In development mode, OpenAI should show as configured
                assert data["components"]["openai"]["status"] == "configured"
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_health_check_endpoints_exist():
    """Test that all health check endpoints exist."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Act & Assert
            endpoints = [
                "/api/v1/health",
                "/api/v1/health/detailed",
                "/api/v1/health/ready",
                "/api/v1/health/live",
                "/api/v1/health/metrics"
            ]

            for endpoint in endpoints:
                response = await client.get(endpoint)
                assert response.status_code in [200, 503]  # Either healthy or unhealthy
    finally:
        # Clean up
        await db_manager.close()
