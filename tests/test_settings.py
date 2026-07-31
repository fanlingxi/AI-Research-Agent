import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_deepseek_model_accepts_project_models() -> None:
    assert Settings(deepseek_model="deepseek-v4-flash").deepseek_model == "deepseek-v4-flash"
    assert Settings(deepseek_model="deepseek-v4-pro").deepseek_model == "deepseek-v4-pro"


def test_deepseek_model_rejects_legacy_names() -> None:
    with pytest.raises(ValidationError):
        Settings(deepseek_model="deepseek-chat")
