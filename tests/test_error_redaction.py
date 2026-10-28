from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import error_redaction
from app.agent.errors import AgentRunConflictError
from app.api import errors as api_errors
from app.api.main import create_app
from app.benchmarking import sut
from app.config.settings import Settings
from app.error_redaction import ERROR_MAX_LENGTH, redact_error, redact_error_payload
from app.knowledge.extractor import LiveLLMRequiredError
from tests.test_agent_runtime import _foundation_request, _runtime_stack
from tests.test_reports import _report_stack


@pytest.fixture
def secret_settings(monkeypatch):
    settings = Settings(
        _env_file=None,
        openai_api_key="configured-openai-value",
        qwen_api_key="configured-qwen-value",
        deepseek_api_key="configured-deepseek-value",
        neo4j_password="configured-db-value",
        openai_base_url="https://proxy-user:proxy-pass@gateway.test/v1?key=gateway-value",
    )
    monkeypatch.setattr(error_redaction, "get_settings", lambda: settings)
    return settings


def test_configured_secrets_from_all_providers_and_gateway(secret_settings):
    secrets = [
        secret_settings.openai_api_key, secret_settings.qwen_api_key,
        secret_settings.deepseek_api_key, secret_settings.neo4j_password,
        "proxy-pass", "gateway-value",
    ]
    diagnostic = redact_error(RuntimeError("provider unavailable: " + " / ".join(secrets)))
    assert diagnostic.startswith("provider unavailable:")
    assert all(secret not in diagnostic for secret in secrets)
    assert diagnostic.count("[REDACTED]") == len(secrets)


def test_injected_and_encoded_settings_and_overlapping_secrets(secret_settings):
    settings = Settings(
        _env_file=None, openai_api_key="opaque:/key+with space",
        qwen_api_key="overlap8", deepseek_api_key="overlap8-longer",
    )
    text = " | ".join([
        settings.openai_api_key, quote(settings.openai_api_key, safe=""),
        settings.qwen_api_key, settings.deepseek_api_key, secret_settings.openai_api_key,
    ])
    assert redact_error(text, settings=settings) == " | ".join(["[REDACTED]"] * 5)


def test_additional_gateway_settings_are_discovered(secret_settings):
    class GatewaySettings(Settings):
        gateway_api_key: str = "gateway-setting-value"
        gateway_token: str = "gateway-token-value"

    settings = GatewaySettings(_env_file=None)
    assert redact_error(settings.gateway_api_key, settings=settings) == "[REDACTED]"
    assert redact_error(settings.gateway_token, settings=settings) == "[REDACTED]"


@pytest.mark.parametrize("value", ["neo4j", "password", "changeme", "PASSWORD", "short12"])
def test_short_and_placeholder_settings_preserve_prose(value, monkeypatch):
    settings = Settings(
        _env_file=None, neo4j_password=value, openai_api_key=value,
        openai_base_url=f"https://user:{value}@proxy.test/v1?key={value}",
    )
    monkeypatch.setattr(error_redaction, "get_settings", lambda: settings)
    text = f"无法连接 neo4j; diagnostic word: {value}"
    assert redact_error(text, settings=settings) == text
    assert redact_error(f"Bearer {value}") == "Bearer [REDACTED]"
    assert redact_error(f"key={value}") == "key=[REDACTED]"
    assert redact_error(settings.openai_base_url) == (
        "https://[REDACTED]@proxy.test/v1?key=[REDACTED]"
    )


def test_configured_secret_minimum_length(monkeypatch):
    settings = Settings(_env_file=None, neo4j_password="long-db-secret", openai_api_key="a1b2c3d4")
    monkeypatch.setattr(error_redaction, "get_settings", lambda: settings)
    assert redact_error("无法连接 neo4j: long-db-secret a1b2c3d4") == (
        "无法连接 neo4j: [REDACTED] [REDACTED]"
    )


@pytest.mark.parametrize("text", [
    "sort_key: created_at", "primary key", "primary_key=id", '"sort_key": "created_at"',
    "keyboard: enabled", "monkey=animal", "keyspace=public", "cache_key: report-id",
    "https://proxy.test/v1?sort_key=created_at&monkey=animal",
])
def test_noncredential_fields_are_unchanged(text, secret_settings):
    assert redact_error(text) == text


@pytest.mark.parametrize("error", [
    ValueError("所选范围内没有已审核、已发布的论文证据。"),
    KeyError("missing item"), AgentRunConflictError("任务已存在。"),
])
def test_exception_type_is_opt_in(error, secret_settings):
    assert redact_error(error) == str(error)
    assert redact_error_payload({"message": error}) == {"message": str(error)}
    assert redact_error(error, include_type=True) == f"{type(error).__name__}: {error}"
    assert redact_error("message", include_type=True) == "message"
    assert redact_error(None, include_type=True) is None


