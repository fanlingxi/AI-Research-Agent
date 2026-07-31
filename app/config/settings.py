from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    deepseek_model: str = "deepseek-chat"

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

    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "research_chunks"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = Field(default="password", repr=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
