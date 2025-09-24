"""Tests for ForexService."""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from datetime import date, time
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.forex_service import ForexService
from app.database.models import ForexNewsModel
from app.models.forex_news import ForexNewsCreate, ForexNewsUpdate
from app.core.exceptions import DatabaseError, ValidationError
from tests.factories import ForexNewsCreateFactory, ForexNewsModelFactory


@pytest.fixture
def forex_service():
    """Create ForexService instance."""
    return ForexService()


@pytest.fixture
def mock_db_session():
    """Create mock database session."""
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def sample_forex_news_data():
    """Sample forex news data for testing."""
    return ForexNewsCreateFactory.build()


@pytest.fixture
def sample_forex_news_model():
    """Sample forex news model for testing."""
    return ForexNewsModelFactory.build()


class TestForexService:
    """Test cases for ForexService."""

    @pytest.mark.asyncio
    async def test_create_forex_news_success(self, forex_service, mock_db_session, sample_forex_news_data):
        """Test successful forex news creation."""
        # Arrange
        mock_db_session.add = AsyncMock()
        mock_db_session.commit = AsyncMock()
        mock_db_session.refresh = AsyncMock()

        # Act
        result = await forex_service.create_forex_news(mock_db_session, sample_forex_news_data)

        # Assert
        assert isinstance(result, ForexNewsModel)
        assert result.currency == sample_forex_news_data.currency
        assert result.event == sample_forex_news_data.event
        mock_db_session.add.assert_called_once()
        mock_db_session.commit.assert_called_once()
        mock_db_session.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_forex_news_by_id_success(self, forex_service, mock_db_session, sample_forex_news_model):
        """Test successful forex news retrieval by ID."""
        # Arrange
        mock_db_session.execute = AsyncMock()
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = sample_forex_news_model
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.get_forex_news_by_id(mock_db_session, sample_forex_news_model.id)

        # Assert
        assert result == sample_forex_news_model
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_forex_news_by_id_not_found(self, forex_service, mock_db_session):
        """Test forex news retrieval when not found."""
        # Arrange
        mock_db_session.execute = AsyncMock()
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.get_forex_news_by_id(mock_db_session, 999)

        # Assert
        assert result is None
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_forex_news_by_date_range(self, forex_service, mock_db_session):
        """Test getting forex news by date range."""
        # Arrange
        start_date = date(2024, 1, 1)
        end_date = date(2024, 1, 31)
        news_items = [ForexNewsModelFactory.build() for _ in range(5)]

        mock_db_session.execute = AsyncMock()
        mock_scalars_result = Mock()
        mock_scalars_result.all.return_value = news_items
        mock_result = AsyncMock()
        mock_result.scalars.return_value = mock_scalars_result
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.get_forex_news_by_date_range(
            mock_db_session, start_date, end_date
        )

        # Assert
        assert len(result) == 5
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_forex_news_by_currency(self, forex_service, mock_db_session):
        """Test getting forex news by currency."""
        # Arrange
        news_items = [ForexNewsModelFactory.build(currency="USD") for _ in range(3)]

        mock_db_session.execute = AsyncMock()
        mock_scalars_result = Mock()
        mock_scalars_result.all.return_value = news_items
        mock_result = AsyncMock()
        mock_result.scalars.return_value = mock_scalars_result
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.get_forex_news_by_currency(mock_db_session, "USD")

        # Assert
        assert len(result) == 3
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_forex_news_by_impact_level(self, forex_service, mock_db_session):
        """Test getting forex news by impact level."""
        # Arrange
        news_items = [ForexNewsModelFactory.build(impact_level="high") for _ in range(2)]

        mock_db_session.execute = AsyncMock()
        mock_scalars_result = Mock()
        mock_scalars_result.all.return_value = news_items
        mock_result = AsyncMock()
        mock_result.scalars.return_value = mock_scalars_result
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.get_forex_news_by_impact_level(mock_db_session, "high")

        # Assert
        assert len(result) == 2
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_todays_forex_news(self, forex_service, mock_db_session):
        """Test getting today's forex news."""
        # Arrange
        news_items = [ForexNewsModelFactory.build() for _ in range(4)]

        mock_db_session.execute = AsyncMock()
        mock_scalars_result = Mock()
        mock_scalars_result.all.return_value = news_items
        mock_result = AsyncMock()
        mock_result.scalars.return_value = mock_scalars_result
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.get_todays_forex_news(mock_db_session)

        # Assert
        assert len(result) == 4
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_upcoming_events(self, forex_service, mock_db_session):
        """Test getting upcoming forex events."""
        # Arrange
        news_items = [ForexNewsModelFactory.build() for _ in range(3)]

        mock_db_session.execute = AsyncMock()
        mock_scalars_result = Mock()
        mock_scalars_result.all.return_value = news_items
        mock_result = AsyncMock()
        mock_result.scalars.return_value = mock_scalars_result
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.get_upcoming_events(mock_db_session, hours=24)

        # Assert
        assert len(result) == 3
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_forex_news_success(self, forex_service, mock_db_session, sample_forex_news_model):
        """Test successful forex news update."""
        # Arrange
        update_data = ForexNewsUpdate(analysis="Updated analysis")

        with patch.object(forex_service, 'update', return_value=sample_forex_news_model) as mock_update:
            # Act
            result = await forex_service.update_forex_news(
                mock_db_session, sample_forex_news_model.id, update_data
            )

        # Assert
        assert result == sample_forex_news_model
        mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_forex_news_not_found(self, forex_service, mock_db_session):
        """Test forex news update when not found."""
        # Arrange
        update_data = ForexNewsUpdate(analysis="Updated analysis")

        with patch.object(forex_service, 'get', return_value=None):
            # Act & Assert
            with pytest.raises(ValidationError, match="ForexNewsModel not found"):
                await forex_service.update_forex_news(mock_db_session, 999, update_data)

    @pytest.mark.asyncio
    async def test_delete_forex_news_success(self, forex_service, mock_db_session, sample_forex_news_model):
        """Test successful forex news deletion."""
        # Arrange
        with patch.object(forex_service, 'delete', return_value=True) as mock_delete:
            # Act
            result = await forex_service.delete_forex_news(mock_db_session, sample_forex_news_model.id)

        # Assert
        assert result is True
        mock_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_forex_news_not_found(self, forex_service, mock_db_session):
        """Test forex news deletion when not found."""
        # Arrange
        with patch.object(forex_service, 'get', return_value=None):
            # Act & Assert
            with pytest.raises(ValidationError, match="Forex news not found"):
                await forex_service.delete_forex_news(mock_db_session, 999)

    @pytest.mark.asyncio
    async def test_count_forex_news(self, forex_service, mock_db_session):
        """Test counting forex news."""
        # Arrange
        mock_db_session.execute = AsyncMock()
        mock_scalar_result = Mock()
        mock_scalar_result.return_value = 25
        mock_result = AsyncMock()
        mock_result.scalar = mock_scalar_result
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.count_forex_news(mock_db_session)

        # Assert
        assert result == 25
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_forex_news_exists(self, forex_service, mock_db_session):
        """Test checking if forex news exists."""
        # Arrange
        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none.return_value = ForexNewsModelFactory.build()
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.forex_news_exists(mock_db_session, 1)

        # Assert
        assert result is True
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_forex_news_with_filters(self, forex_service, mock_db_session):
        """Test getting forex news with multiple filters."""
        # Arrange
        filters = {
            "currency": "USD",
            "impact_level": "high",
            "date": date(2024, 1, 15)
        }
        news_items = [ForexNewsModelFactory.build() for _ in range(2)]

        mock_db_session.execute = AsyncMock()
        mock_result = AsyncMock()
        mock_scalars = Mock()
        mock_scalars.all.return_value = news_items
        mock_result.scalars.return_value = mock_scalars
        mock_db_session.execute.return_value = mock_result

        # Act
        result = await forex_service.get_forex_news_with_filters(mock_db_session, filters)

        # Assert
        assert len(result) == 2
        mock_db_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_bulk_create_forex_news(self, forex_service, mock_db_session):
        """Test bulk creation of forex news."""
        # Arrange
        news_data_list = [ForexNewsCreateFactory.build() for _ in range(3)]
        mock_db_session.commit = AsyncMock()

        # Mock the create method to return mock news items
        mock_news_items = [ForexNewsModelFactory.build() for _ in range(3)]

        with patch.object(forex_service, 'create', side_effect=mock_news_items) as mock_create:
            # Act
            result = await forex_service.bulk_create_forex_news(mock_db_session, news_data_list)

        # Assert
        assert len(result) == 3
        assert mock_create.call_count == 3
        mock_db_session.commit.assert_called_once()
