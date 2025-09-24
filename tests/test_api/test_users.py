"""Tests for user API endpoints."""

import pytest
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.database.models import UserModel
from app.models.user import UserCreate, UserUpdate, UserPreferences
from app.core.exceptions import ValidationError
from tests.factories import UserCreateFactory, UserModelFactory


@pytest.mark.asyncio
async def test_create_user():
    """Test user creation endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_user_data = UserCreateFactory.build()

            with patch('app.services.user_service.UserService.create') as mock_service:
                # Create a mock response that matches the input data
                mock_response = UserModelFactory.build()
                mock_response.telegram_id = sample_user_data.telegram_id
                mock_response.username = sample_user_data.username
                mock_response.first_name = sample_user_data.first_name
                mock_service.return_value = mock_response

                # Act
                response = await client.post("/api/v1/users/", json=sample_user_data.model_dump(mode='json'))

            # Assert
            assert response.status_code == 201
            data = response.json()
            assert data["telegram_id"] == sample_user_data.telegram_id
            assert data["username"] == sample_user_data.username
            assert data["first_name"] == sample_user_data.first_name
            assert "id" in data
            assert "created_at" in data
            assert "updated_at" in data
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_create_user_duplicate():
    """Test creating duplicate user."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_user_data = UserCreateFactory.build()

            with patch('app.services.user_service.UserService.create') as mock_service:
                # First call succeeds, second call raises ValidationError
                mock_response = UserModelFactory.build()
                mock_response.telegram_id = sample_user_data.telegram_id
                mock_service.side_effect = [mock_response, ValidationError("User already exists")]

                # Create first user
                await client.post("/api/v1/users/", json=sample_user_data.model_dump(mode='json'))

                # Try to create duplicate
                response = await client.post("/api/v1/users/", json=sample_user_data.model_dump(mode='json'))

            # Assert
            assert response.status_code == 400
            assert "already exists" in response.json()["detail"]
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_user():
    """Test get user endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_user_data = UserCreateFactory.build()
            mock_user = UserModelFactory.build()
            mock_user.telegram_id = sample_user_data.telegram_id

            with patch('app.services.user_service.UserService.get_by_telegram_id') as mock_service:
                mock_service.return_value = mock_user

                # Get user
                response = await client.get(f"/api/v1/users/{sample_user_data.telegram_id}")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["telegram_id"] == sample_user_data.telegram_id
            assert data["username"] == sample_user_data.username
            assert data["id"] == mock_user.id
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_user_not_found():
    """Test get non-existent user."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            with patch('app.services.user_service.UserService.get_by_telegram_id') as mock_service:
                mock_service.return_value = None

                # Act
                response = await client.get("/api/v1/users/999999999")

            # Assert
            assert response.status_code == 404
            assert "not found" in response.json()["detail"]
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_update_user():
    """Test user update endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_user_data = UserCreateFactory.build()
            mock_user = UserModelFactory.build()
            mock_user.telegram_id = sample_user_data.telegram_id
            mock_user.username = "updateduser"
            mock_user.first_name = "Updated"
            mock_user.is_premium = True

            update_data = {
                "username": "updateduser",
                "first_name": "Updated",
                "is_premium": True
            }

            with patch('app.services.user_service.UserService.update_user') as mock_service:
                mock_service.return_value = mock_user

                # Act
                response = await client.put(
                    f"/api/v1/users/{sample_user_data.telegram_id}",
                    json=update_data
                )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["username"] == "updateduser"
            assert data["first_name"] == "Updated"
            assert data["is_premium"] is True
            assert data["telegram_id"] == sample_user_data.telegram_id
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_update_user_not_found():
    """Test update non-existent user."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            update_data = {"username": "updateduser"}

            with patch('app.services.user_service.UserService.update_user') as mock_service:
                mock_service.side_effect = ValidationError("User not found")

                # Act
                response = await client.put("/api/v1/users/999999999", json=update_data)

                # Assert
                assert response.status_code == 400
            assert "not found" in response.json()["detail"]
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_update_user_preferences():
    """Test user preferences update endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_user_data = UserCreateFactory.build()
            mock_user = UserModelFactory.build()
            mock_user.telegram_id = sample_user_data.telegram_id
            mock_user.preferred_currencies = ["GBP", "JPY"]
            mock_user.impact_levels = ["high"]
            mock_user.notifications_enabled = True
            mock_user.notification_minutes = 15
            mock_user.charts_enabled = True
            mock_user.chart_type = "multi"

            preferences_data = {
                "preferred_currencies": ["GBP", "JPY"],
                "impact_levels": ["high"],
                "notifications_enabled": True,
                "notification_minutes": 15,
                "charts_enabled": True,
                "chart_type": "multi"
            }

            with patch('app.services.user_service.UserService.update_preferences') as mock_service:
                mock_service.return_value = mock_user

                # Act
                response = await client.put(
                    f"/api/v1/users/{sample_user_data.telegram_id}/preferences",
                    json=preferences_data
                )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["preferred_currencies"] == ["GBP", "JPY"]
            assert data["impact_levels"] == ["high"]
            assert data["notifications_enabled"] is True
            assert data["notification_minutes"] == 15
            assert data["charts_enabled"] is True
            assert data["chart_type"] == "multi"
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_users():
    """Test get users endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            mock_users = [UserModelFactory.build() for _ in range(3)]

            with patch('app.services.user_service.UserService.get_all') as mock_service:
                mock_service.return_value = mock_users

                # Act
                response = await client.get("/api/v1/users/")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 3
            assert all("telegram_id" in user for user in data)
            assert all("username" in user for user in data)
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_users_with_pagination():
    """Test get users with pagination."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            mock_users = [UserModelFactory.build() for _ in range(2)]

            with patch('app.services.user_service.UserService.get_all') as mock_service:
                mock_service.return_value = mock_users

                # Get first page
                response = await client.get("/api/v1/users/?skip=0&limit=2")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_users_by_currency():
    """Test get users by currency endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            mock_user = UserModelFactory.build()
            mock_user.telegram_id = 111111111

            with patch('app.services.user_service.UserService.get_users_by_currency') as mock_service:
                mock_service.return_value = [mock_user]

                # Act
                response = await client.get("/api/v1/users/by-currency/USD")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["telegram_id"] == 111111111
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_get_users_by_impact_level():
    """Test get users by impact level endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            mock_user = UserModelFactory.build()
            mock_user.telegram_id = 111111111

            with patch('app.services.user_service.UserService.get_users_by_impact_level') as mock_service:
                mock_service.return_value = [mock_user]

                # Act
                response = await client.get("/api/v1/users/by-impact/high")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["telegram_id"] == 111111111
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_update_user_activity():
    """Test update user activity endpoint."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_user_data = UserCreateFactory.build()

            with patch('app.services.user_service.UserService.update_user_activity') as mock_service:
                mock_service.return_value = True

                # Act
                response = await client.post(
                    f"/api/v1/users/{sample_user_data.telegram_id}/update-activity"
                )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert "Activity updated successfully" in data["message"]
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_create_user_invalid_data():
    """Test user creation with invalid data."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            invalid_data = {
                "telegram_id": "invalid",  # Should be integer
                "username": "testuser"
            }

            # Act
            response = await client.post("/api/v1/users/", json=invalid_data)

            # Assert
            assert response.status_code == 422  # Validation error
    finally:
        # Clean up
        await db_manager.close()


@pytest.mark.asyncio
async def test_update_user_invalid_preferences():
    """Test user update with invalid preferences."""
    # Initialize the database manager for testing
    from app.database.connection import db_manager
    await db_manager.initialize()

    try:
        # Create a simple async client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Arrange
            sample_user_data = UserCreateFactory.build()

            # Update with invalid preferences
            invalid_preferences = {
                "preferred_currencies": ["INVALID"],  # Invalid currency
                "notification_minutes": 45  # Invalid minutes
            }

            with patch('app.services.user_service.UserService.update_preferences') as mock_service:
                mock_service.side_effect = ValidationError("Invalid currency")

                # Act
                response = await client.put(
                    f"/api/v1/users/{sample_user_data.telegram_id}/preferences",
                    json=invalid_preferences
                )

            # Assert
            assert response.status_code == 422
            # FastAPI returns 422 for validation errors
            data = response.json()
            assert "detail" in data
    finally:
        # Clean up
        await db_manager.close()
