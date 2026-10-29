"""Explicit-gap contracts, durable repair, frozen versions and audit deduplication."""

import hashlib
import json
import socket
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.agent import structured_research as sr
from app.agent.explicit_gap_research import (
    ANCHOR_DIAGNOSTIC,
    GAP_PROMPT_SUFFIX,
    MISSING_GAP_DIAGNOSTIC,
    ExplicitGapAnswerPart,
    ExplicitGapReport,
    ExplicitGapResearchWorkflow,
)
from app.agent.feedback_models import FeedbackRequest
from app.agent.models import AgentRunCreateRequest
from app.agent.research_workflow import _canonical_json
from app.domain_plugins.registry import create_builtin_plugin_registry
from app.domain_plugins.research.plugin import ResearchDomainPlugin
from app.llms.provider import LangChainChatClient
from app.research_profiles import STANDARD
from scripts.audit_report_gaps import audit
from tests.test_research_v8 import FROZEN_PROMPTS, prompt_for
from tests.test_research_v9 import _report
from tests.test_research_v10 import prepare, shared_fixture
from tests.test_research_workflow import _analysis, _plan, _research_stack

WORKFLOWS = {
    "research_v8": sr.AnchoredDirectResearchWorkflow,
    "research_v9": sr.FixedPlanResearchWorkflow,
    "research_v10": sr.SharedEvidenceResearchWorkflow,
    "research_v11": sr.NumericSupportResearchWorkflow,
}
OLD_DIAGNOSTIC = "Invalid schema: respect required fields, IDs, types and the 12-finding limit"
OLD_SCHEMA_SHA256 = "ae60d25f78a8a04950cb400f7dd494d0448adb739837b075bad6be51de65e9b5"


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real model/network calls are forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(LangChainChatClient, "invoke", forbidden)


def archive_shape():
    # Two nonempty parts omitting gap, as in the archived pretraining-q2 shape.
    _, value = shared_fixture()
    del value["answer_parts"][0]["gap"]
    value["answer_parts"].append(deepcopy(value["answer_parts"][0]))
    return value


@pytest.mark.parametrize("workflow_class", WORKFLOWS.values())
def test_frozen_prompts_schema_and_missing_gap_diagnostics(workflow_class):
    workflow = workflow_class.__new__(workflow_class)
    expected = FROZEN_PROMPTS.get(workflow_class.__name__,
                                  FROZEN_PROMPTS["SharedEvidenceResearchWorkflow"])
    assert hashlib.sha256((sr.REPORT_SYSTEM_PROMPT + "\n" +
                           prompt_for(workflow_class)).encode()).hexdigest() == expected
    assert hashlib.sha256(_canonical_json(workflow.report_model.model_json_schema())
                          .encode()).hexdigest() == OLD_SCHEMA_SHA256
    value = archive_shape()
    original = deepcopy(value)
    with pytest.raises(ValidationError) as caught:
        workflow.parse_report(value)
    assert {(e["loc"], e["type"]) for e in caught.value.errors()} == {
        (("answer_parts", 0, "gap"), "missing"),
        (("answer_parts", 1, "gap"), "missing"),
    }
    assert workflow._report_error_diagnostic(caught.value) == OLD_DIAGNOSTIC
    assert value == original
    value["answer_parts"][0]["gap"] = ""
    value["answer_parts"][1]["gap"] = "A requested detail is unsupported."
    assert workflow.parse_report(value).answer_status == "partial"


