from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jwt_secret: str
    jwt_ttl_seconds: int = 3600
    working_state_ttl_seconds: int = 1800
    working_state_window: int = 20
    # anthropic: the Claude API. openai_compatible: any chat completions endpoint, e.g. the
    # 9router on the VPS (ADR-0004); the model names below are then that endpoint's.
    llm_provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    anthropic_api_key: SecretStr | None = None
    llm_base_url: str | None = None  # with /v1, e.g. http://router:20128/v1
    llm_api_key: SecretStr | None = None
    # Per request, before the SDK's retries: a combo may take tens of seconds per answer.
    llm_timeout_seconds: float = 180.0
    classifier_model: str = "claude-haiku-4-5"
    responder_model: str = "claude-sonnet-5-5"
    logistics_url: str = "http://logistics:8080"
    logistics_timeout_seconds: float = 3.0
    tool_retries: int = 2
    tool_retry_backoff_seconds: float = 0.5
    tool_failures_before_escalation: int = 2
    clarification_confidence: float = 0.7
    clarifications_before_escalation: int = 2
    recent_order_days: int = 30
    recent_orders_limit: int = 5
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    knowledge_top_k: int = 3
    # Cosine similarity below which a chunk doesn't answer the question (tuned on the seed).
    knowledge_min_score: float = 0.45
    # Refund Policy: eligible if delivered at most this many days ago...
    refund_delivery_window_days: int = 30
    # ...or if the Shipment is more than this many days past its promised date.
    refund_late_after_days: int = 10

    @property
    def checkpointer_url(self) -> str:
        """libpq-style URL for the LangGraph Postgres checkpointer (no SQLAlchemy driver)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
