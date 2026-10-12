"""research_v8: original-task anchoring without relaxing verbatim validation."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from app.agent import structured_research as sr
from app.agent.models import AgentRunCreateRequest
from app.domain_plugins.research.plugin import ResearchDomainPlugin
from tests.test_answer_coverage import anchored_report
from tests.test_research_workflow import _analysis, _plan, _research_stack

# Digests of (system + final-report prompt) for a fixed state, computed before research_v8
# existed. Older versions must stay byte-identical so recorded generation attempts replay.
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
    assert "ORIGINAL TASK" not in prompt


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