def test_v12_schema_and_all_three_prompt_examples():
    assert ExplicitGapResearchWorkflow.report_model is not sr.DirectReport
    schema = ExplicitGapAnswerPart.model_json_schema()
    assert set(schema["required"]) == {"question_part", "findings", "gap"}
    assert "default" not in schema["properties"]["gap"]
    assert schema["properties"]["gap"]["description"]
    assert schema["additionalProperties"] is False
    assert ExplicitGapAnswerPart.model_config["strict"] is True
    prompt = prompt_for(ExplicitGapResearchWorkflow)
    assert prompt.endswith(GAP_PROMPT_SUFFIX)
    assert "EVERY item" in GAP_PROMPT_SUFFIX and "Final key check" in GAP_PROMPT_SUFFIX
    examples = []
    for marker in ("Fully answered:\n", "nonempty findings:\n", "Cannot answer:\n"):
        value, _ = json.JSONDecoder().raw_decode(GAP_PROMPT_SUFFIX.split(marker, 1)[1])
        examples.append(ExplicitGapReport.model_validate(value))
    assert [r.answer_status for r in examples] == ["complete", "partial", "insufficient_evidence"]


@pytest.mark.parametrize("findings", [True, False])
def test_v12_missing_gap_is_rejected_without_mutation_or_content_in_diagnostic(findings):
    workflow = ExplicitGapResearchWorkflow.__new__(ExplicitGapResearchWorkflow)
    value = archive_shape()
    if not findings:
        value["answer_parts"][0]["findings"] = []
    value["answer_parts"][0]["question_part"] = "UNTRUSTED_MODEL_INSTRUCTIONS"
    original = deepcopy(value)
    with pytest.raises(ValidationError) as caught:
        workflow.parse_report(value)
    assert workflow._report_error_diagnostic(caught.value) == MISSING_GAP_DIAGNOSTIC
    assert value == original


@pytest.mark.parametrize("invalid", ["type", "empty", "other_missing"])
def test_v12_other_errors_keep_frozen_diagnostics(invalid):
    workflow = ExplicitGapResearchWorkflow.__new__(ExplicitGapResearchWorkflow)
    _, value = shared_fixture()
    part = value["answer_parts"][0]
    if invalid == "type":
        variants = [None, 0, False, [], {}]
    elif invalid == "empty":
        part["findings"] = []
        variants = ["", "   "]
    else:
        del part["findings"]
        variants = [""]
    for gap in variants:
        part["gap"] = gap
        with pytest.raises(ValidationError) as caught:
            workflow.parse_report(value)
        assert workflow._report_error_diagnostic(caught.value) == OLD_DIAGNOSTIC
    assert workflow._report_error_diagnostic(ValueError("UNTRUSTED")) == (
        sr.report_error_diagnostic(ValueError("UNTRUSTED")))


def test_v12_registration_is_optional_and_namespaces_are_unique():
    registry = create_builtin_plugin_registry()
    plugin = ResearchDomainPlugin()
    spec = plugin.workflow_spec("research_v12")
    assert (spec.version, spec.checkpoint_namespace) == (
        "structured-v11", "agent_research_structured_v11")
    assert (spec.min_steps, spec.min_tool_calls, spec.requires_live_llm) == (9, 1, True)
    namespaces = [w.checkpoint_namespace for m in registry.list_manifests() for w in m.workflows]
    assert len(namespaces) == len(set(namespaces))
    assert spec.version not in {w.version for w in plugin.manifest.workflows
                                if w.key != "research_v12"}
    assert STANDARD.run_defaults()["workflow"] == "research_v11"
    assert plugin.manifest.default_workflow_key == "research"
    with pytest.raises(ValueError, match="max_steps"):
        plugin.validate_run_request("research_v12", max_steps=8, max_tool_calls=1,
                                    is_mock_llm=False)
    with pytest.raises(ValueError, match="live LLM"):
        plugin.validate_run_request("research_v12", max_steps=9, max_tool_calls=1,
                                    is_mock_llm=True)