@pytest.mark.parametrize("callback", [
    api_errors.memory_call, api_errors.agent_call, api_errors.domain_plugin_call,
    api_errors.game_knowledge_call, api_errors.workspace_call,
])
@pytest.mark.parametrize("error", [KeyError("missing item"), ValueError("业务校验未通过。")])
def test_api_wrappers_preserve_exact_messages(callback, error, secret_settings):
    def fail():
        raise error

    with pytest.raises(HTTPException) as caught:
        callback(fail)
    expected_status = 404 if isinstance(error, KeyError) else (
        409 if callback is api_errors.memory_call else 422
    )
    assert caught.value.status_code == expected_status
    assert caught.value.detail == str(error)


def test_api_conflict_retains_exact_message(secret_settings):
    def fail():
        raise AgentRunConflictError("任务已存在。")

    with pytest.raises(HTTPException) as caught:
        api_errors.agent_call(fail)
    assert caught.value.status_code == 409
    assert caught.value.detail == "任务已存在。"


def test_benchmark_preserves_existing_type_prefix_and_long_message(monkeypatch, secret_settings):
    def fail(*args, **kwargs):
        raise RuntimeError("benchmark failure " + "x" * 5000)

    monkeypatch.setattr(sut, "_retrieval", fail)
    monkeypatch.setattr(sut, "_business_counts", lambda fixture: {})
    assert sut.execute_case(None, SimpleNamespace(scenario="retrieval_allowed")) == {
        "status": "exception", "exception": "RuntimeError: benchmark failure " + "x" * 5000,
        "business_counts": {},
    }


@pytest.mark.parametrize(("text", "secrets"), [
    ("upstream Bearer unconfigured-bearer-value timeout", ["unconfigured-bearer-value"]),
    ("Authorization: Bearer header-value", ["header-value"]),
    ('{"Authorization": "Basic dXNlcjpwYXNz"}', ["dXNlcjpwYXNz"]),
    ("Authorization=opaque-header", ["opaque-header"]),
    ("Authorization: Token token-header-value", ["token-header-value"]),
    ("Proxy-Authorization: Basic proxy-credential", ["proxy-credential"]),
    ("upstream sk-proj-unknown_123 failure", ["sk-proj-unknown_123"]),
    ("api_key=unknown-api-value; status=503", ["unknown-api-value"]),
    ('{"api_key": "unknown quoted value"}', ["unknown quoted value"]),
    ("x-api-key: other-header-value", ["other-header-value"]),
    ("https://user:p%40ss@proxy.test/v1?api_key=query-value&safe=ok",
     ["user", "p%40ss", "query-value"]),
    ("bolt://db-user:db-pass@localhost:7687", ["db-user", "db-pass"]),
    ("https://proxy.test/v1?access_token=query-token&safe=ok", ["query-token"]),
    ("https://proxy.test/v1?api%5Fkey=encoded-name&token=second", ["encoded-name", "second"]),
])
def test_common_credential_forms(text, secrets, secret_settings):
    diagnostic = redact_error(text)
    assert all(secret not in diagnostic for secret in secrets)
    assert "[REDACTED]" in diagnostic
    assert redact_error(diagnostic) == diagnostic
    if "safe=ok" in text:
        assert "safe=ok" in diagnostic


def test_bounded_diagnostic_redacts_before_truncation(secret_settings):
    secret = secret_settings.openai_api_key
    diagnostic = redact_error(
        ValueError("x" * (ERROR_MAX_LENGTH - 20) + secret * 100), include_type=True
    )
    assert len(diagnostic) == ERROR_MAX_LENGTH
    assert diagnostic.startswith("ValueError:") and diagnostic.endswith("...")
    assert secret[:8] not in diagnostic
    assert redact_error(None) is None
    assert redact_error("missing PDF page 2") == "missing PDF page 2"


def test_structured_error_preserves_codes(secret_settings):
    result = redact_error_payload({
        "code": "runtime_execution_failed",
        "message": secret_settings.qwen_api_key,
        "details": [{"message": "Bearer nested-secret", "attempt": 2}],
    })
    assert result["code"] == "runtime_execution_failed"
    assert result["details"][0]["attempt"] == 2
    assert "nested-secret" not in json.dumps(result)
    assert secret_settings.qwen_api_key not in json.dumps(result)


