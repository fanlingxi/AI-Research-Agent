"""Numeric omission after validation, durable recovery, and legacy output invariance."""

import hashlib
import json
import socket
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import structured_research as sr
from app.agent.feedback_models import FeedbackRequest
from app.agent.gap_evidence import GapEvidenceRequest, RunGapEvidenceService
from app.agent.models import AgentRunCreateRequest
from app.domain_plugins.contracts import PluginPin
from app.domain_plugins.research.plugin import ResearchDomainPlugin
from app.llms.provider import LangChainChatClient
from app.research_profiles import STANDARD
from app.workspace.service import WorkspaceProjectionService
from tests.test_research_v8 import FROZEN_PROMPTS, prompt_for
from tests.test_research_v9 import _report
from tests.test_research_v10 import prepare, shared_fixture
from tests.test_research_workflow import _analysis, _plan, _research_stack


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real model/network calls are forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(LangChainChatClient, "invoke", forbidden)


def test_registration_and_frozen_generation():
    plugin = ResearchDomainPlugin()
    spec = plugin.workflow_spec("research_v11")
    assert (spec.version, spec.checkpoint_namespace) == (
        "structured-v10", "agent_research_structured_v10",
    )
    assert (spec.min_steps, spec.min_tool_calls, spec.requires_live_llm) == (9, 1, True)
    assert STANDARD.workflow == "research_v8"
    other = [s for s in plugin.manifest.workflows if s.key != "research_v11"]
    assert spec.version not in {s.version for s in other}
    assert spec.checkpoint_namespace not in {s.checkpoint_namespace for s in other}
    assert (sr.NumericSupportResearchWorkflow.report_model
            is sr.SharedEvidenceResearchWorkflow.report_model)
    for cls in (sr.SharedEvidenceResearchWorkflow, sr.NumericSupportResearchWorkflow):
        prompt = prompt_for(cls)
        assert hashlib.sha256((sr.REPORT_SYSTEM_PROMPT + "\n" + prompt).encode()).hexdigest() == (
            FROZEN_PROMPTS["SharedEvidenceResearchWorkflow"]
        )
    for method in ("_task_analysis", "_constrained_plan", "parse_report", "compile",
                   "_research_draft_validate_and_finalize"):
        assert getattr(sr.NumericSupportResearchWorkflow, method) is getattr(
            sr.SharedEvidenceResearchWorkflow, method)


@pytest.mark.parametrize("invalid", ["owner", "anchor", "part", "duplicate", "schema"])
def test_validation_precedes_numeric_omission(invalid):
    package, report = shared_fixture()
    part = report["answer_parts"][0]
    finding = part["findings"][0]
    finding["assertion"] = "Unsupported score 999"
    if invalid == "owner":
        finding["claim_bundle_id"] = "absent"
    elif invalid == "anchor":
        finding["support"][0]["quote"] = "Invented quote that never appeared."
    elif invalid == "part":
        part["question_part"] = "Absent question span"
    elif invalid == "duplicate":
        part["findings"].append(deepcopy(finding))
    else:
        finding["scope"] = None
    for cls in (sr.SharedEvidenceResearchWorkflow, sr.NumericSupportResearchWorkflow):
        with pytest.raises(ValueError):
            prepare(cls, report, package)


def test_part_mapping_separate_full_gaps_and_deduplicated_limitations():
    package, report = shared_fixture()
    package.task.goal = "Other question"
    part = report["answer_parts"][0]
    kept = deepcopy(part["findings"][0])
    part["findings"][0]["assertion"] = "Unsupported 999"
    part["findings"].append(kept)
    part["gap"] = "缺" * 600
    other = deepcopy(part)
    other.update(question_part="Other question", findings=[deepcopy(part["findings"][0])])
    other["findings"][0]["assertion"] = "Another unsupported 888"
    report["answer_parts"].append(other)
    original = deepcopy(report)
    draft = prepare(sr.NumericSupportResearchWorkflow, report, package)
    assert report == original
    assert draft.answer_status == "partial"
    assert len(draft.findings) == 1
    assert draft.limitations == ["缺" * 600,
        "所引证据片段未包含结论中的具体数值，无法核验，已省略 1 条相关结论。"]
    assert draft.markdown.count("缺" * 600) == 2
    assert draft.answer_parts[0]["gap"] == "缺" * 600
    assert draft.answer_parts[0]["finding_indices"] == [1]
    assert draft.answer_parts[1]["finding_indices"] == []
    assert draft.numeric_audit["index_mapping"] == [
        {"original_index": 1, "filtered_index": None},
        {"original_index": 2, "filtered_index": 1},
        {"original_index": 3, "filtered_index": None},
    ]
    assert [f["part_index"] for f in draft.numeric_audit["omitted"]] == [1, 2]


