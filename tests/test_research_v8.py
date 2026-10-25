"""research_v8: original-task anchoring without relaxing verbatim validation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent import structured_research as sr
from app.agent.models import AgentRunCreateRequest
from app.domain_plugins.research.plugin import ResearchDomainPlugin
from tests.test_answer_coverage import anchored_report
from tests.test_research_workflow import _analysis, _plan, _research_stack

# Digests of (system + final-report prompt) for a fixed state, frozen before each successor.
# Older versions must stay byte-identical so recorded generation attempts replay.
FROZEN_PROMPTS = {
    "StructuredResearchWorkflow": (
        "400380522223500a968741cbf8907f480dbad14f66d9d70b55608212568ce7e7"
    ),
    "EvidenceAlignedResearchWorkflow": (
        "bbc0c7525a6617432c4ac238814e9b60153fa9c891847a0dade405256728ec25"
    ),
    "FocusedResearchWorkflow": "950c9674502795725bd74e0ae56e14ea3afe44d6bc2620dadb1e0b75160422c5",
    "CoverageResearchWorkflow": "3a5f041b0a39c752d30cd26070ad0742402b1a70766f566d66b55ae0928c8196",
    "CoreCoverageResearchWorkflow": (
        "5749effa18128244bdc201bffa60de3cd78e8160e1eb43a93fc7b43f1d2639a7"
    ),
    "DirectResearchWorkflow": "56ed47f7c77b4a53c1dad072551aa140400b3e700ca7f29482477b8a76333dd2",
    "AnchoredDirectResearchWorkflow": (
        "36fa21646be1b8e35f57c3fad144f03d743d2a7f71d9b5cf46f7a7e1d8b5e46d"
    ),
}
STATE = {
    "task_analysis": {"research_question": "q?", "intended_output": "o", "constraints": []},
    "research_plan": {"steps": ["s"], "tool_sequence": ["context.research_input"]},
}
TASK = SimpleNamespace(title="仅依据 X 论文。", goal="当前只允许使用 X 论文。请依据 Y 说明结论。")


def prompt_for(cls):
    workflow = cls.__new__(cls)  # prompt assembly needs no service or model client
    workflow._research_input = lambda package: {"task": {"title": "T", "goal": "G"}}
    return workflow._report_prompt(STATE, SimpleNamespace(task=TASK))


@pytest.mark.parametrize("name", sorted(FROZEN_PROMPTS))
def test_older_report_prompts_stay_byte_identical(name):
    prompt = prompt_for(getattr(sr, name))
    digest = hashlib.sha256((sr.REPORT_SYSTEM_PROMPT + "\n" + prompt).encode()).hexdigest()
    assert digest == FROZEN_PROMPTS[name]
    assert ("ORIGINAL TASK" in prompt) == (name == "AnchoredDirectResearchWorkflow")


def test_v8_is_registered_with_its_own_version_and_namespace():
    plugin = ResearchDomainPlugin()
    spec = plugin.workflow_spec("research_v8")
    assert (spec.version, spec.checkpoint_namespace) == (
        "structured-v7", "agent_research_structured_v7"
    )
    others = [w for w in plugin.manifest.workflows if w.key != "research_v8"]
    assert spec.checkpoint_namespace not in {w.checkpoint_namespace for w in others}
    assert spec.version not in {w.version for w in others}
    assert plugin.workflow_spec("research_v7").version == "structured-v6"


def test_v8_prompt_carries_the_original_task_verbatim():
    prompt = prompt_for(sr.AnchoredDirectResearchWorkflow)
    block = prompt.split("ORIGINAL TASK (verbatim", 1)[1].split("Task analysis:", 1)[0]
    assert f"Title: {json.dumps(TASK.title, ensure_ascii=False)}\n" in block
    assert f"Goal: {json.dumps(TASK.goal, ensure_ascii=False)}\n" in block
    assert "超出允许资料范围" in prompt and "research_question" in prompt
    # Same output schema as v7: only the prompt changed, not the contract.
    assert sr.AnchoredDirectResearchWorkflow.report_model is sr.DirectReport


def _v8_run(tmp_path, question_part):
    paraphrase = {**_analysis(), "research_question": "How is the evidence-grounded workflow?"}
    _, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [paraphrase, _plan()]
    )
    finding = anchored_report(bundle, task.goal)["findings"][0]
    value = {
        "title": "Anchored report",
        "answer_parts": [{"question_part": question_part(task, paraphrase),
                          "findings": [finding], "gap": ""}],
    }
    llm.responses.extend([json.dumps(value)] * 2)
    run = service.create_run(
        project.id, task.id,
        AgentRunCreateRequest(workflow="research_v8", token_budget=32000,
                              reading_format="inline-v1"),
    )
    assert worker.run_once()
    return service, service.get_run(run.id), task, llm


def test_v8_run_succeeds_when_question_part_is_copied_from_original_task(tmp_path):
    service, done, task, llm = _v8_run(tmp_path, lambda task, _: task.goal)
    assert done.status == "completed", done.error_message
    assert done.workflow_version == "structured-v7"
    assert done.repair_count == 0
    assert f"Goal: {json.dumps(task.goal)}" in llm.calls[-1]
    output = service.repository.get_output(done.id)
    assert output.structured["draft"]["answer_status"] == "complete"
    assert f"## {task.goal}" in output.rendered_text


def test_v8_still_rejects_question_part_copied_from_paraphrase(tmp_path):
    _, done, _, _ = _v8_run(tmp_path, lambda _, analysis: analysis["research_question"])
    assert done.status == "needs_review"
    assert done.repair_count == 1


@pytest.mark.parametrize("workflow,verbatim", [
    ("research_v8", True), ("research_v7", True), ("research_v8", False),
])
def test_long_support_quote_run(tmp_path, monkeypatch, workflow, verbatim):
    _, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    quote = ("A governed snapshot preserves evidence. " * 50)[:1600]
    load_snapshot = service.load_context_snapshot

    def load_with_long_evidence(run_id):
        package = load_snapshot(run_id).model_copy(deep=True)
        for claim_bundle in package.knowledge.claim_bundles:
            for evidence in claim_bundle.evidence:
                if evidence.evidence_id == bundle.evidence[0].evidence_id:
                    evidence.quote = quote
        return package

    monkeypatch.setattr(service, "load_context_snapshot", load_with_long_evidence)
    finding = anchored_report(bundle, task.goal)["findings"][0]
    finding["support"][0]["quote"] = quote if verbatim else "x" * 1600
    value = {"title": "Long quote report", "answer_parts": [
        {"question_part": task.goal, "findings": [finding], "gap": ""},
    ]}
    llm.responses.extend([json.dumps(value)] * 2)
    run = service.create_run(
        project.id, task.id,
        AgentRunCreateRequest(workflow=workflow, token_budget=32000, reading_format="inline-v1"),
    )
    assert worker.run_once()
    done = service.get_run(run.id)
    if workflow == "research_v8" and verbatim:
        assert done.status == "completed", done.error_message
        assert done.repair_count == 0
        assert service.repository.get_output(run.id).structured["draft"]["answer_status"] == (
            "complete"
        )
    else:
        assert done.status == "needs_review"
        assert done.repair_count == 1
        diagnostic = "Invalid schema" if workflow == "research_v7" else "exact contiguous quote"
        assert diagnostic in llm.calls[-1]


def _direct_value(quote):
    return {"title": "Report", "answer_parts": [{
        "question_part": "Requested fact", "gap": "Missing detail",
        "findings": [{"claim_bundle_id": "bundle", "assertion": "Supported assertion",
                      "evidence_ids": ["evidence"], "scope": "Source scope",
                      "support": [{"evidence_id": "evidence", "quote": quote}]}],
    }]}


@pytest.mark.parametrize("quote,expected", [
    ("x" * 1500, "x" * 1500),
    ("x" * 1600, "x" * 1000),
    ("x" * 950 + "。" + "y" * 649, "x" * 950 + "。"),
    ("x" * 950 + "\n" + "y" * 649, "x" * 950 + "\n"),
    ("x " + "y" * 1598, ("x " + "y" * 1598)[:1000]),
])
def test_v8_only_shortens_oversized_support_quotes_to_contiguous_prefixes(quote, expected):
    value = _direct_value(quote)
    expected_value = deepcopy(value)
    expected_value["answer_parts"][0]["findings"][0]["support"][0]["quote"] = expected
    workflow = sr.AnchoredDirectResearchWorkflow.__new__(sr.AnchoredDirectResearchWorkflow)
    assert workflow.parse_report(value).model_dump() == expected_value
    assert quote.startswith(expected)
    assert 20 <= len(expected) <= 1500


@pytest.mark.parametrize("field", ["assertion", "gap"])
def test_v8_does_not_truncate_other_fields(field):
    value = _direct_value("x" * 1600)
    part = value["answer_parts"][0]
    target = part if field == "gap" else part["findings"][0]
    target[field] = "x" * 1600
    workflow = sr.AnchoredDirectResearchWorkflow.__new__(sr.AnchoredDirectResearchWorkflow)
    with pytest.raises(ValidationError) as exc:
        workflow.parse_report(value)
    assert any(error["loc"][-1] == field for error in exc.value.errors())
    assert target[field] == "x" * 1600


@pytest.mark.parametrize("workflow_class", [sr.AnchoredDirectResearchWorkflow,
                                            sr.DirectResearchWorkflow])
@pytest.mark.parametrize("mode", ["all_outside", "mixed_gaps", "partly_answered"])
def test_out_of_scope_heading_is_only_used_for_all_outside_v8(workflow_class, mode):
    quote = "A governed snapshot preserves evidence."
    value = _direct_value(quote)
    part = value["answer_parts"][0]
    if mode != "partly_answered":
        part["findings"] = []
    part["gap"] = "超出允许资料范围：仅允许 X 论文。"
    value["answer_parts"].append({
        "question_part": "Another fact", "findings": [],
        "gap": "缺少相关证据" if mode == "mixed_gaps" else "超出允许资料范围：仅允许 X 论文。",
    })
    package = SimpleNamespace(
        task=SimpleNamespace(title="Requested fact", goal="Another fact"),
        knowledge=SimpleNamespace(claim_bundles=[SimpleNamespace(
            claim=SimpleNamespace(claim_id="bundle"),
            evidence=[SimpleNamespace(evidence_id="evidence", quote=quote)],
        )]),
    )
    report = sr.DirectReport.model_validate(value)
    workflow = workflow_class.__new__(workflow_class)
    rendered = workflow.prepare_report(report, package)
    expected = "部分回答" if mode == "partly_answered" else "证据不足"
    if workflow_class is sr.AnchoredDirectResearchWorkflow and mode == "all_outside":
        expected = "超出允许资料范围"
    assert rendered.markdown.split("\n\n")[1] == f"回答状态：{expected}"
    assert rendered.answer_status == (
        "partial" if mode == "partly_answered" else "insufficient_evidence"
    )
