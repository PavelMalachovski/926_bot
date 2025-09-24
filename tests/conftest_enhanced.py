"""Enhanced test configuration with Context7 best practices."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from typing import AsyncGenerator, Generator
import tempfile
import os
from datetime import datetime, date, time
import json

# Import application components
from app.main import app
from app.database.connection import db_manager
from app.services.cache_service import cache_service
from app.core.config import settings
from app.database.models import UserModel, ForexNewsModel, UserPreferences
from tests.factories import UserModelFactory, ForexNewsModelFactory, UserPreferencesFactory


# ============================================================================
# ASYNC TEST CONFIGURATION
# ============================================================================

@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# DATABASE FIXTURES
# ============================================================================

@pytest.fixture
async def test_db_session() -> AsyncGenerator:
    """Create a test database session."""
    # Initialize database manager for testing
    await db_manager.initialize()

    try:
        async with db_manager.get_session_async() as session:
            yield session
    finally:
        await db_manager.close()


@pytest.fixture
async def clean_db(test_db_session) -> AsyncGenerator:
    """Provide a clean database for each test."""
    # Clean up all tables
    await test_db_session.execute("DELETE FROM user_preferences")
    await test_db_session.execute("DELETE FROM forex_news")
    await test_db_session.execute("DELETE FROM users")
    await test_db_session.commit()

    yield test_db_session

    # Clean up after test
    await test_db_session.execute("DELETE FROM user_preferences")
    await test_db_session.execute("DELETE FROM forex_news")
    await test_db_session.execute("DELETE FROM users")
    await test_db_session.commit()


# ============================================================================
# REDIS/CACHE FIXTURES
# ============================================================================

@pytest.fixture
async def mock_redis_client():
    """Create a mock Redis client for testing."""
    mock_client = AsyncMock()

    # Configure common Redis methods
    mock_client.ping = AsyncMock(return_value=True)
    mock_client.config_set = AsyncMock(return_value=True)
    mock_client.info = AsyncMock(return_value={
        "connected_clients": 1,
        "used_memory_human": "1MB",
        "keyspace_hits": 100,
        "keyspace_misses": 50,
        "total_commands_processed": 1000,
        "redis_version": "7.0.0",
        "uptime_in_seconds": 3600
    })

    # Cache operations
    mock_client.get = AsyncMock(return_value=None)
    mock_client.set = AsyncMock(return_value=True)
    mock_client.delete = AsyncMock(return_value=1)
    mock_client.exists = AsyncMock(return_value=True)
    mock_client.mget = AsyncMock(return_value=[])
    mock_client.keys = AsyncMock(return_value=[])
    mock_client.expire = AsyncMock(return_value=True)

    # Pipeline operations
    mock_pipeline = AsyncMock()
    mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
    mock_pipeline.__aexit__ = AsyncMock(return_value=None)
    mock_pipeline.execute = AsyncMock(return_value=[True, True])
    mock_client.pipeline.return_value = mock_pipeline

    # Pub/Sub operations
    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = AsyncMock()
    mock_pubsub.close = AsyncMock()
    mock_client.pubsub.return_value = mock_pubsub
    mock_client.publish = AsyncMock(return_value=1)

    # Rate limiting operations
    mock_client.zremrangebyscore = AsyncMock(return_value=0)
    mock_client.zcard = AsyncMock(return_value=5)
    mock_client.zadd = AsyncMock(return_value=True)

    mock_client.close = AsyncMock()

    return mock_client


@pytest.fixture
async def mock_cache_service(mock_redis_client):
    """Create a mock cache service."""
    with patch('app.services.cache_service.cache_service') as mock_service:
        mock_service._initialized = True
        mock_service.redis_client = mock_redis_client
        mock_service.pubsub_client = mock_redis_client
        mock_service.connection_pool = AsyncMock()

        # Cache operations
        mock_service.get = AsyncMock(return_value=None)
        mock_service.set = AsyncMock(return_value=True)
        mock_service.delete = AsyncMock(return_value=True)
        mock_service.exists = AsyncMock(return_value=True)
        mock_service.get_many = AsyncMock(return_value={})
        mock_service.set_many = AsyncMock(return_value=True)
        mock_service.get_or_set = AsyncMock()
        mock_service.invalidate_pattern = AsyncMock(return_value=0)
        mock_service.get_stats = AsyncMock(return_value={"status": "healthy"})

        # Enhanced features
        mock_service.pubsub_service = AsyncMock()
        mock_service.rate_limiter = AsyncMock()
        mock_service.session_manager = AsyncMock()
        mock_service.get_hit_ratio = AsyncMock(return_value=0.8)

        yield mock_service


# ============================================================================
# SERVICE FIXTURES
# ============================================================================

@pytest.fixture
def mock_telegram_bot():
    """Create a mock Telegram bot."""
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock())
    mock_bot.set_webhook = AsyncMock(return_value=True)
    mock_bot.delete_webhook = AsyncMock(return_value=True)
    mock_bot.get_webhook_info = AsyncMock(return_value={"url": ""})
    return mock_bot


@pytest.fixture
def mock_openai_client():
    """Create a mock OpenAI client."""
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=MagicMock(
        choices=[MagicMock(message=MagicMock(content="Test analysis"))]
    ))
    return mock_client


@pytest.fixture
def mock_alpha_vantage_client():
    """Create a mock Alpha Vantage client."""
    mock_client = AsyncMock()
    mock_client.get_forex_data = AsyncMock(return_value={
        "Time Series (FX)": {
            "2024-01-01 00:00:00": {
                "1. open": "1.2345",
                "2. high": "1.2350",
                "3. low": "1.2340",
                "4. close": "1.2348"
            }
        }
    })
    return mock_client


# ============================================================================
# MODEL FIXTURES
# ============================================================================

@pytest.fixture
def sample_user() -> UserModel:
    """Create a sample user for testing."""
    return UserModelFactory.build(
        telegram_id=123456789,
        username="testuser",
        first_name="Test",
        last_name="User"
    )


@pytest.fixture
def sample_forex_news() -> ForexNewsModel:
    """Create sample forex news for testing."""
    return ForexNewsModelFactory.build(
        currency="USD",
        event="Non-Farm Payrolls",
        impact_level="high",
        actual="200K",
        forecast="180K",
        previous="190K"
    )


@pytest.fixture
def sample_user_preferences() -> UserPreferences:
    """Create sample user preferences for testing."""
    return UserPreferencesFactory.build(
        preferred_currencies=["USD", "EUR"],
        impact_levels=["high", "medium"],
        notifications_enabled=True,
        notification_minutes=15
    )


# ============================================================================
# API CLIENT FIXTURES
# ============================================================================

@pytest.fixture
async def async_client():
    """Create an async HTTP client for testing."""
    from httpx import AsyncClient, ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def authenticated_client(async_client, sample_user):
    """Create an authenticated HTTP client."""
    # Mock authentication
    with patch('app.api.dependencies.get_current_user', return_value=sample_user):
        yield async_client


# ============================================================================
# TEST DATA FIXTURES
# ============================================================================

@pytest.fixture
def test_forex_data():
    """Sample forex data for testing."""
    return {
        "currency": "USD",
        "symbol": "USD/EUR",
        "price": 0.9234,
        "change": 0.0012,
        "change_percent": 0.13,
        "timestamp": datetime.now().isoformat()
    }


@pytest.fixture
def test_chart_data():
    """Sample chart data for testing."""
    return {
        "symbol": "USD/EUR",
        "timeframe": "1h",
        "data": [
            {"timestamp": "2024-01-01T00:00:00", "open": 0.9234, "high": 0.9240, "low": 0.9230, "close": 0.9238},
            {"timestamp": "2024-01-01T01:00:00", "open": 0.9238, "high": 0.9245, "low": 0.9235, "close": 0.9242},
        ]
    }


@pytest.fixture
def test_notification_data():
    """Sample notification data for testing."""
    return {
        "user_id": 123456789,
        "message": "High impact news: USD Non-Farm Payrolls",
        "priority": "high",
        "timestamp": datetime.now().isoformat()
    }


# ============================================================================
# PERFORMANCE TEST FIXTURES
# ============================================================================

@pytest.fixture
def performance_test_data():
    """Generate large datasets for performance testing."""
    return {
        "users": [UserModelFactory.build() for _ in range(100)],
        "forex_news": [ForexNewsModelFactory.build() for _ in range(1000)],
        "preferences": [UserPreferencesFactory.build() for _ in range(100)]
    }


# ============================================================================
# INTEGRATION TEST FIXTURES
# ============================================================================

@pytest.fixture
async def integration_test_setup(clean_db, mock_cache_service):
    """Setup for integration tests."""
    # Create test data
    users = [UserModelFactory.build() for _ in range(5)]
    forex_news = [ForexNewsModelFactory.build() for _ in range(10)]

    # Add to database
    for user in users:
        clean_db.add(user)
    for news in forex_news:
        clean_db.add(news)

    await clean_db.commit()

    return {
        "users": users,
        "forex_news": forex_news,
        "db": clean_db
    }


# ============================================================================
# UTILITY FIXTURES
# ============================================================================

@pytest.fixture
def temp_file():
    """Create a temporary file for testing."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"test data")
        temp_path = f.name

    yield temp_path

    # Cleanup
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def mock_logger():
    """Create a mock logger for testing."""
    mock_logger = MagicMock()
    mock_logger.info = MagicMock()
    mock_logger.error = MagicMock()
    mock_logger.warning = MagicMock()
    mock_logger.debug = MagicMock()
    return mock_logger


