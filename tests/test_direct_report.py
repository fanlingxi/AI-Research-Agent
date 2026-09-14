import json

import pytest
from pydantic import ValidationError

from app.agent.models import AgentRunCreateRequest
from app.agent.structured_research import DirectReport
from app.benchmarking.store_validation import run as store_run
from tests.test_answer_coverage import anchored_report
from tests.test_research_workflow import _analysis, _plan, _research_stack


@pytest.mark.parametrize("mode", ["complete", "partial", "insufficient_evidence", "repair"])
def test_direct_worker_derives_status_and_renders_each_part_once(tmp_path, mode):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    finding = anchored_report(bundle, task.goal)["findings"][0]
    value = {
        "title": "Direct report",
        "answer_parts": [
            {
                "question_part": task.goal,
                "findings": [] if mode == "insufficient_evidence" else [finding],
                "gap": "Missing requested evidence"
                if mode in {"partial", "insufficient_evidence"}
                else "",
            }
        ],
    }
    if mode == "repair":
        invalid = json.loads(json.dumps(value))
        invalid["answer_parts"][0]["findings"][0]["support"][0]["quote"] = (
            "UNTRUSTED_MARKER_1234567890"
        )
        llm.responses.append(json.dumps(invalid))
    llm.responses.append(json.dumps(value))
    run = service.create_run(
        project.id,
        task.id,
        AgentRunCreateRequest(
            workflow="research_v7", token_budget=32000, reading_format="inline-v1"
        ),
    )
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == "completed", done.error_message
    assert done.workflow_version == "structured-v6"
    assert done.repair_count == (mode == "repair")
    output = service.repository.get_output(run.id)
    expected = "complete" if mode == "repair" else mode
    assert output.structured["draft"]["answer_status"] == expected
    assert output.rendered_text.count("Missing requested evidence") == (expected != "complete")
    assert "UNTRUSTED_MARKER" not in llm.calls[-1]
    assert len(output.structured["draft"]["findings"]) == (mode != "insufficient_evidence")


def test_direct_schema_has_one_source_of_state_and_requires_answer_or_gap():
    assert set(DirectReport.model_json_schema()["properties"]) == {"title", "answer_parts"}
    value = {
        "title": "Missing evidence",
        "answer_parts": [{"question_part": "requested fact", "findings": [], "gap": " "}],
    }
    with pytest.raises(ValidationError, match="findings or a specific gap"):
        DirectReport.model_validate(value)
    value["answer_parts"][0]["gap"] = "Evidence unavailable"
    with pytest.raises(ValidationError, match="Extra inputs"):
        DirectReport.model_validate({**value, "answer_status": "complete"})


def test_real_store_entry_is_explicit_and_offline_guarded():
    assert store_run()["mode"] == "dry_run"
    with pytest.raises(ValueError, match="offline"):
        store_run(execute=True)


def test_new_paper_evaluation_cannot_escape_offline_test_isolation():
    from app.benchmarking.resume_closeout import execute, prepare

    with pytest.raises(ValueError, match="offline"):
        prepare()
    with pytest.raises(ValueError, match="offline"):
        execute("not-a-real-path")
