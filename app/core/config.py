"""Application configuration using Pydantic Settings (SMC watcher only)."""

from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelegramSettings(BaseSettings):
    """Telegram bot configuration."""

    bot_token: str = Field(
        default="your-telegram-bot-token-here", description="Telegram bot token"
    )
    chat_id: Optional[str] = Field(default=None, description="Owner chat ID")

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")


class OandaSettings(BaseSettings):
    """OANDA v20 API configuration (forex market data)."""

    api_token: Optional[str] = Field(default=None, description="OANDA API token")
    environment: Literal["practice", "live"] = Field(
        default="practice", description="OANDA environment"
    )

    model_config = SettingsConfigDict(env_prefix="OANDA_")


class SMCSettings(BaseSettings):
    """Triple Sync + Imbalance strategy watcher configuration."""

    pairs: str = Field(
        default="ETHUSD,USDJPY",
        description="Comma-separated default pairs (runtime changes via /pairs)",
    )
    interval_minutes: int = Field(default=15, description="Check interval in minutes")
    min_rr: float = Field(default=2.0, description="Minimum risk/reward ratio")
    risk_pct: float = Field(default=2.0, description="Risk percent per trade")
    deposit: Optional[float] = Field(
        default=None, description="Deposit size in USD for lot calculation"
    )
    enforce_sessions: bool = Field(
        default=True, description="Only look for entries inside session windows"
    )
    notify_no_setup: bool = Field(
        default=False,
        description="Send 15-min heartbeat messages when no setup is found "
        "(off: only setup alerts go to Telegram, checks are logged)",
    )
    chat_id: Optional[str] = Field(
        default=None,
        description="Telegram chat id override (falls back to TELEGRAM_CHAT_ID)",
    )

    def default_pairs(self) -> list[str]:
        return [p.strip().upper() for p in self.pairs.split(",") if p.strip()]

    model_config = SettingsConfigDict(env_prefix="SMC_")


class LoggingSettings(BaseSettings):
    """Logging configuration."""

    level: str = Field(default="INFO", description="Log level")
    format: str = Field(
        default="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        description="Log format",
    )
    file_path: Optional[str] = Field(default=None, description="Log file path")

    model_config = SettingsConfigDict(env_prefix="LOG_")


class Settings(BaseSettings):
    """Main application settings."""

    app_name: str = Field(default="SMC Watcher", description="Application name")
    app_version: str = Field(default="3.0.0", description="Application version")
    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Environment"
    )
    debug: bool = Field(default=False, description="Debug mode")

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    oanda: OandaSettings = Field(default_factory=OandaSettings)
    smc: SMCSettings = Field(default_factory=SMCSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


# Global settings instance
settings = Settings()
