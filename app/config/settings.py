from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_DEEPSEEK_MODELS = {"deepseek-v4-pro", "deepseek-v4-flash"}


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["mock", "openai", "qwen", "deepseek"] = "mock"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.2
    llm_input_cost_per_million: float | None = None
    llm_output_cost_per_million: float | None = None

    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"

    qwen_api_key: str | None = None
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"

    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    @field_validator("deepseek_model")
    @classmethod
    def validate_deepseek_model(cls, value: str) -> str:
        if value not in SUPPORTED_DEEPSEEK_MODELS:
            allowed = ", ".join(sorted(SUPPORTED_DEEPSEEK_MODELS))
            raise ValueError(f"DEEPSEEK_MODEL 仅支持：{allowed}")
        return value

    chunk_size: int = 900
    chunk_overlap: int = 150

    embedding_provider: Literal["hash", "openai"] = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 384

    knowledge_db_path: str = "data/knowledge/knowledge.db"
    agent_checkpoint_path: str = "data/runtime/agent_checkpoints.db"
    knowledge_vault_path: str = "data/obsidian_vault_v2"
    knowledge_qdrant_collection: str = "knowledge_chunks_v2"
    knowledge_worker_lease_seconds: int = 180
    knowledge_worker_poll_seconds: float = 1.0
    report_min_citation_coverage: float = 0.9

    qdrant_url: str = "http://localhost:6333"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = Field(default="password", repr=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
