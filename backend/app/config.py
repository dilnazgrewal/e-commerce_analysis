"""Application settings, loaded from environment variables / the repo-root .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    database_url: str = "sqlite:///./offeriq.db"
    cloud_database_url: SecretStr | None = None  # only used by scripts/export_cloud_db.py

    # Secrets are SecretStr so they never show up in logs or reprs.
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    webhook_url: str | None = None

    anthropic_api_key: SecretStr | None = None
    claude_daily_token_budget: int = 2_000_000

    dashboard_password: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
