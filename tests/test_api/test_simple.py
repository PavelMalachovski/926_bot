"""Simple API test without complex fixtures."""

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint():
    """Test health endpoint without async."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_forex_news_endpoint_simple():
    """Test forex news endpoint with simple async client."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Mock the entire service method directly
            with patch('app.services.forex_service.ForexService.get') as mock_get:
                from tests.factories import ForexNewsModelFactory

                # Create a real ForexNewsModel instance
                news_instance = ForexNewsModelFactory.build()
                # Mock the get method to return the actual instance
                mock_get.return_value = news_instance

                # Test the endpoint
                response = await client.get("/api/v1/forex-news/1")

                # Assert
                assert response.status_code == 200
                data = response.json()
                assert "id" in data
    finally:
        # Clean up
        await db_manager.close()
