from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import REAL_DATABASE, TEST_RUNTIME_ROOT, database_fingerprint

from app.config.settings import get_settings


def _assert_isolation_probe(environment_updates: dict[str, str], assertions: str) -> None:
    """Import conftest in a fresh process to exercise import-time isolation."""

    environment = os.environ.copy()
    environment.update(environment_updates)
    project_root = Path(__file__).resolve().parents[1]
    python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(project_root), str(project_root / "tests"), python_path) if item
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import conftest; "
                "from pathlib import Path; "
                "from app.config.settings import get_settings; "
                "settings = get_settings(); "
                + assertions
            ),
        ],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_application_defaults_are_redirected_before_imports(pytestconfig) -> None:
    settings = get_settings()

    assert settings.llm_provider == "mock"
    assert settings.knowledge_db_path == str(TEST_RUNTIME_ROOT / "knowledge.db")
    assert settings.knowledge_vault_path == str(TEST_RUNTIME_ROOT / "vault")
    assert settings.qdrant_url == "http://127.0.0.1:9"
    assert settings.neo4j_uri == "bolt://127.0.0.1:9"
    assert os.environ["RUN_LIVE_LLM_INTEGRATION"] == "0"
    assert os.environ["RUN_STORE_INTEGRATION"] == "0"
    assert REAL_DATABASE not in (TEST_RUNTIME_ROOT / "knowledge.db").parents
    assert database_fingerprint() == pytestconfig._real_database_baseline


def test_live_llm_opt_in_preserves_only_caller_llm_configuration() -> None:
    _assert_isolation_probe(
        {
            "RUN_LIVE_LLM_INTEGRATION": "1",
            "RUN_STORE_INTEGRATION": "0",
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "probe-live-model",
            "OPENAI_BASE_URL": "https://llm-probe.invalid/v1",
            "OPENAI_API_KEY": "probe-key-not-a-secret",
        },
        (
            "assert settings.llm_provider == 'openai'; "
            "assert settings.llm_model == 'probe-live-model'; "
            "assert settings.openai_base_url == 'https://llm-probe.invalid/v1'; "
            "assert settings.qdrant_url == 'http://127.0.0.1:9'; "
            "assert settings.neo4j_uri == 'bolt://127.0.0.1:9'; "
            "assert Path(settings.knowledge_db_path).is_relative_to(conftest.TEST_RUNTIME_ROOT)"
        ),
    )


def test_live_store_opt_in_preserves_store_configuration_but_not_default_llm() -> None:
    _assert_isolation_probe(
        {
            "RUN_LIVE_LLM_INTEGRATION": "0",
            "RUN_STORE_INTEGRATION": "1",
            "LLM_PROVIDER": "openai",
            "QDRANT_URL": "http://qdrant-probe.invalid:6333",
            "NEO4J_URI": "bolt://neo4j-probe.invalid:7687",
            "NEO4J_USERNAME": "probe-user",
            "NEO4J_PASSWORD": "probe-password-not-a-secret",
        },
        (
            "assert settings.llm_provider == 'mock'; "
            "assert settings.qdrant_url == 'http://qdrant-probe.invalid:6333'; "
            "assert settings.neo4j_uri == 'bolt://neo4j-probe.invalid:7687'; "
            "assert settings.neo4j_username == 'probe-user'; "
            "assert settings.neo4j_password == 'probe-password-not-a-secret'; "
            "assert Path(settings.knowledge_db_path).is_relative_to(conftest.TEST_RUNTIME_ROOT)"
        ),
    )


def test_importing_api_module_does_not_initialize_stores(
    tmp_path: Path, pytestconfig
) -> None:
    """Only an explicit create_app() call may initialize application stores."""

    runtime_root = tmp_path / "import-only"
    database_path = runtime_root / "knowledge.db"
    checkpoint_path = runtime_root / "agent_checkpoints.db"
    vault_path = runtime_root / "vault"
    environment = os.environ.copy()
    environment.update(
        {
            "LLM_PROVIDER": "mock",
            "KNOWLEDGE_DB_PATH": str(database_path),
            "AGENT_CHECKPOINT_PATH": str(checkpoint_path),
            "KNOWLEDGE_VAULT_PATH": str(vault_path),
            "XDG_CACHE_HOME": str(runtime_root / "cache"),
            "QDRANT_URL": "http://127.0.0.1:9",
            "NEO4J_URI": "bolt://127.0.0.1:9",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    project_root = Path(__file__).resolve().parents[1]
    python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(project_root), python_path) if item
    )

    result = subprocess.run(
        [sys.executable, "-c", """
import importlib.abc
import sys

class NoEvaluationTools(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'scripts' or fullname.startswith(('scripts.', 'app.benchmarking')):
            raise ImportError(f'Application requires development-only module: {fullname}')

sys.meta_path.insert(0, NoEvaluationTools())
import app.api.main
from pathlib import Path
from app.config.settings import get_settings

root = Path(get_settings().knowledge_db_path).parent
service = app.api.main.ExperimentReadService(
    root=root / 'evaluation', semantic_archive=root / 'observations',
)
assert service.list_experiments() == {'experiments': [], 'unavailable': []}
assert service.semantic_observation()['available'] is False
"""],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not database_path.exists()
    assert not checkpoint_path.exists()
    assert not vault_path.exists()
    assert database_fingerprint() == pytestconfig._real_database_baseline


def test_browser_fixture_configures_isolation_before_app_import(
    tmp_path: Path, pytestconfig
) -> None:
    """The uvicorn fixture must not rely on pytest's process-level setup."""

    fixture_root = tmp_path / "browser-fixture"
    unsafe_default = tmp_path / "would-be-default.db"
    environment = os.environ.copy()
    environment.update(
        {
            "E2E_DB_PATH": str(fixture_root / "knowledge.db"),
            "E2E_VAULT_PATH": str(fixture_root / "vault"),
            "E2E_CHECKPOINT_PATH": str(fixture_root / "agent_checkpoints.db"),
            # The fixture must override an inherited default database path.
            "KNOWLEDGE_DB_PATH": str(unsafe_default),
            "LLM_PROVIDER": "openai",
        }
    )
    project_root = Path(__file__).resolve().parents[1]
    python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(project_root), python_path) if item
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import tests.e2e_server; "
                "from app.config.settings import get_settings; "
                "settings = get_settings(); "
                "assert settings.llm_provider == 'mock'; "
                "assert settings.knowledge_db_path == __import__('os').environ['E2E_DB_PATH']"
            ),
        ],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (fixture_root / "knowledge.db").exists()
    assert not unsafe_default.exists()
    assert database_fingerprint() == pytestconfig._real_database_baseline
