"""Shared evidence ownership, frozen v8 generation, and durable v10 recovery."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.agent.feedback_models import FeedbackRequest
from app.agent.models import AgentRunCreateRequest
from app.agent.research_workflow import validate_research_draft
from app.agent.structured_research import (
    AnchoredDirectResearchWorkflow,
    ReportContractError,
    SharedEvidenceResearchWorkflow,
)
from app.domain_plugins.research.plugin import ResearchDomainPlugin
from app.research_profiles import STANDARD
from tests.test_research_v8 import prompt_for
from tests.test_research_v9 import _report
from tests.test_research_workflow import _analysis, _plan, _research_stack

QUOTE = "Shared evidence retains a traceable claim-to-evidence chain."


def shared_fixture():
    bundles = [SimpleNamespace(claim=SimpleNamespace(claim_id=cid), evidence=[
        SimpleNamespace(evidence_id="shared", quote=QUOTE),
    ]) for cid in ("first", "second")]
    package = SimpleNamespace(task=SimpleNamespace(title="Requested fact", goal="Requested fact"),
                              knowledge=SimpleNamespace(claim_bundles=bundles))
    value = {"title": "Shared report", "answer_parts": [{
        "question_part": "Requested fact", "gap": "", "findings": [{
            "claim_bundle_id": "first", "assertion": "Supported assertion",
            "evidence_ids": ["shared"], "scope": "",
            "support": [{"evidence_id": "shared", "quote": QUOTE}],
        }],
    }]}
    return package, value


def prepare(cls, value, package):
    workflow = cls.__new__(cls)
    return workflow.prepare_report(workflow.parse_report(deepcopy(value)), package)


def test_shared_evidence_passes_v10_and_keeps_v8_rejection_and_order_behavior():
    package, value = shared_fixture()
    with pytest.raises(ReportContractError, match="own bundle"):
        prepare(AnchoredDirectResearchWorkflow, value, package)
    first = prepare(SharedEvidenceResearchWorkflow, value, package)
    assert validate_research_draft(first, package).passed
    package.knowledge.claim_bundles.reverse()
    assert prepare(SharedEvidenceResearchWorkflow, value, package) == first
    assert prepare(AnchoredDirectResearchWorkflow, value, package) == first


@pytest.mark.parametrize("invalid", ["cross_bundle", "unknown", "forged", "reordered", "conflict"])
@pytest.mark.parametrize("reverse", [False, True])
def test_v10_rejects_invalid_ownership_and_quotes(invalid, reverse):
    package, value = shared_fixture()
    finding = value["answer_parts"][0]["findings"][0]
    if invalid == "cross_bundle":
        package.knowledge.claim_bundles[0].evidence = []
    elif invalid == "unknown":
        finding["evidence_ids"] = ["unknown"]
        finding["support"][0]["evidence_id"] = "unknown"
    elif invalid == "forged":
        finding["support"][0]["quote"] = "Entirely fabricated evidence content."
    elif invalid == "reordered":
        finding["support"][0]["quote"] = "claim-to-evidence chain. Shared evidence retains"
    else:
        package.knowledge.claim_bundles[1].evidence[0].quote = QUOTE + " Conflict."
    if reverse:
        package.knowledge.claim_bundles.reverse()
    with pytest.raises(ReportContractError):
        prepare(SharedEvidenceResearchWorkflow, value, package)


def test_v10_keeps_v8_prompt_schema_and_generation_methods():
    assert prompt_for(SharedEvidenceResearchWorkflow) == prompt_for(AnchoredDirectResearchWorkflow)
    assert (SharedEvidenceResearchWorkflow.report_model
            is AnchoredDirectResearchWorkflow.report_model)
    for method in ("_task_analysis", "_constrained_plan", "parse_report",
                   "_research_draft_validate_and_finalize", "compile"):
        assert getattr(SharedEvidenceResearchWorkflow, method) is getattr(
            AnchoredDirectResearchWorkflow, method
        )
    plugin = ResearchDomainPlugin()
    spec = plugin.workflow_spec("research_v10")
    assert (spec.version, spec.checkpoint_namespace) == (
        "structured-v9", "agent_research_structured_v9",
    )
    assert (spec.min_steps, spec.min_tool_calls, spec.requires_live_llm) == (9, 1, True)
    others = [w for w in plugin.manifest.workflows if w.key != "research_v10"]
    assert spec.version not in {w.version for w in others}
    assert spec.checkpoint_namespace not in {w.checkpoint_namespace for w in others}
    assert STANDARD.workflow == "research_v8"


def checkpoint(worker, run):
    with worker.agent_runtime.checkpoint_factory.open(
        namespace="agent_research_structured_v9"
    ) as saver:
        saved = saver.get_tuple({"configurable": {"thread_id": run.id}})
    assert saved is not None
    return saved.checkpoint["channel_values"]


@pytest.mark.parametrize("interruption", [None, "tool", "returned", "repair_returned", "pending"])
def test_v10_worker_recovery_keeps_v8_calls_and_never_duplicates(
    tmp_path, monkeypatch, interruption,
):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    if interruption == "repair_returned":
        llm.responses.append("invalid json")
    llm.responses.append(json.dumps(_report(bundle, task)))
    if interruption:
        method = {"tool": "record_tool_call", "returned": "finish_generation_attempt",
                  "repair_returned": "finish_generation_attempt",
                  "pending": "begin_generation_attempt"}[interruption]
        original = getattr(service, method)

        def interrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            if interruption != "repair_returned" or args[1] == 1:
                raise RuntimeError("Interrupted after durable write")
            return result

        monkeypatch.setattr(service, method, interrupt)
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow="research_v10", token_budget=32000, max_steps=16, max_tool_calls=1,
    ))
    assert service.load_context_snapshot(run.id).reading_format == "research-v1"
    assert worker.run_once()
    if interruption:
        assert service.get_run(run.id).status == "failed"
        before = len(llm.calls)
        tools = service.repository.list_tool_calls(run.id)
        assert checkpoint(worker, run)["llm_calls_used"] == 2
        monkeypatch.setattr(service, method, original)
        service.resume_run(run.id)
        assert worker.run_once()
        assert len(llm.calls) == before + (interruption == "tool")
        assert [t.id for t in tools] == [t.id for t in service.repository.list_tool_calls(run.id)]
    done = service.get_run(run.id)
    assert done.status == ("needs_review" if interruption == "pending" else "completed")
    assert done.workflow_version == "structured-v9"
    assert len(llm.calls) == (2 if interruption == "pending" else
                              4 if interruption == "repair_returned" else 3)
    assert done.tool_call_count == 1
    if done.status == "completed":
        assert checkpoint(worker, run)["llm_calls_used"] == len(llm.calls)
    with repo.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == (
            done.status == "completed"
        )


@pytest.mark.parametrize("workflow", ["research_v8", "research_v10"])
def test_finding_feedback_binds_v10_owner_and_preserves_old_anchors(
    tmp_path, monkeypatch, workflow,
):
    _, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    llm.responses.append(json.dumps(_report(bundle, task)))
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow=workflow, token_budget=32000,
    ))
    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    snapshots = service.context_builder.snapshot_repository
    original = snapshots.get

    def shared_snapshot(snapshot_id):
        package = original(snapshot_id).model_copy(deep=True)
        duplicate = package.knowledge.claim_bundles[0].model_copy(deep=True)
        duplicate.claim.claim_id = "second-owner"
        for evidence in duplicate.evidence:
            evidence.claim_id = "second-owner"
        package.knowledge.claim_bundles.append(duplicate)
        return package

    monkeypatch.setattr(snapshots, "get", shared_snapshot)
    request = FeedbackRequest(idempotency_key="owner", category="incomplete", reporter="test",
                              note="Check support", finding_index=1,
                              evidence_ids=[bundle.evidence[0].evidence_id])
    result = service.feedback.create(run.id, request)
    expected = bundle.claim.claim_id if workflow == "research_v10" else "second-owner"
    assert result["anchor"]["evidence"][0]["claim_id"] == expected
    assert service.feedback.create(run.id, request) == result
