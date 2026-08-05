import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_deepseek_model_accepts_project_models() -> None:
    assert Settings(deepseek_model="deepseek-v4-flash").deepseek_model == "deepseek-v4-flash"
    assert Settings(deepseek_model="deepseek-v4-pro").deepseek_model == "deepseek-v4-pro"


def test_deepseek_model_rejects_legacy_names() -> None:
    with pytest.raises(ValidationError):
        Settings(deepseek_model="deepseek-chat")


def test_phase_43_defaults_are_safe_for_local_development() -> None:
    settings = Settings()

    assert settings.memory_backend == "json"
    assert not settings.obsidian_export_enabled
    assert settings.obsidian_vault_path == "data/obsidian_vault"
