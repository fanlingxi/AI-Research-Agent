"""Fixed preparation preserves the anchored report contract and durable recovery."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.agent.errors import ResearchLLMRequiredError
from app.agent.models import AgentRunCreateRequest
from app.agent.research_workflow import TaskAnalysis
from app.agent.structured_research import AnchoredDirectResearchWorkflow, FixedPlanResearchWorkflow
from app.domain_plugins.research.plugin import ResearchDomainPlugin
from app.research_profiles import STANDARD
from tests.test_answer_coverage import anchored_report
from tests.test_research_v8 import STATE, TASK, prompt_for
from tests.test_research_workflow import _research_stack


def _report(bundle, task):
    return {
        "title": "Fixed plan report",
        "answer_parts": [{
            "question_part": task.goal,
            "findings": [anchored_report(bundle, task.goal)["findings"][0]],
            "gap": "",
        }],
    }


def _request(**kwargs):
    return AgentRunCreateRequest(workflow="research_v9", token_budget=32000, **kwargs)


def _checkpoint(worker, run):
    namespace = ResearchDomainPlugin().workflow_spec("research_v9").checkpoint_namespace
    with worker.agent_runtime.checkpoint_factory.open(namespace=namespace) as checkpointer:
        saved = checkpointer.get_tuple({"configurable": {"thread_id": run.id}})
    assert saved is not None
    return saved.checkpoint["channel_values"]


def test_v9_registration_is_separate_and_default_is_unchanged():
    plugin = ResearchDomainPlugin()
    spec = plugin.workflow_spec("research_v9")
    assert (spec.version, spec.checkpoint_namespace) == (
        "structured-v8", "agent_research_structured_v8"
    )
    assert (spec.min_steps, spec.min_tool_calls, spec.requires_live_llm) == (9, 1, True)
    others = [w for w in plugin.manifest.workflows if w.key != "research_v9"]
    assert spec.version not in {w.version for w in others}
    assert spec.checkpoint_namespace not in {w.checkpoint_namespace for w in others}
    assert STANDARD.workflow == "research_v8"
    plugin.validate_run_request("research_v9", max_steps=9, max_tool_calls=1, is_mock_llm=False)


@pytest.mark.parametrize("steps,tools,mock,error,match", [
    (8, 1, False, ValueError, "max_steps >= 9"),
    (9, 0, False, ValueError, "at least 1 tool"),
    (9, 1, True, ResearchLLMRequiredError, "live LLM"),
])
def test_v9_rejects_invalid_run_limits_and_mock(steps, tools, mock, error, match):
    with pytest.raises(error, match=match):
        ResearchDomainPlugin().validate_run_request(
            "research_v9", max_steps=steps, max_tool_calls=tools, is_mock_llm=mock
        )


def test_v9_prompt_only_removes_analysis_and_plan():
    old = prompt_for(AnchoredDirectResearchWorkflow)
    expected = "\n".join(
        line for line in old.split("\n")
        if not line.startswith(("Task analysis:", "Plan:"))
    )
    assert prompt_for(FixedPlanResearchWorkflow) == expected
    workflow = FixedPlanResearchWorkflow.__new__(FixedPlanResearchWorkflow)
    workflow._research_input = lambda package: {"task": {"title": "T", "goal": "G"}}
    changed = deepcopy(STATE)
    changed["task_analysis"]["research_question"] = "PARAPHRASED_QUESTION_MARKER"
    changed["research_plan"]["steps"] = ["PLAN_MARKER"]
    prompt = workflow._report_prompt(changed, SimpleNamespace(task=TASK))
    assert prompt == expected
    assert "PARAPHRASED_QUESTION_MARKER" not in prompt and "PLAN_MARKER" not in prompt


@pytest.mark.parametrize("mode", ["complete", "partial", "insufficient_evidence", "repair"])
@pytest.mark.parametrize("max_tools", [1, 3])
def test_v9_worker_uses_one_generation_and_audits_deterministic_nodes(tmp_path, mode, max_tools):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(tmp_path, [])
    value = _report(bundle, task)
    part = value["answer_parts"][0]
    if mode in {"partial", "insufficient_evidence"}:
        part["gap"] = "缺少所请求的细节证据。"
    if mode == "insufficient_evidence":
        part["findings"] = []
    if mode == "repair":
        invalid = deepcopy(value)
        invalid["answer_parts"][0]["question_part"] = "PARAPHRASED_QUESTION_MARKER"
        llm.responses.append(json.dumps(invalid))
    llm.responses.append(json.dumps(value))
    run = service.create_run(project.id, task.id, _request(max_steps=9, max_tool_calls=max_tools))
    assert service.load_context_snapshot(run.id).reading_format == "research-v1"
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == "completed", done.error_message
    assert done.repair_count == (mode == "repair")
    assert len(llm.calls) == 1 + (mode == "repair")
    output = service.repository.get_output(run.id)
    assert output.validation["passed"]
    assert output.validation["repair_applied"] == (mode == "repair")
    assert output.structured["draft"]["answer_status"] == (
        "complete" if mode == "repair" else mode
    )
    events = service.repository.list_events(run.id)
    completed = {e.node_name: e for e in events if e.event_type == "node_completed"}
    for name in ("task_analysis", "constrained_plan"):
        assert completed[name].output_summary["deterministic"] is True
        assert completed[name].token_usage == {"input_tokens": 0, "output_tokens": 0}
        assert any(e.node_name == name and e.event_type != "node_completed" for e in events)
    state = _checkpoint(worker, run)
    analysis = TaskAnalysis.model_validate(state["task_analysis"])
    assert analysis.research_question == task.title
    assert state["llm_calls_used"] == len(llm.calls)
    assert completed["constrained_plan"].output_summary["plan_steps"] == [
        {"position": i, "description": step}
        for i, step in enumerate(state["research_plan"]["steps"], 1)
    ]
    assert state["research_plan"]["tool_sequence"] == ["context.research_input"]
    assert state["tool_calls_used"] == done.tool_call_count == 1
    assert len(service.repository.list_tool_calls(run.id)) == 1
    assert sum(e.token_usage.get("input_tokens", 0) for e in events) == 11 * len(llm.calls)
    assert sum(e.token_usage.get("output_tokens", 0) for e in events) == 7 * len(llm.calls)
    with repo.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM research_generation_attempts WHERE run_id=?", (run.id,)
        ).fetchone()[0] == len(llm.calls)


@pytest.mark.parametrize("invalid_field", ["question_part", "quote", "evidence_id"])
def test_v9_rejects_invalid_questions_and_evidence_after_one_repair(tmp_path, invalid_field):
    _, service, worker, project, task, _, bundle, llm = _research_stack(tmp_path, [])
    value = _report(bundle, task)
    part = value["answer_parts"][0]
    if invalid_field == "question_part":
        part["question_part"] = "A paraphrased question absent from the task."
    elif invalid_field == "quote":
        part["findings"][0]["support"][0]["quote"] = "Unsupported invented source quote."
    else:
        part["findings"][0]["evidence_ids"] = ["invented-id"]
        part["findings"][0]["support"][0]["evidence_id"] = "invented-id"
    llm.responses.extend([json.dumps(value)] * 2)
    run = service.create_run(project.id, task.id, _request())
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == "needs_review"
    assert done.repair_count == 1 and len(llm.calls) == 2
    with pytest.raises(KeyError):
        service.repository.get_output(run.id)


@pytest.mark.parametrize("title", ["正常标题", "x", "", "题" * 2000, "题" * 2001])
def test_v9_analysis_preserves_long_original_task_without_truncation(tmp_path, title):
    _, service, _, project, task, _, _, llm = _research_stack(tmp_path, [])
    run = service.create_run(project.id, task.id, _request())
    package = service.load_context_snapshot(run.id).model_copy(deep=True)
    package.task.title = title
    package.task.goal = "任务原文不可截断。" * 500
    events = []
    port = SimpleNamespace(
        llm=llm, load_context_snapshot=lambda _: package, mark_node=lambda *a, **kw: None,
        record_node_completed=lambda *a, **kw: events.append(kw),
    )
    workflow = FixedPlanResearchWorkflow(port)
    updates = workflow._task_analysis({"run_id": run.id, "llm_calls_used": 0})
    analysis = TaskAnalysis.model_validate(updates["task_analysis"])
    assert analysis.research_question == (
        title if 2 <= len(title) <= 2000 else "原始任务（详见快照）"
    )
    constraints = {key: json.loads(value) for key, value in (
        constraint.split(": ", 1) for constraint in analysis.constraints
    )}
    assert constraints == package.constraints.model_dump(mode="json")
    assert "llm_calls_used" not in updates
    assert not llm.calls
    prompt = workflow._report_prompt(updates, package)
    assert f"Title: {json.dumps(title, ensure_ascii=False)}\n" in prompt
    assert f"Goal: {json.dumps(package.task.goal, ensure_ascii=False)}\n" in prompt


@pytest.mark.parametrize("interruption", ["tool", "returned", "repair_returned", "pending"])
def test_v9_checkpoint_recovery_does_not_duplicate_calls(tmp_path, monkeypatch, interruption):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(tmp_path, [])
    value = _report(bundle, task)
    if interruption == "repair_returned":
        llm.responses.append("invalid json")
    llm.responses.append(json.dumps(value))
    method_name = {
        "tool": "record_tool_call", "returned": "finish_generation_attempt",
        "repair_returned": "finish_generation_attempt", "pending": "begin_generation_attempt",
    }[interruption]
    original = getattr(service, method_name)

    def interrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        if interruption != "repair_returned" or args[1] == 1:
            raise RuntimeError("Interrupted after durable write")
        return result

    monkeypatch.setattr(service, method_name, interrupt)
    run = service.create_run(project.id, task.id, _request(max_steps=16, max_tool_calls=1))
    assert worker.run_once()
    assert service.get_run(run.id).status == "failed"
    calls_before = len(llm.calls)
    tools_before = service.repository.list_tool_calls(run.id)
    assert len(tools_before) == 1
    state = _checkpoint(worker, run)
    assert state["llm_calls_used"] == 0
    monkeypatch.setattr(service, method_name, original)
    assert service.resume_run(run.id).status == "queued"
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == ("needs_review" if interruption == "pending" else "completed")
    assert len(llm.calls) == calls_before + (interruption == "tool")
    tools_after = service.repository.list_tool_calls(run.id)
    assert [t.id for t in tools_after] == [t.id for t in tools_before]
    assert done.tool_call_count == 1
    events = service.repository.list_events(run.id)
    for name in ("task_analysis", "constrained_plan"):
        assert sum(e.node_name == name and e.event_type == "node_completed" for e in events) == 1
    attempts = [e for e in events if e.output_summary.get("attempt_reference")]
    assert len(attempts) == len(llm.calls)
    with repo.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == (
            interruption != "pending"
        )
    if done.status == "completed":
        assert _checkpoint(worker, run)["llm_calls_used"] == len(llm.calls)
