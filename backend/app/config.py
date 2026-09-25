from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "TravelAgent API"
    app_version: str = "0.1.0"
    debug: bool = False
    api_prefix: str = "/api"
    database_url: str = "sqlite:///./travelagent.db"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    
    # LLM settings
    openai_api_key: str = ""
    openai_api_base: str = ""
    openai_model: str = "gpt-4"
    openai_temperature: float = 0.7
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 4000


@lru_cache
def get_settings() -> Settings:
    return Settings()