# ============================================================================
# PARAMETRIZED FIXTURES
# ============================================================================

@pytest.fixture(params=["USD", "EUR", "GBP", "JPY"])
def currency_param(request):
    """Parametrized fixture for different currencies."""
    return request.param


@pytest.fixture(params=["high", "medium", "low"])
def impact_level_param(request):
    """Parametrized fixture for different impact levels."""
    return request.param


@pytest.fixture(params=[1, 5, 10, 50, 100])
def batch_size_param(request):
    """Parametrized fixture for different batch sizes."""
    return request.param


# ============================================================================
# MARKERS AND CONFIGURATION
# ============================================================================

def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "performance: marks tests as performance tests"
    )
    config.addinivalue_line(
        "markers", "redis: marks tests that require Redis"
    )
    config.addinivalue_line(
        "markers", "database: marks tests that require database"
    )


def pytest_collection_modifyitems(config, items):
    """Modify test collection to add markers automatically."""
    for item in items:
        # Add slow marker to tests that take longer than 1 second
        if "performance" in item.name or "load" in item.name:
            item.add_marker(pytest.mark.slow)

        # Add integration marker to integration tests
        if "integration" in item.name:
            item.add_marker(pytest.mark.integration)

        # Add redis marker to Redis-related tests
        if "redis" in item.name or "cache" in item.name:
            item.add_marker(pytest.mark.redis)

        # Add database marker to database-related tests
        if "database" in item.name or "db" in item.name:
            item.add_marker(pytest.mark.database)


# ============================================================================
# TEST UTILITIES
# ============================================================================

class TestDataBuilder:
    """Builder pattern for creating test data."""

    def __init__(self):
        self.data = {}

    def with_user(self, **kwargs):
        self.data["user"] = UserModelFactory.build(**kwargs)
        return self

    def with_forex_news(self, **kwargs):
        self.data["forex_news"] = ForexNewsModelFactory.build(**kwargs)
        return self

    def with_preferences(self, **kwargs):
        self.data["preferences"] = UserPreferencesFactory.build(**kwargs)
        return self

    def build(self):
        return self.data


@pytest.fixture
def test_data_builder():
    """Provide a test data builder."""
    return TestDataBuilder()


# ============================================================================
# CLEANUP FIXTURES
# ============================================================================

@pytest.fixture(autouse=True)
async def cleanup_after_test():
    """Automatic cleanup after each test."""
    yield

    # Clean up any temporary files, connections, etc.
    if hasattr(cache_service, 'redis_client') and cache_service.redis_client:
        try:
            await cache_service.close()
        except Exception:
            pass
