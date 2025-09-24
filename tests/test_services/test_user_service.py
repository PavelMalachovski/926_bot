"""Tests for UserService."""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.services.user_service import UserService
from app.database.models import UserModel
from app.models.user import UserCreate, UserUpdate, UserPreferences
from app.core.exceptions import DatabaseError, ValidationError
from tests.factories import UserCreateFactory, UserModelFactory, UserPreferencesFactory


@pytest.fixture
def user_service():
    """Create UserService instance."""
    return UserService()


@pytest.fixture
def mock_db_session():
    """Create mock database session."""
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def sample_user_data():
    """Sample user data for testing."""
    return UserCreateFactory.build()


@pytest.fixture
def sample_user_model():
    """Sample user model for testing."""
    return UserModelFactory.build()


class TestUserService:
    """Test cases for UserService."""

    @pytest.mark.asyncio
    async def test_create_user_success(self, user_service, mock_db_session, sample_user_data):
        """Test successful user creation."""
        # Arrange
        mock_db_session.add = AsyncMock()
        mock_db_session.commit = AsyncMock()
        mock_db_session.refresh = AsyncMock()

        # Mock the get_by_telegram_id call to return None (user doesn't exist)
        with patch.object(user_service, 'get_by_telegram_id', return_value=None):
            # Act
            result = await user_service.create_user(mock_db_session, sample_user_data)

        # Assert
        assert isinstance(result, UserModel)
        assert result.telegram_id == sample_user_data.telegram_id
        assert result.username == sample_user_data.username
        mock_db_session.add.assert_called_once()
        mock_db_session.commit.assert_called_once()
        mock_db_session.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_user_database_error(self, user_service, mock_db_session, sample_user_data):
        """Test user creation with database error."""
        # Arrange
        mock_db_session.add = AsyncMock()
        mock_db_session.commit = AsyncMock(side_effect=IntegrityError("", "", ""))
        mock_db_session.rollback = AsyncMock()

        # Mock the get_by_telegram_id call to return None (user doesn't exist)
        with patch.object(user_service, 'get_by_telegram_id', return_value=None):
            # Act & Assert
            with pytest.raises(DatabaseError):
                await user_service.create_user(mock_db_session, sample_user_data)

        mock_db_session.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id_success(self, user_service, mock_db_session, sample_user_model):
        """Test successful user retrieval by telegram_id."""
        # Arrange
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = Mock(return_value=sample_user_model)
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        # Act
        result = await user_service.get_user_by_telegram_id(
            mock_db_session, sample_user_model.telegram_id
        )

        # Assert
        assert result == sample_user_model
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id_not_found(self, user_service, mock_db_session):
        """Test user retrieval when user not found."""
        # Arrange
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = Mock(return_value=None)
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        # Act
        result = await user_service.get_user_by_telegram_id(mock_db_session, 999999999)

        # Assert
        assert result is None

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id_database_error(self, user_service, mock_db_session):
        """Test user retrieval with database error."""
        # Arrange
        mock_db_session.execute.side_effect = Exception("Database error")

        # Act & Assert
        with pytest.raises(DatabaseError):
            await user_service.get_user_by_telegram_id(mock_db_session, 123456789)

    @pytest.mark.asyncio
    async def test_update_user_success(self, user_service, mock_db_session, sample_user_model):
        """Test successful user update."""
        # Arrange
        update_data = UserUpdate(first_name="Updated Name")
        mock_db_session.commit = AsyncMock(return_value=None)
        mock_db_session.refresh = AsyncMock(return_value=None)

        # Mock the get_by_telegram_id call
        with patch.object(user_service, 'get_by_telegram_id', return_value=sample_user_model):
            # Act
            result = await user_service.update_user(
                mock_db_session, sample_user_model.telegram_id, update_data
            )

        # Assert
        assert result == sample_user_model
        mock_db_session.commit.assert_called_once()
        mock_db_session.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_user_not_found(self, user_service, mock_db_session):
        """Test user update when user not found."""
        # Arrange
        update_data = UserUpdate(first_name="Updated Name")

        with patch.object(user_service, 'get_by_telegram_id', return_value=None):
            # Act & Assert
            with pytest.raises(ValidationError):
                await user_service.update_user(mock_db_session, 999999999, update_data)

    @pytest.mark.asyncio
    async def test_update_user_preferences_success(self, user_service, mock_db_session, sample_user_model):
        """Test successful user preferences update."""
        # Arrange
        new_preferences = UserPreferencesFactory.build(
            preferred_currencies=["USD", "EUR"],
            impact_levels=["high"]
        )

        mock_db_session.commit = AsyncMock(return_value=None)
        mock_db_session.refresh = AsyncMock(return_value=None)

        with patch.object(user_service, 'get_by_telegram_id', return_value=sample_user_model):
            # Act
            result = await user_service.update_user_preferences(
                mock_db_session, sample_user_model.telegram_id, new_preferences
            )

        # Assert
        assert result == sample_user_model
        mock_db_session.commit.assert_called_once()
        mock_db_session.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_users_by_currency(self, user_service, mock_db_session):
        """Test getting users by currency preference."""
        # Arrange
        users = [UserModelFactory.build() for _ in range(3)]
        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_scalars = Mock()
        mock_scalars.all.return_value = users
        mock_result.scalars.return_value = mock_scalars
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await user_service.get_users_by_currency(mock_db_session, "USD")

        # Assert
        assert len(result) == 3
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_users_by_impact_level(self, user_service, mock_db_session):
        """Test getting users by impact level preference."""
        # Arrange
        users = [UserModelFactory.build() for _ in range(2)]
        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_scalars = Mock()
        mock_scalars.all.return_value = users
        mock_result.scalars.return_value = mock_scalars
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await user_service.get_users_by_impact_level(mock_db_session, "high")

        # Assert
        assert len(result) == 2
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_all_users_with_pagination(self, user_service, mock_db_session):
        """Test getting all users with pagination."""
        # Arrange
        users = [UserModelFactory.build() for _ in range(5)]
        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_scalars = Mock()
        mock_scalars.all.return_value = users
        mock_result.scalars.return_value = mock_scalars
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await user_service.get_all_users_with_pagination(mock_db_session, skip=0, limit=10)

        # Assert
        assert len(result) == 5
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_user_success(self, user_service, mock_db_session, sample_user_model):
        """Test successful user deletion."""
        # Arrange
        with patch.object(user_service, 'get_user_by_telegram_id', return_value=sample_user_model):
            mock_db_session.delete = AsyncMock()
            mock_db_session.commit = AsyncMock()

            # Act
            result = await user_service.delete_user(mock_db_session, sample_user_model.telegram_id)

        # Assert
        assert result is True
        mock_db_session.delete.assert_called_once()
        mock_db_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_user_not_found(self, user_service, mock_db_session):
        """Test user deletion when user not found."""
        # Arrange
        with patch.object(user_service, 'get_by_telegram_id', return_value=None):
            # Act
            result = await user_service.delete_user(mock_db_session, 999999999)

        # Assert
        assert result is False

    @pytest.mark.asyncio
    async def test_count_users(self, user_service, mock_db_session):
        """Test counting users."""
        # Arrange
        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_result.scalar = Mock(return_value=42)
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await user_service.count_users(mock_db_session)

        # Assert
        assert result == 42
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_exists(self, user_service, mock_db_session):
        """Test checking if user exists."""
        # Arrange
        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = Mock(return_value=UserModelFactory.build())
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await user_service.user_exists(mock_db_session, 123456789)

        # Assert
        assert result is True
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_not_exists(self, user_service, mock_db_session):
        """Test checking if user does not exist."""
        # Arrange
        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = Mock(return_value=None)
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await user_service.user_exists(mock_db_session, 999999999)

        # Assert
        assert result is False
        mock_db_session.execute.assert_called_once()