def test_numbers_from_other_findings_or_question_are_not_support():
    package, report = shared_fixture()
    package.task.goal += " 999"
    first = report["answer_parts"][0]["findings"][0]
    first["assertion"] = "Unsupported value 999"
    second = deepcopy(first)
    second.update(assertion="Supported value 999", evidence_ids=["other"],
                  support=[{"evidence_id": "other", "quote": "The measured quantity is 999."}])
    package.knowledge.claim_bundles[0].evidence.append(
        SimpleNamespace(evidence_id="other", quote="The measured quantity is 999."))
    report["answer_parts"][0]["findings"].append(second)
    draft = prepare(sr.NumericSupportResearchWorkflow, report, package)
    assert len(draft.findings) == len(draft.numeric_audit["omitted"]) == 1
    assert draft.findings[0].assertion == second["assertion"]


@pytest.mark.parametrize("all_omitted", [False, True])
def test_worker_persists_filtered_report_and_gap_evidence(tmp_path, monkeypatch, all_omitted):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()])
    report = _report(bundle, task)
    findings = report["answer_parts"][0]["findings"]
    bad = deepcopy(findings[0])
    bad["assertion"] = "Unsupported numerical score 999"
    if all_omitted:
        findings.clear()
    findings.insert(0, bad)
    llm.responses.append(json.dumps(report))
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow="research_v11", token_budget=32000))
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == "completed", done.error_message
    assert done.repair_count == 0
    assert len(llm.calls) == 3
    output = service.repository.get_output(run.id)
    draft = output.structured["draft"]
    assert draft["answer_status"] == ("insufficient_evidence" if all_omitted else "partial")
    assert len(draft["findings"]) == (0 if all_omitted else 1)
    assert "999" not in output.rendered_text
    assert "999" in draft["numeric_audit"]["omitted"][0]["assertion"]
    assert output.rendered_text == draft["markdown"]
    artifact = repo.memory_repository.get_artifact(output.structured["artifact_id"])
    assert artifact.reference == f"agent-run-output:{output.id}"
    content = WorkspaceProjectionService(repo, agent_run_service=service).get_artifact_content(
        artifact.id)
    assert content.rendered_markdown == output.rendered_text
    events = service.repository.list_events(run.id)
    summary = next(e.output_summary for e in events
                   if e.node_name == "citation_validation" and e.output_summary.get("passed"))
    assert summary["answer_status"] == draft["answer_status"]
    assert summary["numeric_omitted"] == 1
    assert "numeric_audit" not in summary
    _, _, gap = RunGapEvidenceService(service).parent(run.id, GapEvidenceRequest(
        output_sha256=output.output_sha256, gap_index=0))
    assert gap == draft["limitations"][0]
    if not all_omitted:
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
        feedback = service.feedback.create(run.id, FeedbackRequest(
            idempotency_key="filtered", category="incomplete", reporter="test", note="Check",
            finding_index=1, evidence_ids=[bundle.evidence[0].evidence_id]))
        assert feedback["anchor"]["assertion"] == draft["findings"][0]["assertion"]
        assert feedback["anchor"]["evidence"][0]["claim_id"] == bundle.claim.claim_id


@pytest.mark.parametrize("interruption", ["tool", "returned", "repair_returned", "pending"])
def test_worker_resume_never_repeats_calls(tmp_path, monkeypatch, interruption):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()])
    report = _report(bundle, task)
    report["answer_parts"][0]["findings"][0]["assertion"] = "Unsupported value 999"
    if interruption == "repair_returned":
        llm.responses.append("invalid json")
    llm.responses.append(json.dumps(report))
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
        workflow="research_v11", token_budget=32000))
    assert worker.run_once()
    assert service.get_run(run.id).status == "failed"
    before = len(llm.calls)
    tool_ids = [t.id for t in service.repository.list_tool_calls(run.id)]
    monkeypatch.setattr(service, method, original)
    service.resume_run(run.id)
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == ("needs_review" if interruption == "pending" else "completed")
    assert done.workflow_version == "structured-v10"
    assert len(llm.calls) == before + (interruption == "tool")
    assert tool_ids == [t.id for t in service.repository.list_tool_calls(run.id)]
    with worker.agent_runtime.checkpoint_factory.open(
        namespace="agent_research_structured_v10"
    ) as saver:
        saved = saver.get_tuple({"configurable": {"thread_id": run.id}})
    assert saved is not None
    if done.status == "completed":
        assert service.repository.get_output(run.id).structured["draft"]["answer_status"] == (
            "insufficient_evidence")
    with repo.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == (
            done.status == "completed")


