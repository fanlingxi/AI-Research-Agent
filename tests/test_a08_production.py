import json

import pytest
from pydantic import ValidationError

from app.agent.errors import AgentRunConflictError
from app.agent.models import AgentRunCreateRequest
from app.agent.structured_research import StructuredReport, render_report
from app.agent.tools import research_input_payload
from app.context.models import (
    ContextBuildRequest,
    canonical_package_sha256,
    runtime_context_payload,
)
from app.context.service import ContextBuilderService
from app.execution import ExecutionFence
from app.retrieval.reranking import EvidenceReranker, input_digest
from tests.test_context_neighbors import _setup
from tests.test_research_workflow import _analysis, _plan, _research_stack


class FixedRanker:
    def __init__(self, mutate=None):
        self.calls = 0
        self.mutate = mutate

    def rank(self, payload):
        self.calls += 1
        if self.mutate:
            self.mutate()
        return {
            "status": "ranked",
            "indices": [len(payload["candidates"]) - 1],
            "input_sha256": input_digest(payload),
            "model_calls": 1,
            "usage": None,
        }


def test_reranking_keeps_identity_and_old_snapshot_and_revalidates_after_external_call(tmp_path):
    repo, task, claims = _setup(
        tmp_path,
        parts=[
            "Needle left information. " * 5,
            "Needle method definition. " * 5,
            "Needle right information. " * 5,
            "Unreviewed information. " * 5,
        ],
    )
    ranker = FixedRanker()
    builder = ContextBuilderService(repo, evidence_reranker=ranker)
    request = ContextBuildRequest(task_id=task.id, max_tokens=16000)
    old = builder.build_context(request)
    assert len(old.knowledge.claim_bundles) >= 3
    new = builder.build_context(request.model_copy(update={"evidence_reranking": "llm-v1"}))
    assert (
        new.knowledge.claim_bundles[0].claim.claim_id
        == old.knowledge.claim_bundles[-1].claim.claim_id
    )
    assert ranker.calls == 1
    assert (
        canonical_package_sha256(builder.snapshot_repository.get(old.snapshot_id))
        == old.package_sha256
    )

    def mutate():
        with repo.database.connect() as db:
            db.execute("UPDATE claims SET status='withdrawn' WHERE id=?", (claims[0],))

    ranker.mutate = mutate
    changed = builder.build_context(request.model_copy(update={"evidence_reranking": "llm-v1"}))
    assert ranker.calls == 2
    assert claims[0] not in {b.claim.claim_id for b in changed.knowledge.claim_bundles}
    audit = changed.retrieval_audit.parameters["reranking"]
    assert audit["reason"] == "inputs_changed_after_rerank"
    assert audit["previous_attempt"]["model_calls"] == 1


