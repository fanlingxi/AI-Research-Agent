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

    search_live_enabled: bool = False
    paper_search_limit: int = 5

    chunk_size: int = 900
    chunk_overlap: int = 150

    embedding_provider: Literal["hash", "openai"] = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 384

    vector_store_provider: Literal["memory", "qdrant"] = "memory"
    retrieval_top_k: int = 5

    graph_store_provider: Literal["memory", "neo4j"] = "memory"
    graph_entities_per_chunk: int = 8
    graph_max_hops: int = 2
    graph_top_k: int = 5

    memory_enabled: bool = True
    memory_backend: Literal["json"] = "json"
    memory_path: str = "data/memory/research_memory.json"
    memory_recall_limit: int = 3

    evaluation_passing_score: float = 0.72
    evaluation_critical_min_score: float = 0.65
    critic_min_score: float = 0.78
    evidence_source_min_score: float = 0.65

    obsidian_export_enabled: bool = False
    obsidian_vault_path: str = "data/obsidian_vault"
    obsidian_review_status: str = "pending"

    graphrag_query_mode: Literal["local", "global", "hybrid"] = "hybrid"
    graph_ranking_strategy: Literal["path_score", "personalized_pagerank"] = "path_score"

    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "research_chunks"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = Field(default="password", repr=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
