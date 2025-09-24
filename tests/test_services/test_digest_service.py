"""Tests for digest service."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, time, timedelta
import pytz

from app.services.digest_service import DailyDigestScheduler
from app.core.exceptions import DigestError, DatabaseError
from tests.factories import UserCreateFactory, ForexNewsCreateFactory


@pytest.fixture
def digest_scheduler(mock_db_session, mock_telegram_service):
    """Create digest scheduler instance."""
    mock_config = {"timezone": "UTC"}
    return DailyDigestScheduler(mock_db_session, mock_telegram_service, mock_config)


@pytest.fixture
def mock_db_session():
    """Create mock database session."""
    return AsyncMock()


@pytest.fixture
def mock_telegram_service():
    """Create mock telegram service."""
    return AsyncMock()


@pytest.fixture
def mock_forex_service():
    """Create mock forex service."""
    return AsyncMock()


class TestDailyDigestScheduler:
    """Test cases for DailyDigestScheduler."""

    def test_health_check_success(self, digest_scheduler):
        """Test successful health check."""
        # Arrange
        digest_scheduler.scheduler = MagicMock()
        digest_scheduler.scheduler.running = True

        # Act
        result = digest_scheduler.health_check()

        # Assert
        assert result["status"] == "healthy"
        assert result["scheduler_running"] is True

    @pytest.mark.asyncio
    async def test_initialize_error(self, digest_scheduler):
        """Test digest scheduler initialization with error."""
        # This test is skipped because DailyDigestScheduler doesn't have an initialize method
        # The scheduler is initialized in the constructor
        pytest.skip("DailyDigestScheduler doesn't have an initialize method")

    @pytest.mark.asyncio
    async def test_shutdown_success(self, digest_scheduler):
        """Test successful digest scheduler shutdown."""
        # Arrange
        mock_scheduler = MagicMock()
        digest_scheduler.scheduler = mock_scheduler

        # Act
        digest_scheduler.shutdown()

        # Assert
        mock_scheduler.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_no_scheduler(self, digest_scheduler):
        """Test digest scheduler shutdown when scheduler is not initialized."""
        # Arrange
        digest_scheduler.scheduler = None

        # Act
        digest_scheduler.shutdown()

        # Assert
        # Should not raise any exception

    @pytest.mark.asyncio
    async def test_schedule_user_digest_success(self, digest_scheduler, mock_db_session):
        """Test successful user digest scheduling."""
        # Arrange
        user_data = UserCreateFactory.build()
        digest_time = time(8, 0)
        timezone = "Europe/Prague"

        mock_scheduler = MagicMock()
        digest_scheduler.scheduler = mock_scheduler

        # Act
        digest_scheduler.schedule_user_digest(
            user_data.telegram_id, timezone, digest_time
        )

        # Assert
        mock_scheduler.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_schedule_user_digest_error(self, digest_scheduler, mock_db_session):
        """Test user digest scheduling with error."""
        # Arrange
        user_data = UserCreateFactory.build()
        digest_time = time(8, 0)
        timezone = "Europe/Prague"

        mock_scheduler = MagicMock()
        mock_scheduler.add_job.side_effect = Exception("Scheduling error")
        digest_scheduler.scheduler = mock_scheduler

        # Act & Assert
        with pytest.raises(DigestError):
            digest_scheduler.schedule_user_digest(
                user_data.telegram_id, timezone, digest_time
            )

    @pytest.mark.asyncio
    async def test_unschedule_user_digest_success(self, digest_scheduler):
        """Test successful user digest unscheduling."""
        # Arrange
        user_id = 123456789
        mock_scheduler = MagicMock()
        digest_scheduler.scheduler = mock_scheduler

        # Act
        digest_scheduler.unschedule_user_digest(user_id)

        # Assert
        mock_scheduler.remove_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_unschedule_user_digest_error(self, digest_scheduler):
        """Test user digest unscheduling with error."""
        # Arrange
        user_id = 123456789
        mock_scheduler = MagicMock()
        mock_scheduler.remove_job.side_effect = Exception("Unscheduling error")
        digest_scheduler.scheduler = mock_scheduler

        # Act
        digest_scheduler.unschedule_user_digest(user_id)

        # Assert - should not raise exception, just log error
        # The method catches exceptions and logs them

    @pytest.mark.asyncio
    async def test_send_daily_digest_success(self, digest_scheduler, mock_db_session, mock_telegram_service, mock_forex_service):
        """Test successful daily digest sending."""
        # Arrange
        user_data = UserCreateFactory.build()
        news_data = [ForexNewsCreateFactory.build() for _ in range(3)]

        mock_forex_service.get_news_by_date.return_value = news_data
        mock_telegram_service.send_formatted_message.return_value = AsyncMock()

        # Act
        await digest_scheduler.send_daily_digest(
            mock_db_session, user_data, mock_telegram_service, mock_forex_service
        )

        # Assert
        mock_forex_service.get_news_by_date.assert_called_once()
        mock_telegram_service.send_formatted_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_daily_digest_no_news(self, digest_scheduler, mock_db_session, mock_telegram_service, mock_forex_service):
        """Test daily digest sending with no news."""
        # Arrange
        user_data = UserCreateFactory.build()
        mock_forex_service.get_news_by_date.return_value = []
        mock_telegram_service.send_formatted_message.return_value = AsyncMock()

        # Act
        await digest_scheduler.send_daily_digest(
            mock_db_session, user_data, mock_telegram_service, mock_forex_service
        )

        # Assert - when no news, the method returns early without sending message
        mock_forex_service.get_news_by_date.assert_called_once()
        mock_telegram_service.send_formatted_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_daily_digest_error(self, digest_scheduler, mock_db_session, mock_telegram_service, mock_forex_service):
        """Test daily digest sending with error."""
        # Arrange
        user_data = UserCreateFactory.build()
        mock_forex_service.get_news_by_date.side_effect = Exception("Database error")

        # Act & Assert
        with pytest.raises(DigestError):
            await digest_scheduler.send_daily_digest(
                mock_db_session, user_data, mock_telegram_service, mock_forex_service
            )

    @pytest.mark.asyncio
    async def test_schedule_channel_digest_success(self, digest_scheduler, mock_db_session):
        """Test successful channel digest scheduling."""
        # Arrange
        channel_id = -1001234567890
        digest_time = time(9, 0)
        timezone = "Europe/Prague"

        mock_scheduler = MagicMock()
        digest_scheduler.scheduler = mock_scheduler

        # Act
        await digest_scheduler.schedule_channel_digest(
            mock_db_session, channel_id, digest_time, timezone
        )

        # Assert
        mock_scheduler.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_schedule_channel_digest_error(self, digest_scheduler, mock_db_session):
        """Test channel digest scheduling with error."""
        # Arrange
        channel_id = -1001234567890
        digest_time = time(9, 0)
        timezone = "Europe/Prague"

        mock_scheduler = MagicMock()
        mock_scheduler.add_job.side_effect = Exception("Scheduling error")
        digest_scheduler.scheduler = mock_scheduler

        # Act & Assert
        with pytest.raises(DigestError):
            await digest_scheduler.schedule_channel_digest(
                mock_db_session, channel_id, digest_time, timezone
            )

    @pytest.mark.asyncio
    async def test_send_channel_digest_success(self, digest_scheduler, mock_db_session, mock_telegram_service, mock_forex_service):
        """Test successful channel digest sending."""
        # Arrange
        channel_id = -1001234567890
        news_data = [ForexNewsCreateFactory.build() for _ in range(5)]

        mock_forex_service.get_news_by_date.return_value = news_data
        mock_telegram_service.send_formatted_message.return_value = AsyncMock()

        # Act
        await digest_scheduler.send_channel_digest(
            mock_db_session, channel_id, mock_telegram_service, mock_forex_service
        )

        # Assert
        mock_forex_service.get_news_by_date.assert_called_once()
        mock_telegram_service.send_formatted_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_channel_digest_error(self, digest_scheduler, mock_db_session, mock_telegram_service, mock_forex_service):
        """Test channel digest sending with error."""
        # Arrange
        channel_id = -1001234567890
        mock_forex_service.get_news_by_date.side_effect = Exception("Database error")

        # Act & Assert
        with pytest.raises(DigestError):
            await digest_scheduler.send_channel_digest(
                mock_db_session, channel_id, mock_telegram_service, mock_forex_service
            )

    @pytest.mark.asyncio
    async def test_get_scheduled_jobs_success(self, digest_scheduler):
        """Test successful scheduled jobs retrieval."""
        # Arrange
        mock_scheduler = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "user_123456789_digest"
        mock_job.next_run_time = datetime.now() + timedelta(hours=1)
        mock_scheduler.get_jobs.return_value = [mock_job]
        digest_scheduler.scheduler = mock_scheduler

        # Act
        result = await digest_scheduler.get_scheduled_jobs()

        # Assert
        assert len(result) == 1
        assert result[0]["job_id"] == "user_123456789_digest"
        mock_scheduler.get_jobs.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_scheduled_jobs_error(self, digest_scheduler):
        """Test scheduled jobs retrieval with error."""
        # Arrange
        mock_scheduler = MagicMock()
        mock_scheduler.get_jobs.side_effect = Exception("Scheduler error")
        digest_scheduler.scheduler = mock_scheduler

        # Act & Assert
        with pytest.raises(DigestError):
            await digest_scheduler.get_scheduled_jobs()


    @pytest.mark.asyncio
    async def test_health_check_not_running(self, digest_scheduler):
        """Test health check when scheduler is not running."""
        # Arrange
        mock_scheduler = MagicMock()
        mock_scheduler.running = False
        mock_scheduler.get_jobs.return_value = []
        mock_scheduler.jobstores = {}
        digest_scheduler.scheduler = mock_scheduler

        # Act
        result = digest_scheduler.health_check()

        # Assert
        assert result["status"] == "healthy"  # scheduler exists, just not running
        assert result["scheduler_running"] is False

    @pytest.mark.asyncio
    async def test_health_check_no_scheduler(self, digest_scheduler):
        """Test health check when scheduler is not initialized."""
        # Arrange
        digest_scheduler.scheduler = None

        # Act
        result = digest_scheduler.health_check()

        # Assert
        assert result["status"] == "unhealthy"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_format_digest_message_success(self, digest_scheduler):
        """Test successful digest message formatting."""
        # Arrange
        news_data = [ForexNewsCreateFactory.build() for _ in range(3)]
        user_data = UserCreateFactory.build()

        # Act
        result = digest_scheduler.format_digest_message(news_data, user_data)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_format_digest_message_empty_news(self, digest_scheduler):
        """Test digest message formatting with empty news."""
        # Arrange
        news_data = []
        user_data = UserCreateFactory.build()

        # Act
        result = digest_scheduler.format_digest_message(news_data, user_data)

        # Assert
        assert isinstance(result, str)
        assert "📰 Daily Forex Digest" in result
        assert result.strip() == "📰 Daily Forex Digest"

    @pytest.mark.asyncio
    async def test_get_timezone_offset_success(self, digest_scheduler):
        """Test successful timezone offset calculation."""
        # Arrange
        timezone_str = "Europe/Prague"
        target_date = datetime.now().date()

        # Act
        result = digest_scheduler.get_timezone_offset(timezone_str, target_date)

        # Assert
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_get_timezone_offset_invalid_timezone(self, digest_scheduler):
        """Test timezone offset calculation with invalid timezone."""
        # Arrange
        timezone_str = "Invalid/Timezone"
        target_date = datetime.now().date()

        # Act & Assert
        with pytest.raises(DigestError):
            await digest_scheduler.get_timezone_offset(timezone_str, target_date)