def test_provider_failure_is_redacted_in_report_job_and_api(tmp_path, secret_settings):
    repository, ingestion, reports, worker, _ = _report_stack(tmp_path)
    # Exercise both process settings and a provider injected with separate settings.
    reports.settings.openai_api_key = "injected-provider-key"
    secrets = [secret_settings.openai_api_key, secret_settings.qwen_api_key,
               secret_settings.deepseek_api_key, "injected-provider-key", "bearer-value",
               "url-user", "url-pass", "query-value"]
    message = (
        "provider unavailable status=503 " + " ".join(secrets[:4])
        + " Authorization: Bearer bearer-value "
        + "https://url-user:url-pass@proxy.test/v1?token=query-value&safe=ok"
    )

    class FailingProvider:
        provider_name = "openai"

        def invoke(self, prompt, system_prompt=None):
            raise RuntimeError(message)

    reports.llm = FailingProvider()
    report = reports.submit(
        query="How is the method verified?", topic_slugs=[ingestion.topic_slug],
        top_k=2, report_depth="standard",
    )
    assert worker.run_once()
    with repository._connect() as connection:
        stored = connection.execute("SELECT status, error FROM reports WHERE id = ?",
                                    (report.id,)).fetchone()
        job = connection.execute(
            "SELECT id, status, last_error FROM knowledge_jobs WHERE resource_id = ?",
            (report.id,),
        ).fetchone()
    assert stored["status"] == job["status"] == "failed"
    for diagnostic in (stored["error"], job["last_error"]):
        assert all(secret not in diagnostic for secret in secrets)
        assert "RuntimeError: provider unavailable status=503" in diagnostic

    app = create_app(knowledge_repository=repository, report_service=reports,
                     knowledge_service=worker.ingestion_service)
    with TestClient(app) as client:
        for endpoint in (f"/api/reports/{report.id}", "/api/reports",
                         "/api/v1/runtime/work?status=failed"):
            response = client.get(endpoint)
            assert response.status_code == 200
            assert all(secret not in response.text for secret in secrets)
            assert "provider unavailable status=503" in response.text

        # Older, already persisted raw messages must also be safe on reads.
        legacy_message = message.replace("injected-provider-key", "prior failure")
        with repository._connect() as connection:
            connection.execute("UPDATE reports SET error = ? WHERE id = ?",
                               (legacy_message, report.id))
            connection.execute("UPDATE knowledge_jobs SET last_error = ? WHERE id = ?",
                               (legacy_message, job["id"]))
        for endpoint in (f"/api/reports/{report.id}", "/api/v1/runtime/work?status=failed"):
            response = client.get(endpoint)
            assert response.status_code == 200
            assert all(secret not in response.text for secret in secrets)


def test_agent_failure_redacts_run_event_and_job_without_changing_code(tmp_path, secret_settings):
    def fail(node_name, run_id):
        raise RuntimeError("provider timeout " + secret_settings.deepseek_api_key)

    repository, service, _, worker, project, task, _ = _runtime_stack(
        tmp_path, failure_injector=fail
    )
    run = service.create_run(project.id, task.id, _foundation_request())
    assert worker.run_once()
    with repository._connect() as connection:
        stored = connection.execute(
            "SELECT error_code, error_message FROM agent_runs WHERE id = ?", (run.id,)
        ).fetchone()
        event = connection.execute(
            "SELECT error_json FROM agent_run_events "
            "WHERE run_id = ? AND event_type = 'run_failed'",
            (run.id,),
        ).fetchone()
        job = connection.execute(
            "SELECT last_error FROM knowledge_jobs WHERE resource_id = ?", (run.id,)
        ).fetchone()
    assert stored["error_code"] == "runtime_execution_failed"
    assert json.loads(event["error_json"])["code"] == stored["error_code"]
    for diagnostic in (stored["error_message"], event["error_json"], job["last_error"]):
        assert secret_settings.deepseek_api_key not in diagnostic
        assert "RuntimeError: provider timeout" in diagnostic


@pytest.mark.parametrize("message", [
    "invalid request", "所选范围内没有已审核、已发布的论文证据。",
])
def test_api_exception_retains_status_and_redacts_detail(
    tmp_path, monkeypatch, secret_settings, message,
):
    repository, ingestion, reports, worker, _ = _report_stack(tmp_path)

    def fail(**kwargs):
        raise ValueError(message + " " + secret_settings.openai_api_key)

    monkeypatch.setattr(reports, "submit", fail)
    app = create_app(knowledge_repository=repository, report_service=reports,
                     knowledge_service=worker.ingestion_service)
    with TestClient(app) as client:
        response = client.post("/api/reports", json={
            "query": "How is the method verified?", "topic_slugs": [ingestion.topic_slug],
        })
    assert response.status_code == 422
    assert response.json()["detail"] == message + " [REDACTED]"
    assert secret_settings.openai_api_key not in response.text


@pytest.mark.parametrize("error", [
    ValueError("所选范围内没有已审核、已发布的论文证据。"),
    KeyError("missing evidence"), LiveLLMRequiredError("研究报告需要配置真实 LLM。"),
])
@pytest.mark.parametrize("stage", ["query", "dispatch"])
def test_report_business_failure_preserves_stored_message(tmp_path, monkeypatch, error, stage):
    repository, ingestion, reports, worker, _ = _report_stack(tmp_path)

    def fail(*args, **kwargs):
        raise error

    if stage == "query":
        monkeypatch.setattr(reports.query_service, "search", fail)
    else:
        monkeypatch.setattr(reports, "execute_claimed", fail)
    report = reports.submit(query="Research question", topic_slugs=[ingestion.topic_slug])
    assert worker.run_once()
    assert repository.reports.get_report(report.id).error == str(error)
    assert repository.jobs.get_resource_job("report", report.id).last_error == str(error)
