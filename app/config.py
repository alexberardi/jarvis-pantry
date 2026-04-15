"""Pydantic settings for the Jarvis Pantry service."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    pantry_port: int = Field(7721, alias="PANTRY_PORT")
    database_url: str = Field("postgresql://postgres:postgres@localhost:5432/jarvis_pantry", alias="DATABASE_URL")

    # GitHub OAuth
    github_client_id: str = Field("", alias="GITHUB_CLIENT_ID")
    github_client_secret: str = Field("", alias="GITHUB_CLIENT_SECRET")

    # Abuse alerting
    alert_webhook_url: str = Field("", alias="ALERT_WEBHOOK_URL")

    # Admin
    admin_api_key: str = Field("", alias="ADMIN_API_KEY")

    # Rate limiting
    rate_limit_per_ip_per_hour: int = Field(100, alias="RATE_LIMIT_PER_IP_PER_HOUR")
    submission_rate_limit_per_hour: int = Field(10, alias="SUBMISSION_RATE_LIMIT_PER_HOUR")
    submission_rate_limit_per_user_per_hour: int = Field(
        15, alias="SUBMISSION_RATE_LIMIT_PER_USER_PER_HOUR",
    )

    # Validation pipeline
    max_concurrent_container_tests: int = Field(3, alias="MAX_CONCURRENT_CONTAINER_TESTS")
    max_concurrent_clones: int = Field(5, alias="MAX_CONCURRENT_CLONES")
    sdk_path: str = Field("/app/jarvis-command-sdk", alias="JARVIS_SDK_PATH")
    container_test_timeout: int = Field(180, alias="CONTAINER_TEST_TIMEOUT")
    bypass_llm_key: bool = Field(False, alias="BYPASS_LLM_KEY")

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except Exception:
        return Settings(_env_file=None)
