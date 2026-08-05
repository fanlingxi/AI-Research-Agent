import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_deepseek_model_accepts_project_models() -> None:
    assert Settings(deepseek_model="deepseek-v4-flash").deepseek_model == "deepseek-v4-flash"
    assert Settings(deepseek_model="deepseek-v4-pro").deepseek_model == "deepseek-v4-pro"


def test_deepseek_model_rejects_legacy_names() -> None:
    with pytest.raises(ValidationError):
        Settings(deepseek_model="deepseek-chat")


def test_knowledge_core_defaults_preserve_current_physical_storage_names() -> None:
    settings = Settings()

    assert settings.knowledge_db_path == "data/knowledge/knowledge.db"
    assert settings.knowledge_vault_path == "data/obsidian_vault_v2"
    assert settings.knowledge_qdrant_collection == "knowledge_chunks_v2"
    assert settings.knowledge_worker_lease_seconds > 0
    assert settings.report_min_citation_coverage == 0.9
