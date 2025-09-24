"""Tests for forex news API endpoints."""

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from datetime import date, datetime

from app.main import app
from app.models.forex_news import ForexNewsCreate, ForexNewsUpdate
from app.core.exceptions import ValidationError
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
async def test_create_forex_news_validation_error():
    """Test forex news creation with validation error."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            invalid_data = {
                "currency": "INVALID",  # Invalid currency
                "event": "",  # Empty event
                "impact_level": "invalid"  # Invalid impact level
            }

            # Act
            response = await client.post("/api/v1/forex-news/", json=invalid_data)

            # Assert
            assert response.status_code == 422
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


@pytest.mark.asyncio
async def test_get_forex_news_by_date_range():
    """Test getting forex news by date range."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            start_date = "2024-01-01"
            end_date = "2024-01-31"
            news_items = [ForexNewsModelFactory.build() for _ in range(3)]

            with patch('app.services.forex_service.ForexService.get_forex_news_by_date_range') as mock_service:
                mock_service.return_value = news_items

                # Act
                response = await client.get(
                    f"/api/v1/forex-news/by-date-range/?start_date={start_date}&end_date={end_date}"
                )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 3
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_forex_news_by_currency():
    """Test getting forex news by currency."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            currency = "USD"
            news_items = [ForexNewsModelFactory.build(currency=currency) for _ in range(2)]

            with patch('app.services.forex_service.ForexService.get_news_by_currency') as mock_service:
                mock_service.return_value = news_items

                # Act
                response = await client.get(f"/api/v1/forex-news/by-currency/{currency}")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
            assert all(item["currency"] == currency for item in data)
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_forex_news_by_impact_level():
    """Test getting forex news by impact level."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            impact_level = "high"
            news_items = [ForexNewsModelFactory.build(impact_level=impact_level) for _ in range(2)]

            with patch('app.services.forex_service.ForexService.get_news_by_impact_level') as mock_service:
                mock_service.return_value = news_items

                # Act
                response = await client.get(f"/api/v1/forex-news/by-impact/{impact_level}")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
            assert all(item["impact_level"] == impact_level for item in data)
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_todays_forex_news():
    """Test getting today's forex news."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            news_items = [ForexNewsModelFactory.build() for _ in range(4)]

            with patch('app.services.forex_service.ForexService.get_todays_forex_news') as mock_service:
                mock_service.return_value = news_items

                # Act
                response = await client.get("/api/v1/forex-news/today/")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 4
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_upcoming_events():
    """Test getting upcoming forex events."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            news_items = [ForexNewsModelFactory.build() for _ in range(3)]

            # Mock the database dependency
            with patch('app.database.connection.get_database') as mock_get_db:
                from unittest.mock import AsyncMock

                # Create a mock database session
                mock_session = AsyncMock()
                async def mock_db_generator():
                    yield mock_session
                mock_get_db.return_value = mock_db_generator()

                with patch('app.services.forex_service.ForexService.get_upcoming_events') as mock_service:
                    mock_service.return_value = news_items

                    # Act
                    response = await client.get("/api/v1/forex-news/upcoming?hours=24")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 3
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_update_forex_news_success():
    """Test successful forex news update."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_model = ForexNewsModelFactory.build()
            update_data = {"analysis": "Updated analysis"}

            with patch('app.services.forex_service.ForexService.update') as mock_service:
                mock_service.return_value = sample_model

                # Act
                response = await client.put(
                    f"/api/v1/forex-news/{sample_model.id}",
                    json=update_data
                )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == sample_model.id
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_update_forex_news_not_found():
    """Test forex news update when not found."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            update_data = {"analysis": "Updated analysis"}

            with patch('app.services.forex_service.ForexService.update') as mock_service:
                mock_service.side_effect = ValidationError("Forex news not found")

                # Act
                response = await client.put("/api/v1/forex-news/999", json=update_data)

            # Assert
            assert response.status_code == 400
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_delete_forex_news_success():
    """Test successful forex news deletion."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.services.forex_service.ForexService.delete') as mock_service:
                mock_service.return_value = True

                # Act
                response = await client.delete("/api/v1/forex-news/1")

            # Assert
            assert response.status_code == 204
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_delete_forex_news_not_found():
    """Test forex news deletion when not found."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.services.forex_service.ForexService.delete') as mock_service:
                mock_service.side_effect = ValidationError("Forex news not found")

                # Act
                response = await client.delete("/api/v1/forex-news/999")

            # Assert
            assert response.status_code == 400
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_forex_news_with_pagination():
    """Test getting forex news with pagination."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            news_items = [ForexNewsModelFactory.build() for _ in range(5)]

            with patch('app.services.forex_service.ForexService.get_all') as mock_service:
                mock_service.return_value = news_items

                # Act
                response = await client.get("/api/v1/forex-news/?skip=0&limit=10")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 5
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_forex_news_with_filters():
    """Test getting forex news with multiple filters."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            news_items = [ForexNewsModelFactory.build() for _ in range(2)]

            with patch('app.services.forex_service.ForexService.get_forex_news_with_filters') as mock_service:
                mock_service.return_value = news_items

                # Act
                response = await client.get(
                    "/api/v1/forex-news/filtered/?currency=USD&impact_level=high"
                )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_bulk_create_forex_news():
    """Test bulk creation of forex news."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            news_data_list = [ForexNewsCreateFactory.build() for _ in range(3)]
            created_models = [ForexNewsModelFactory.build() for _ in range(3)]

            with patch('app.services.forex_service.ForexService.bulk_create') as mock_service:
                mock_service.return_value = created_models

                # Act
                response = await client.post(
                    "/api/v1/forex-news/bulk/",
                    json=[item.model_dump(mode='json') for item in news_data_list]
                )

            # Assert
            assert response.status_code == 201
            data = response.json()
            assert len(data) == 3
    finally:
        # Clean up
        await db_manager.close()