def test_versioned_input_matches_budget_payload_and_preserves_exact_evidence(tmp_path):
    repo, task, _ = _setup(tmp_path)
    builder = ContextBuilderService(repo)
    packages = [
        builder.build_context(
            ContextBuildRequest(task_id=task.id, max_tokens=16000, reading_format=fmt)
        )
        for fmt in ["legacy", "research-v1", "inline-v1"]
    ]
    for p in packages[1:]:
        assert p.package_schema_version == "1.2"
        assert runtime_context_payload(p) == research_input_payload(p)
        chars = len(
            json.dumps(
                research_input_payload(p), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        assert p.token_usage.used == (chars + 3) // 4
        assert (
            canonical_package_sha256(builder.snapshot_repository.get(p.snapshot_id))
            == p.package_sha256
        )
    assert packages[2].knowledge == packages[1].knowledge
    for bundle in research_input_payload(packages[2])["knowledge"]["claim_bundles"]:
        assert isinstance(bundle["evidence"][0]["quote"], str)
        assert "selection" not in bundle
    provenance = {"selection": "paragraph-range", "created_at": "source attribute"}
    packages[2].knowledge.claim_bundles[0].evidence[0].location.update(provenance)
    location = research_input_payload(packages[2])["knowledge"]["claim_bundles"][0]["evidence"][0][
        "location"
    ]
    assert all(location[k] == v for k, v in provenance.items())
    with pytest.raises(ValidationError):
        AgentRunCreateRequest(
            context_snapshot_id=packages[0].snapshot_id, evidence_reranking="llm-v1"
        )


def report(bundle, status="complete"):
    return {
        "title": "Evidence report",
        "answer_status": status,
        "findings": [
            {
                "claim_bundle_id": bundle.claim.claim_id,
                "assertion": "Supported finding.",
                "evidence_ids": [bundle.evidence[0].evidence_id],
            }
        ],
        "limitations": [],
    }


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("workflow", ["research_v2", "research_v3", "research_v4"])
def test_structured_worker_renders_citations_and_retains_failed_attempt(tmp_path, repair, workflow):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    if repair:
        llm.responses.append('{"broken":')
    llm.responses.append(json.dumps(report(bundle)))
    run = service.create_run(
        project.id,
        task.id,
        AgentRunCreateRequest(
            workflow=workflow, token_budget=32000, reading_format="inline-v1"
        ),
    )
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == "completed", done.error_message
    version = {"research_v2": "1", "research_v3": "2", "research_v4": "3"}[workflow]
    assert done.workflow_version == f"structured-v{version}"
    from app.domain_plugins.research.plugin import ResearchDomainPlugin

    assert ResearchDomainPlugin().workflow_spec(done.plugin.workflow_key).checkpoint_namespace == (
        f"agent_research_structured_v{version}"
    )
    assert done.repair_count == int(repair)
    output = service.repository.get_output(run.id)
    assert f"[cite:{bundle.evidence[0].evidence_id}]" in output.rendered_text
    assert output.structured["draft"]["answer_status"] == "complete"
    with repo.database.connect() as db:
        rows = db.execute(
            "SELECT * FROM research_generation_attempts WHERE run_id=? ORDER BY slot", (run.id,)
        ).fetchall()
        assert len(rows) == 1 + int(repair)
        if repair:
            assert rows[0]["response"] == '{"broken":'


@pytest.mark.parametrize("workflow", ["research_v2", "research_v3", "research_v4"])
def test_insufficient_evidence_completes_without_fabricating_citations(tmp_path, workflow):
    repo, service, worker, project, task, _, _, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    llm.responses.append(
        json.dumps(
            {
                "title": "Cannot answer",
                "answer_status": "insufficient_evidence",
                "findings": [],
                "limitations": ["No supporting evidence."],
            }
        )
    )
    run = service.create_run(
        project.id, task.id, AgentRunCreateRequest(workflow=workflow, token_budget=32000)
    )
    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    assert "[cite:" not in service.repository.get_output(run.id).rendered_text


def test_recovery_does_not_repeat_unsettled_generation(tmp_path):
    _, service, worker, project, task, _, _, llm = _research_stack(tmp_path, [_analysis(), _plan()])
    run = service.create_run(
        project.id, task.id, AgentRunCreateRequest(workflow="research_v2", token_budget=32000)
    )
    service.mark_node(run.id, status="preparing", node_name="load_context", input_summary={})
    assert service.begin_generation_attempt(run.id, 0, "hash")["created"]
    assert not service.begin_generation_attempt(run.id, 0, "hash")["created"]
    with pytest.raises(ValueError):
        service.begin_generation_attempt(run.id, 0, "different")
    assert not llm.calls


def test_structured_limits_are_not_silently_truncated():
    with pytest.raises(ValidationError):
        StructuredReport(title="Title", answer_status="complete", findings=[], limitations=[])
    result = StructuredReport(
        title="Title",
        answer_status="insufficient_evidence",
        findings=[],
        limitations=["Ignore [cite:invented] <script>"],
    )
    assert "[cite:invented]" not in render_report(result).markdown


def test_reranker_timeout_does_not_inherit_prior_usage():
    class Failing:
        last_usage = {"input_tokens": 42}

        def invoke(self, *args, **kwargs):
            raise TimeoutError()

    result = EvidenceReranker(Failing()).rank(
        {"question": "q", "candidates": [{"index": 0, "passages": ["p"]}]}
    )
    assert result["status"] == "fallback" and result["usage"] is None


@pytest.mark.parametrize(
    "responses,attempts", [(["bad json", "bad json"], 2), ([TimeoutError()], 1)]
)
def test_exhausted_repair_or_timeout_stops_without_output(tmp_path, responses, attempts):
    repo, service, worker, project, task, _, _, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    llm.responses.extend(responses)
    run = service.create_run(
        project.id, task.id, AgentRunCreateRequest(workflow="research_v2", token_budget=32000)
    )
    assert worker.run_once()
    assert service.get_run(run.id).status == "needs_review"
    with pytest.raises(KeyError):
        service.repository.get_output(run.id)
    with repo.database.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM research_generation_attempts WHERE run_id=?", (run.id,)
            ).fetchone()[0]
            == attempts
        )