LEGACY_CLASSES = list(FROZEN_PROMPTS)


def legacy_report(cls, status):
    package, direct = shared_fixture()
    finding = direct["answer_parts"][0]["findings"][0]
    finding.update(claim_bundle_id="second", assertion="Unverified numerical value 999")
    gap = "Requested evidence missing" if status != "complete" else ""
    if status == "insufficient_evidence":
        direct["answer_parts"][0]["findings"] = []
    direct["answer_parts"][0]["gap"] = gap
    if cls.report_model.__name__ == "DirectReport":
        return package, direct
    findings = direct["answer_parts"][0]["findings"]
    value = {"title": direct["title"], "findings": findings, "answer_status": status,
             "limitations": [gap] if gap else []}
    if cls.report_model.__name__ == "CoveredReport":
        value["answer_parts"] = [{"question_part": "Requested fact", "gap": gap,
                                  "finding_indices": list(range(len(findings)))}]
    else:
        value["findings"] = [{k: v for k, v in f.items() if k not in {"support", "scope"}}
                             for f in findings]
    return package, value


@pytest.mark.parametrize("name", LEGACY_CLASSES)
@pytest.mark.parametrize("status", ["complete", "partial", "insufficient_evidence"])
def test_legacy_draft_bytes_and_status_are_frozen(name, status):
    # Digests generated from git HEAD (6fa6608), before any v11 changes.
    path = Path(__file__).parent / "fixtures/research-v2-v10-drafts.json"
    frozen = json.loads(path.read_text())
    cls = getattr(sr, name)
    package, value = legacy_report(cls, status)
    workflow = cls.__new__(cls)
    report = workflow.parse_report(value)
    draft = workflow.prepare_report(report, package)
    assert draft.answer_status == report.answer_status == status
    assert hashlib.sha256(draft.model_dump_json().encode()).hexdigest() == frozen[name][status]
    assert "numeric_audit" not in draft.model_dump()


@pytest.mark.parametrize("workflow_key", [f"research_v{i}" for i in range(2, 11)])
@pytest.mark.parametrize("status", ["complete", "partial", "insufficient_evidence"])
def test_legacy_loop_uses_unchanged_status_and_event_fields(tmp_path, workflow_key, status):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [] if workflow_key == "research_v9" else [_analysis(), _plan()])
    plugin = ResearchDomainPlugin()
    cls = type(plugin.build_workflow(SimpleNamespace(llm=llm), PluginPin(
        key=plugin.manifest.key, version=plugin.manifest.version,
        contract_version=plugin.manifest.contract_version, workflow_key=workflow_key)))
    _, value = legacy_report(cls, status)
    findings = (value["answer_parts"][0]["findings"]
                if cls.report_model is sr.DirectReport else value["findings"])
    for finding in findings:
        finding.update(claim_bundle_id=bundle.claim.claim_id,
                       evidence_ids=[bundle.evidence[0].evidence_id])
        if "support" in finding:
            finding["support"] = [{"evidence_id": bundle.evidence[0].evidence_id,
                                   "quote": bundle.evidence[0].quote}]
    if "answer_parts" in value:
        value["answer_parts"][0]["question_part"] = task.goal
    llm.responses.append(json.dumps(value))
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow=workflow_key, token_budget=32000))
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == "completed", done.error_message
    assert done.repair_count == 0
    output = service.repository.get_output(run.id)
    assert output.structured["draft"]["answer_status"] == status
    assert "numeric_audit" not in json.dumps(output.structured)
    if status != "insufficient_evidence":
        assert "999" in output.rendered_text
    summary = next(e.output_summary for e in service.repository.list_events(run.id)
                   if e.node_name == "citation_validation" and e.output_summary.get("passed"))
    assert summary["answer_status"] == status
    assert set(summary) == {"passed", "semantic_support", "answer_status",
                            "reading_format", "reading_estimated_tokens"}
