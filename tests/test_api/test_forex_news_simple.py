"""Simple tests for forex news API endpoints."""

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from app.main import app
from tests.factories import ForexNewsCreateFactory, ForexNewsModelFactory


@pytest.mark.asyncio
async def test_create_forex_news_success():
    """Test successful forex news creation via API."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_data = ForexNewsCreateFactory.build()

            with patch('app.services.forex_service.ForexService.create') as mock_service:
                # Create a mock response that matches the input data
                mock_response = ForexNewsModelFactory.build()
                mock_response.currency = sample_data.currency
                mock_response.event = sample_data.event
                mock_service.return_value = mock_response

                # Act
                response = await client.post(
                    "/api/v1/forex-news/",
                    json=sample_data.model_dump(mode='json')
                )

            # Assert
            assert response.status_code == 201
            data = response.json()
            assert "id" in data
            assert data["currency"] == sample_data.currency
            assert data["event"] == sample_data.event
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_forex_news_by_id_success():
    """Test successful forex news retrieval by ID."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_model = ForexNewsModelFactory.build()

            with patch('app.services.forex_service.ForexService.get') as mock_service:
                mock_service.return_value = sample_model

                # Act
                response = await client.get(f"/api/v1/forex-news/{sample_model.id}")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == sample_model.id
            assert data["currency"] == sample_model.currency
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_forex_news_by_id_not_found():
    """Test forex news retrieval when not found."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.services.forex_service.ForexService.get') as mock_service:
                mock_service.return_value = None

                # Act
                response = await client.get("/api/v1/forex-news/999")

            # Assert
            assert response.status_code == 404
    finally:
        # Clean up
        await db_manager.close()
