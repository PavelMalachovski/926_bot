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


class TwelveDataSettings(BaseSettings):
    """Twelve Data API configuration (forex + crypto market data)."""

    api_key: Optional[str] = Field(default=None, description="Twelve Data API key")

    model_config = SettingsConfigDict(env_prefix="TWELVEDATA_")


class SMCSettings(BaseSettings):
    """Triple Sync + Imbalance strategy watcher configuration."""

    pairs: str = Field(
        default="ETHUSD,USDJPY",
        description="Comma-separated default pairs (runtime changes via /pairs)",
    )
    interval_minutes: int = Field(
        default=15, description="Check interval outside sessions, minutes"
    )
    session_interval_minutes: int = Field(
        default=5,
        description="Check interval inside session windows (M5 cadence), minutes",
    )
    min_rr: float = Field(default=2.0, description="Minimum risk/reward ratio")
    risk_pct: float = Field(default=2.0, description="Risk percent per trade")
    deposit: Optional[float] = Field(
        default=None, description="Deposit size in USD for lot calculation"
    )
    enforce_sessions: bool = Field(
        default=True, description="Only look for entries inside session windows"
    )
    forex_source: str = Field(
        default="auto",
        description="Forex data source: auto | twelvedata | oanda | yahoo. "
        "auto = Twelve Data if its key is set, else OANDA if its token is set, "
        "else keyless Yahoo",
    )
    taken_cooldown_hours: float = Field(
        default=4.0,
        description="After you press 'Took it', mute new alerts for that pair "
        "for this many hours (you are managing the position)",
    )
    zone_ping: bool = Field(
        default=True,
        description="Send a 'get ready' ping when price first reaches a live "
        "H1 zone, before the full setup forms",
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
    news_enabled: bool = Field(
        default=True, description="Block entries around Forex Factory red news"
    )
    news_digest: bool = Field(
        default=True, description="Send a morning red-news digest before trading"
    )
    news_digest_time: str = Field(
        default="07:45", description="Prague local time (HH:MM) for the digest"
    )
    news_blackout_before_min: int = Field(
        default=60, description="No-entry window before a red news release, minutes"
    )
    news_blackout_after_min: int = Field(
        default=15, description="No-entry window after a red news release, minutes"
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
    twelvedata: TwelveDataSettings = Field(default_factory=TwelveDataSettings)
    smc: SMCSettings = Field(default_factory=SMCSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


# Global settings instance
settings = Settings()
