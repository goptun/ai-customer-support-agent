from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jwt_secret: str
    jwt_ttl_seconds: int = 3600
    working_state_ttl_seconds: int = 1800
    working_state_window: int = 20

    @property
    def checkpointer_url(self) -> str:
        """libpq-style URL for the LangGraph Postgres checkpointer (no SQLAlchemy driver)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
