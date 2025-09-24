"""Tests for core configuration module."""

import pytest
from pydantic import ValidationError
from app.core.config import Settings, DatabaseSettings, TelegramSettings


class TestDatabaseSettings:
    """Test database settings configuration."""

    def test_default_database_settings(self):
        """Test default database settings."""
        settings = DatabaseSettings()

        assert settings.port == 5432
        assert settings.pool_size == 10
        assert settings.max_overflow == 20
        assert settings.echo is False

    def test_database_settings_from_env(self, monkeypatch):
        """Test database settings from environment variables."""
        monkeypatch.setenv("DB_HOST", "testhost")
        monkeypatch.setenv("DB_PORT", "5433")
        monkeypatch.setenv("DB_NAME", "testdb")
        monkeypatch.setenv("DB_USER", "testuser")
        monkeypatch.setenv("DB_PASSWORD", "testpass")

        settings = DatabaseSettings()

        assert settings.host == "testhost"
        assert settings.port == 5433
        assert settings.name == "testdb"
        assert settings.user == "testuser"
        assert settings.password == "testpass"


class TestTelegramSettings:
    """Test Telegram settings configuration."""

    def test_telegram_settings_validation(self):
        """Test Telegram settings validation."""
        # Valid settings
        settings = TelegramSettings(bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
        assert settings.bot_token == "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"

    def test_telegram_settings_from_env(self, monkeypatch):
        """Test Telegram settings from environment variables."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.com/webhook")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret123")

        settings = TelegramSettings()

        assert settings.bot_token == "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        assert settings.webhook_url == "https://example.com/webhook"
        assert settings.webhook_secret == "secret123"


class TestSettings:
    """Test main settings configuration."""

    def test_default_settings(self):
        """Test default settings values."""
        settings = Settings()

        assert settings.environment == "development"
        assert settings.debug is False
        assert settings.host == "0.0.0.0"
        assert settings.port == 8000
        assert settings.app_name == "Forex News Bot"
        assert settings.app_version == "2.0.0"

    def test_environment_validation(self):
        """Test environment validation."""
        # Valid environments
        valid_envs = ["development", "staging", "production"]
        for env in valid_envs:
            settings = Settings(environment=env)
            assert settings.environment == env

        # Invalid environment
        with pytest.raises(ValidationError):
            Settings(environment="invalid")

    def test_debug_validation(self):
        """Test debug mode validation."""
        # Debug can be True in development
        settings = Settings(environment="development", debug=True)
        assert settings.debug is True

        # Debug cannot be True in production
        with pytest.raises(ValidationError):
            Settings(environment="production", debug=True)

    def test_settings_from_env(self, monkeypatch):
        """Test settings from environment variables."""
        monkeypatch.setenv("ENVIRONMENT", "staging")
        monkeypatch.setenv("DEBUG", "true")
        monkeypatch.setenv("SERVER_HOST", "127.0.0.1")
        monkeypatch.setenv("SERVER_PORT", "9000")
        monkeypatch.setenv("APP_NAME", "Test Bot")

        settings = Settings()

        assert settings.environment == "staging"
        assert settings.debug is True
        assert settings.host == "127.0.0.1"
        assert settings.port == 9000
        assert settings.app_name == "Test Bot"

    def test_nested_settings(self):
        """Test nested settings configuration."""
        settings = Settings()

        # Test database settings
        assert isinstance(settings.database, DatabaseSettings)
        assert settings.database.port == 5432

        # Test Telegram settings
        assert isinstance(settings.telegram, TelegramSettings)

        # Test API settings
        assert isinstance(settings.api, type(settings.api))
        assert settings.api.openai_model == "gpt-4o-mini"
        assert settings.api.openai_temperature == 0.25
