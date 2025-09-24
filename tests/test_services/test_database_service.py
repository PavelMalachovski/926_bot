"""Tests for database service."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from datetime import datetime, date
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.database_service import DatabaseService
from app.core.exceptions import DatabaseError, ValidationError
from app.database.models import UserModel, ForexNewsModel
from tests.factories import UserCreateFactory, ForexNewsCreateFactory


class TestDatabaseService:
    """Test cases for DatabaseService."""

    @pytest.fixture
    def database_service(self):
        """Create database service instance."""
        return DatabaseService()

    @pytest.fixture
    def mock_db_session(self):
        """Create mock database session."""
        return AsyncMock(spec=AsyncSession)

    @pytest.mark.asyncio
    async def test_create_user_success(self, database_service, mock_db_session):
        """Test successful user creation."""
        # Arrange
        user_data = UserCreateFactory.build()
        mock_db_session.add = AsyncMock()
        mock_db_session.commit = AsyncMock()
        mock_db_session.refresh = AsyncMock()

        # Act
        result = await database_service.create_user(mock_db_session, user_data)

        # Assert
        assert result is not None
        mock_db_session.add.assert_called_once()
        mock_db_session.commit.assert_called_once()
        mock_db_session.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_user_database_error(self, database_service, mock_db_session):
        """Test user creation with database error."""
        # Arrange
        user_data = UserCreateFactory.build()
        mock_db_session.add = AsyncMock()
        mock_db_session.commit = AsyncMock(side_effect=Exception("Database error"))
        mock_db_session.rollback = AsyncMock()

        # Act & Assert
        with pytest.raises(DatabaseError):
            await database_service.create_user(mock_db_session, user_data)

        mock_db_session.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id_success(self, database_service, mock_db_session):
        """Test successful user retrieval by Telegram ID."""
        # Arrange
        telegram_id = 123456789
        mock_user = UserModel()
        mock_user.id = 1
        mock_user.telegram_id = telegram_id
        mock_user.preferred_currencies = ["USD"]
        mock_user.impact_levels = ["high"]
        mock_user.analysis_required = True
        mock_user.digest_time = "08:00:00"
        mock_user.created_at = datetime.now()
        mock_user.updated_at = datetime.now()

        # Mock the database calls
        mock_db_session.execute = AsyncMock()
        mock_db_session.get = AsyncMock(return_value=mock_user)

        # Mock the column check result
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(return_value=[
            ('notifications_enabled',),
            ('notification_minutes',),
            ('notification_impact_levels',),
            ('charts_enabled',),
            ('chart_type',),
            ('chart_window_hours',),
            ('timezone',)
        ])
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await database_service.get_user_by_telegram_id(mock_db_session, telegram_id)

        # Assert
        assert result == mock_user
        mock_db_session.execute.assert_called_once()
        mock_db_session.get.assert_called_once_with(UserModel, telegram_id)

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id_not_found(self, database_service, mock_db_session):
        """Test user retrieval when user not found."""
        # Arrange
        telegram_id = 999999999

        # Mock the database calls
        mock_db_session.execute = AsyncMock()
        mock_db_session.get = AsyncMock(return_value=None)

        # Mock the column check result
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(return_value=[
            ('notifications_enabled',),
            ('notification_minutes',),
            ('notification_impact_levels',),
            ('charts_enabled',),
            ('chart_type',),
            ('chart_window_hours',),
            ('timezone',)
        ])
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await database_service.get_user_by_telegram_id(mock_db_session, telegram_id)

        # Assert
        assert result is None
        mock_db_session.execute.assert_called_once()
        mock_db_session.get.assert_called_once_with(UserModel, telegram_id)

    @pytest.mark.asyncio
    async def test_update_user_success(self, database_service, mock_db_session):
        """Test successful user update."""
        # Arrange
        telegram_id = 123456789
        update_data = {"first_name": "Updated Name"}
        mock_user = UserModel()
        mock_user.id = 1
        mock_user.telegram_id = telegram_id
        mock_user.first_name = "Original Name"
        mock_user.preferred_currencies = ["USD"]
        mock_user.impact_levels = ["high"]
        mock_user.analysis_required = True
        mock_user.digest_time = "08:00:00"
        mock_user.created_at = datetime.now()
        mock_user.updated_at = datetime.now()

        # Mock the get_user_by_telegram_id call
        with patch.object(database_service, 'get_user_by_telegram_id', return_value=mock_user) as mock_get_user:
            mock_db_session.commit = AsyncMock()
            mock_db_session.refresh = AsyncMock()

            # Act
            result = await database_service.update_user(mock_db_session, telegram_id, update_data)

            # Assert
            assert result == mock_user
            mock_get_user.assert_called_once_with(mock_db_session, telegram_id)
            mock_db_session.commit.assert_called_once()
            mock_db_session.refresh.assert_called_once_with(mock_user)

    @pytest.mark.asyncio
    async def test_update_user_not_found(self, database_service, mock_db_session):
        """Test user update when user not found."""
        # Arrange
        telegram_id = 999999999
        update_data = {"first_name": "Updated Name"}

        # Mock get_user_by_telegram_id to return None
        with patch.object(database_service, 'get_user_by_telegram_id', return_value=None):
            # Act & Assert
            with pytest.raises(ValidationError):
                await database_service.update_user(mock_db_session, telegram_id, update_data)

    @pytest.mark.asyncio
    async def test_create_forex_news_success(self, database_service, mock_db_session):
        """Test successful forex news creation."""
        # Arrange
        news_data = ForexNewsCreateFactory.build()
        mock_db_session.add = AsyncMock()
        mock_db_session.commit = AsyncMock()
        mock_db_session.refresh = AsyncMock()

        # Act
        result = await database_service.create_forex_news(mock_db_session, news_data)

        # Assert
        assert result is not None
        mock_db_session.add.assert_called_once()
        mock_db_session.commit.assert_called_once()
        mock_db_session.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_forex_news_by_date_success(self, database_service, mock_db_session):
        """Test successful forex news retrieval by date."""
        # Arrange
        target_date = date(2024, 1, 15)
        # Create mock row data that can be subscripted (12 fields total)
        mock_rows = [
            (1, date(2024, 1, 15), "08:00:00", "USD", "Test Event 1", "1.2", "1.1", "1.0", "High", "Analysis 1", datetime.now(), datetime.now()),
            (2, date(2024, 1, 15), "09:00:00", "EUR", "Test Event 2", "1.3", "1.2", "1.1", "Medium", "Analysis 2", datetime.now(), datetime.now()),
            (3, date(2024, 1, 15), "10:00:00", "GBP", "Test Event 3", "1.4", "1.3", "1.2", "Low", "Analysis 3", datetime.now(), datetime.now())
        ]

        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(return_value=mock_rows)
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await database_service.get_forex_news_by_date(mock_db_session, target_date)

        # Assert
        assert len(result) == 3
        assert all(isinstance(news, ForexNewsModel) for news in result)
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_forex_news_by_currency_success(self, database_service, mock_db_session):
        """Test successful forex news retrieval by currency."""
        # Arrange
        currency = "USD"
        # Create mock row data that can be subscripted (12 fields total)
        mock_rows = [
            (1, date(2024, 1, 15), "08:00:00", currency, "Test Event 1", "1.2", "1.1", "1.0", "High", "Analysis 1", datetime.now(), datetime.now()),
            (2, date(2024, 1, 15), "09:00:00", currency, "Test Event 2", "1.3", "1.2", "1.1", "Medium", "Analysis 2", datetime.now(), datetime.now())
        ]

        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(return_value=mock_rows)
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await database_service.get_forex_news_by_currency(mock_db_session, currency)

        # Assert
        assert len(result) == 2
        assert all(isinstance(news, ForexNewsModel) for news in result)
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_users_by_currency_success(self, database_service, mock_db_session):
        """Test successful user retrieval by currency preference."""
        # Arrange
        currency = "USD"
        # Create mock row data that can be subscripted
        mock_rows = [
            (1, 123456789, [currency], ["high"], True, "08:00:00", datetime.now(), datetime.now()),
            (2, 123456790, [currency], ["medium"], False, "09:00:00", datetime.now(), datetime.now()),
            (3, 123456791, [currency], ["low"], True, "10:00:00", datetime.now(), datetime.now())
        ]

        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(return_value=mock_rows)
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await database_service.get_users_by_currency(mock_db_session, currency)

        # Assert
        assert len(result) == 3
        assert all(isinstance(user, UserModel) for user in result)
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_users_by_impact_level_success(self, database_service, mock_db_session):
        """Test successful user retrieval by impact level preference."""
        # Arrange
        impact_level = "high"
        # Create mock row data that can be subscripted
        mock_rows = [
            (1, 123456789, ["USD"], [impact_level], True, "08:00:00", datetime.now(), datetime.now()),
            (2, 123456790, ["EUR"], [impact_level], False, "09:00:00", datetime.now(), datetime.now())
        ]

        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(return_value=mock_rows)
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await database_service.get_users_by_impact_level(mock_db_session, impact_level)

        # Assert
        assert len(result) == 2
        assert all(isinstance(user, UserModel) for user in result)
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_database_error_handling(self, database_service, mock_db_session):
        """Test database error handling."""
        # Arrange
        mock_db_session.execute = AsyncMock(side_effect=Exception("Database connection error"))

        # Act
        result = await database_service.get_user_by_telegram_id(mock_db_session, 123456789)

        # Assert - should return None on error, not raise exception
        assert result is None

    @pytest.mark.asyncio
    async def test_validation_error_handling(self, database_service, mock_db_session):
        """Test validation error handling."""
        # Arrange
        invalid_user_data = UserCreateFactory.build()
        # Simulate validation error during commit
        mock_db_session.add = AsyncMock()
        mock_db_session.commit = AsyncMock(side_effect=ValidationError("Invalid data"))
        mock_db_session.rollback = AsyncMock()

        # Act & Assert
        with pytest.raises(DatabaseError):
            await database_service.create_user(mock_db_session, invalid_user_data)
