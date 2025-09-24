"""Pytest configuration and shared fixtures for the modern FastAPI application."""

import pytest
import pytest_asyncio
from typing import AsyncGenerator
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database.connection import db_manager
from app.database.models import Base
from app.core.config import settings


@pytest_asyncio.fixture
async def test_db_engine():
    """Create a test database engine."""
    # Use in-memory SQLite for testing
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        echo=False,
    )

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Clean up
    await engine.dispose()


@pytest_asyncio.fixture
async def test_db_session(test_db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    async_session = async_sessionmaker(
        test_db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def test_client(test_db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create a test client for the FastAPI application."""
    # Override the database dependency
    async def override_get_database():
        yield test_db_session

    app.dependency_overrides[db_manager.get_session_async] = override_get_database

    # Create test client
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    # Clean up
    app.dependency_overrides.clear()


@pytest.fixture
def sample_user_data():
    """Sample user data for testing."""
    return {
        "telegram_id": 123456789,
        "username": "testuser",
        "first_name": "Test",
        "last_name": "User",
        "language_code": "en",
        "is_bot": False,
        "is_premium": False,
        "preferences": {
            "preferred_currencies": ["USD", "EUR"],
            "impact_levels": ["high", "medium"],
            "analysis_required": True,
            "digest_time": "08:00:00",
            "timezone": "Europe/Prague",
            "notifications_enabled": True,
            "notification_minutes": 30,
            "notification_impact_levels": ["high"],
            "charts_enabled": True,
            "chart_type": "single",
            "chart_window_hours": 2
        }
    }


@pytest.fixture
def sample_forex_news_data():
    """Sample forex news data for testing."""
    return {
        "date": "2024-01-15T00:00:00Z",
        "time": "14:30",
        "currency": "USD",
        "event": "Non-Farm Payrolls",
        "actual": "200K",
        "forecast": "195K",
        "previous": "190K",
        "impact_level": "high",
        "analysis": "Strong employment data suggests economic growth"
    }


@pytest.fixture
def sample_chart_request_data():
    """Sample chart request data for testing."""
    return {
        "currency": "USD",
        "event_time": "2024-01-15T14:30:00Z",
        "event_name": "Non-Farm Payrolls",
        "impact_level": "high",
        "window_hours": 2,
        "chart_type": "single"
    }


@pytest.fixture
def sample_notification_data():
    """Sample notification data for testing."""
    return {
        "user_id": 1,
        "event_id": 1,
        "notification_type": "event_reminder",
        "message": "High impact event starting in 30 minutes",
        "scheduled_time": "2024-01-15T14:00:00Z",
        "status": "pending"
    }


@pytest.fixture
def sample_telegram_update_data():
    """Sample Telegram update data for testing."""
    return {
        "update_id": 123456789,
        "message": {
            "message_id": 1,
            "from": {
                "id": 123456789,
                "is_bot": False,
                "first_name": "Test",
                "last_name": "User",
                "username": "testuser",
                "language_code": "en"
            },
            "chat": {
                "id": 123456789,
                "type": "private",
                "first_name": "Test",
                "last_name": "User",
                "username": "testuser"
            },
            "date": 1640995200,
            "text": "/start"
        }
    }


@pytest.fixture
def mock_redis():
    """Mock Redis for testing."""
    from unittest.mock import AsyncMock

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True
    mock_redis.delete.return_value = True
    mock_redis.exists.return_value = False

    return mock_redis


@pytest.fixture
def mock_telegram_bot():
    """Mock Telegram bot for testing."""
    from unittest.mock import AsyncMock

    mock_bot = AsyncMock()
    mock_bot.send_message.return_value = AsyncMock()
    mock_bot.get_webhook_info.return_value = AsyncMock()
    mock_bot.set_webhook.return_value = AsyncMock()
    mock_bot.delete_webhook.return_value = AsyncMock()

    return mock_bot


@pytest.fixture
def mock_external_api():
    """Mock external API responses for testing."""
    from unittest.mock import AsyncMock

    mock_api = AsyncMock()
    mock_api.get.return_value = AsyncMock(
        status_code=200,
        json=lambda: {"data": "mock_data"}
    )

    return mock_api


# Test markers
pytest.mark.asyncio = pytest.mark.asyncio
pytest.mark.integration = pytest.mark.integration
pytest.mark.unit = pytest.mark.unit
pytest.mark.slow = pytest.mark.slow
