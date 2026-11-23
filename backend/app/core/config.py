from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTSCOPE_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./agentscope.db"
    demo_mode: bool = True
    admin_token: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    max_request_bytes: int = 5_000_000
    max_spans_per_request: int = 2_000
    retention_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