@pytest.mark.parametrize("workflow,repair", [
    ("research_v12", "complete"), ("research_v12", "partial"),
    ("research_v12", "missing_again"), ("research_v11", "partial"),
])
def test_worker_repairs_once_with_versioned_diagnostic(tmp_path, monkeypatch, workflow, repair):
    _, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()])
    value = _report(bundle, task)
    del value["answer_parts"][0]["gap"]
    corrected = deepcopy(value)
    if repair != "missing_again":
        corrected["answer_parts"][0]["gap"] = (
            "Requested detail unavailable." if repair == "partial" else "")
    llm.responses.extend(json.dumps(v) for v in (value, corrected))
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow=workflow, token_budget=32000))
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.repair_count == 1
    assert len(llm.calls) == 4
    diagnostic = MISSING_GAP_DIAGNOSTIC if workflow == "research_v12" else OLD_DIAGNOSTIC
    expected = llm.calls[-2] + (
        f"\nPrevious attempt invalid: {diagnostic}. "
        "Produce a corrected complete object within the limits.")
    assert llm.calls[-1] == expected
    assert done.workflow_version == ("structured-v11" if workflow == "research_v12"
                                     else "structured-v10")
    if repair == "missing_again":
        assert done.status == "needs_review"
        assert done.error_message == MISSING_GAP_DIAGNOSTIC
        with pytest.raises(KeyError, match="has no output"):
            service.repository.get_output(run.id)
    else:
        assert done.status == "completed", done.error_message
        output = service.repository.get_output(run.id)
        assert output.structured["draft"]["answer_status"] == repair
        assert output.validation["repair_applied"] is True
        # Feedback must keep the actual bundle owner even with shared evidence IDs.
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
            idempotency_key="gap-repair", category="incomplete", reporter="test", note="Check",
            finding_index=1, evidence_ids=[bundle.evidence[0].evidence_id]))
        assert feedback["anchor"]["evidence"][0]["claim_id"] == bundle.claim.claim_id


def test_v12_resumes_returned_repair_without_repeating_calls(tmp_path, monkeypatch):
    _, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()])
    corrected = _report(bundle, task)
    initial = deepcopy(corrected)
    del initial["answer_parts"][0]["gap"]
    llm.responses.extend(json.dumps(v) for v in (initial, corrected))
    original = service.finish_generation_attempt

    def interrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == 1:
            raise RuntimeError("Interrupted after durable repair write")
        return result

    monkeypatch.setattr(service, "finish_generation_attempt", interrupt)
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow="research_v12", token_budget=32000))
    assert worker.run_once()
    assert service.get_run(run.id).status == "failed"
    assert len(llm.calls) == 4
    monkeypatch.setattr(service, "finish_generation_attempt", original)
    service.resume_run(run.id)
    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    assert len(llm.calls) == 4
    assert service.repository.get_output(run.id).validation["repair_applied"] is True
    with worker.agent_runtime.checkpoint_factory.open(
        namespace="agent_research_structured_v11"
    ) as saver:
        assert saver.get_tuple({"configurable": {"thread_id": run.id}}) is not None


def test_v12_keeps_numeric_filtering_and_evidence_validation():
    package, value = shared_fixture()
    assert prepare(ExplicitGapResearchWorkflow, value, package) == prepare(
        sr.NumericSupportResearchWorkflow, value, package)
    value["answer_parts"][0]["findings"][0]["assertion"] = "Unsupported score 999"
    draft = prepare(ExplicitGapResearchWorkflow, value, package)
    assert draft.answer_status == "insufficient_evidence"
    assert len(draft.numeric_audit["omitted"]) == 1
    assert draft.limitations
    value["answer_parts"][0]["findings"][0]["support"][0]["quote"] = "Forged supporting quote."
    with pytest.raises(sr.ReportContractError):
        prepare(ExplicitGapResearchWorkflow, value, package)