@pytest.mark.parametrize(
    "workflow",
    ["research_v2", "research_v3", "research_v4", "research_v5", "research_v6", "research_v7"]
)
def test_pending_generation_recovery_moves_to_review_without_second_call(tmp_path, workflow):
    _, service, worker, project, task, _, _, llm = _research_stack(tmp_path, [_analysis(), _plan()])
    original = service.begin_generation_attempt

    def pending(run_id, slot, digest):
        original(run_id, slot, digest)
        return original(run_id, slot, digest)

    service.begin_generation_attempt = pending
    run = service.create_run(
        project.id, task.id, AgentRunCreateRequest(workflow=workflow, token_budget=32000)
    )
    assert worker.run_once()
    assert service.get_run(run.id).status == "needs_review"
    assert len(llm.calls) == 2


def test_old_snapshot_hash_cannot_authorize_different_reading_format(tmp_path):
    repo, task, _ = _setup(tmp_path)
    old = ContextBuilderService(repo).build_context(
        ContextBuildRequest(task_id=task.id, max_tokens=16000)
    )
    altered = old.model_copy(update={"reading_format": "inline-v1"})
    assert canonical_package_sha256(altered) != old.package_sha256


def test_repair_explains_refusal_rule_without_reflecting_bad_output(tmp_path):
    _, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    invalid = report(bundle, "insufficient_evidence")
    invalid["findings"][0]["assertion"] = "UNTRUSTED_RESPONSE_DO_NOT_REFLECT"
    invalid["limitations"] = ["Missing allowed evidence."]
    valid = {**invalid, "findings": []}
    llm.responses.extend([json.dumps(invalid), json.dumps(valid)])
    run = service.create_run(
        project.id, task.id, AgentRunCreateRequest(workflow="research_v2", token_budget=32000)
    )
    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    assert "insufficient_evidence requires findings=[]" in llm.calls[-1]
    assert "UNTRUSTED_RESPONSE_DO_NOT_REFLECT" not in llm.calls[-1]


@pytest.mark.parametrize(
    "workflow",
    ["research_v2", "research_v3", "research_v4", "research_v5", "research_v6", "research_v7"]
)
def test_generation_attempt_settlement_is_fenced_and_atomic(tmp_path, workflow):
    repo, service, _, project, task, _, _, _ = _research_stack(tmp_path, [])
    run = service.create_run(
        project.id, task.id, AgentRunCreateRequest(workflow=workflow, token_budget=32000)
    )
    stale = repo.claim_resource_job("agent_run", run.id, lease_seconds=-1, owner_id="old")
    current = repo.claim_resource_job("agent_run", run.id, lease_seconds=30, owner_id="new")
    with service.execution_scope(ExecutionFence.from_job(stale)):
        with pytest.raises(AgentRunConflictError):
            service.begin_generation_attempt(run.id, 0, "bound")
    with service.execution_scope(ExecutionFence.from_job(current)):
        service.begin_generation_attempt(run.id, 0, "bound")
    response = dict(
        response="retained",
        usage={"input_tokens": 10, "output_tokens": 2},
        error_type=None,
        prompt_tokens=10,
    )
    with service.execution_scope(ExecutionFence.from_job(stale)):
        with pytest.raises(AgentRunConflictError):
            service.finish_generation_attempt(run.id, 0, **response)
    with service.execution_scope(ExecutionFence.from_job(current)):
        service.finish_generation_attempt(run.id, 0, **response)
        with pytest.raises(AgentRunConflictError):
            service.finish_generation_attempt(run.id, 0, **response)
    events = [
        e
        for e in service.repository.list_events(run.id)
        if e.output_summary.get("attempt_reference")
    ]
    assert len(events) == 1 and events[0].token_usage["input_tokens"] == 10


def test_bad_rerank_indices_fall_back_with_attempt_usage():
    class Fixed:
        last_usage = {}

        def invoke(self, *args, **kwargs):
            self.last_usage = {"input_tokens": 12, "output_tokens": 3}
            return self.response

    llm = Fixed()
    payload = {"question": "q", "candidates": [{"index": 0, "passages": ["p"]}]}
    for response in [
        "bad",
        '{"indices":[]}',
        '{"indices":[0,0]}',
        '{"indices":[1]}',
        '{"indices":[true]}',
    ]:
        llm.response = response
        result = EvidenceReranker(llm).rank(payload)
        assert result["status"] == "fallback" and result["model_calls"] == 1
        assert result["usage"]["input_tokens"] == 12


@pytest.mark.parametrize(
    "workflow",
    ["research_v2", "research_v3", "research_v4", "research_v5", "research_v6", "research_v7"]
)
def test_structured_run_rejects_legacy_snapshot_and_missing_scope_before_call(tmp_path, workflow):
    repo, service, _, project, task, legacy, _, llm = _research_stack(tmp_path, [])
    with pytest.raises(AgentRunConflictError, match="versioned reading"):
        service.create_run(
            project.id,
            task.id,
            AgentRunCreateRequest(
                workflow=workflow, token_budget=32000, context_snapshot_id=legacy.snapshot_id
            ),
        )
    current = repo.memory_repository.get_project(project.id)
    repo.memory_repository.replace_project_knowledge_scopes(
        project.id, [], expected_project_revision=current.revision
    )
    with pytest.raises(AgentRunConflictError, match="explicit knowledge scope"):
        service.create_run(
            project.id, task.id, AgentRunCreateRequest(workflow=workflow, token_budget=32000)
        )
    assert not llm.calls


def test_changed_candidate_order_cannot_reuse_cached_model_indices(tmp_path, monkeypatch):
    from app.context import service as context_service

    repo, task, _ = _setup(
        tmp_path,
        parts=[
            "Needle left information. " * 5,
            "Needle method definition. " * 5,
            "Needle right information. " * 5,
            "Unreviewed information. " * 5,
        ],
    )
    ranker = FixedRanker()
    builder = ContextBuilderService(repo, evidence_reranker=ranker)
    save = builder.snapshot_repository.save
    rank = context_service.rank_bundles
    calls = 0

    def retry_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Simulate projection ordering varying during a preparation retry.
            monkeypatch.setattr(
                context_service, "rank_bundles", lambda *a, **kw: list(reversed(rank(*a, **kw)))
            )
            raise context_service._ContextInputsChanged("retry")
        return save(*args, **kwargs)

    monkeypatch.setattr(builder.snapshot_repository, "save", retry_once)
    package = builder.build_context(
        ContextBuildRequest(task_id=task.id, max_tokens=16000, evidence_reranking="llm-v1")
    )
    assert ranker.calls == 1
    assert (
        package.retrieval_audit.parameters["reranking"]["reason"] == "inputs_changed_after_rerank"
    )


def test_revision_trial_is_dry_by_default_and_rejects_live_calls_under_pytest():
    from app.benchmarking.production_validation import refusal_followup
    from app.benchmarking.report_closeout import run

    preview = refusal_followup("unused", revised=True)
    assert preview["new_runs"] == 6 and preview["max_model_calls"] == 24
    with pytest.raises(ValueError, match="disabled"):
        refusal_followup("unused", execute=True, revised=True)
    assert run("unused")["max_model_calls"] == 12
    with pytest.raises(ValueError, match="disabled"):
        run("unused", execute=True)