@pytest.mark.parametrize("target", ["question_part", "support"])
@pytest.mark.parametrize("original,copied", [
    ("Scientific Mg measurement", "Scientific mg measurement"),
    ("Scientific x² measurement", "Scientific x2 measurement"),
])
def test_v12_anchor_policy_rejects_semantic_changes_legacy_accepts(target, original, copied):
    package, value = shared_fixture()
    package.knowledge.claim_bundles = package.knowledge.claim_bundles[:1]
    part = value["answer_parts"][0]
    if target == "question_part":
        package.task.title = package.task.goal = original
        part["question_part"] = copied
    else:
        package.knowledge.claim_bundles[0].evidence[0].quote = original
        part["findings"][0]["support"][0]["quote"] = copied
    for cls in WORKFLOWS.values():
        assert prepare(cls, value, package).findings
    with pytest.raises(sr.ReportContractError) as caught:
        prepare(ExplicitGapResearchWorkflow, value, package)
    diagnostic = ExplicitGapResearchWorkflow._report_error_diagnostic(caught.value)
    assert diagnostic == sr.report_error_diagnostic(caught.value) + ". " + ANCHOR_DIAGNOSTIC
    assert copied not in diagnostic


def test_v12_both_anchor_checks_tolerate_layout_noise():
    package, value = shared_fixture()
    package.task.title = package.task.goal = "Requested Ｍｇ fact"
    value["answer_parts"][0]["question_part"] = "Requested Mg\nfact"
    source = "A “scientiﬁc” mea-\nsurement uses Ｍｇ."
    for bundle in package.knowledge.claim_bundles:
        bundle.evidence[0].quote = source
    value["answer_parts"][0]["findings"][0]["support"][0]["quote"] = (
        'A "scientific" measurement uses Mg.')
    assert prepare(ExplicitGapResearchWorkflow, value, package).answer_status == "complete"


@pytest.mark.parametrize("target", ["question_part", "support"])
def test_worker_v12_repairs_anchor_case_with_fixed_diagnostic(tmp_path, target):
    _, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()])
    corrected = _report(bundle, task)
    invalid = deepcopy(corrected)
    part = invalid["answer_parts"][0]
    if target == "question_part":
        part["question_part"] = part["question_part"].swapcase()
    else:
        anchor = part["findings"][0]["support"][0]
        anchor["quote"] = anchor["quote"].swapcase()
    llm.responses.extend(json.dumps(v) for v in (invalid, corrected))
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow="research_v12", token_budget=32000))
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == "completed", done.error_message
    assert done.repair_count == 1
    assert len(llm.calls) == 4
    assert ANCHOR_DIAGNOSTIC in llm.calls[-1]
    assert service.repository.get_output(run.id).validation["repair_applied"] is True


def test_structured_gap_audit_counts_physical_calls_and_ignores_replay_rows(tmp_path):
    arm = tmp_path / "batch/case/arm"
    schema = sr.DirectReport.model_json_schema()
    prompt = "Required JSON schema: " + json.dumps(schema)
    initial = archive_shape()
    corrected = deepcopy(initial)
    for part in corrected["answer_parts"]:
        part["gap"] = ""
    for index, value in enumerate([initial, corrected], start=1):
        folder = arm / f"generation/call-{index}"
        folder.mkdir(parents=True)
        (folder / "request.json").write_text(json.dumps({"prompt": prompt}), encoding="utf-8")
        (folder / "response.json").write_text(
            json.dumps({"text": "```json\n" + json.dumps(value) + "\n```"}), encoding="utf-8")
    (arm / "archive").mkdir()
    (arm / "archive/run.json").write_text(json.dumps({
        "plugin": {"workflow_key": "research_v8"}, "workflow_version": "structured-v7",
        "id": "run", "status": "completed", "repair_count": 1,
    }), encoding="utf-8")
    (tmp_path / "results.json").write_text(json.dumps([initial, initial]), encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    result = audit(tmp_path)
    assert result["totals"] == {
        "generation_calls": 2, "report_calls": 2, "answer_parts_report_calls": 2,
        "gap_required_calls": 2, "missing_gap_calls": 1, "missing_gap_fields": 2,
        "affected_runs": 1,
    }
    assert result["missing"][0]["gap_required"] is True
    assert result["missing"][0]["parts"] == [
        {"index": 0, "findings_count": 1}, {"index": 1, "findings_count": 1}]
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == before
