"""Deterministic archived-response replay with no model or network access."""

import json
from copy import deepcopy

import pytest

from app.agent import structured_research as sr
from app.agent.research_workflow import _canonical_json
from app.benchmarking import replay_v10 as replay
from tests.test_research_v10 import QUOTE, shared_fixture
from tests.test_research_workflow import _analysis, _plan

STATE = {"task_analysis": _analysis(), "research_plan": _plan()}


def seal(raw):
    raw["package_sha256"] = ""
    raw["package_sha256"] = replay.digest(_canonical_json(raw))


def archive_case(archive, name, mode, *, arm="A0", cached=False):
    case = archive / name
    package, value = shared_fixture()
    raw = json.loads(json.dumps(vars(package), default=vars))
    raw.update(snapshot_id=f"{name}-{arm}", package_schema_version="1.2",
               reading_format="inline-v1")
    seal(raw)
    replay.write(case / f"snapshot-{arm}.json", raw)
    workflow = sr.AnchoredDirectResearchWorkflow.__new__(sr.AnchoredDirectResearchWorkflow)
    workflow._research_input = lambda _: {
        "context_sha256": raw["package_sha256"], "context_snapshot_id": raw["snapshot_id"],
        "task": raw["task"], "knowledge": raw["knowledge"],
    }
    prompt = workflow._report_prompt(STATE, package)
    repair = deepcopy(value)
    repair["answer_parts"][0]["findings"][0].update(
        claim_bundle_id="second", assertion="Different repaired assertion",
    )
    if mode == "both_fail":
        for report in (value, repair):
            report["answer_parts"][0]["findings"][0]["support"][0]["quote"] = (
                "Invented quote absent from evidence."
            )
    pairs = [("analysis", STATE["task_analysis"]), ("plan", STATE["research_plan"]),
             (prompt, value)]
    if mode != "new_complete":
        pairs.append((prompt + "\nPrevious attempt invalid: invalid. "
                      "Produce a corrected complete object within the limits.", repair))
    records, paid = [], 0
    for index, (prompt, response) in enumerate(pairs):
        request = {"prompt": prompt,
                   "system_prompt": sr.REPORT_SYSTEM_PROMPT if index >= 2 else "prep"}
        record = {"kind": "live", "status": "returned",
                  "prompt_sha256": replay.request_digest(request)}
        if cached and index < 2:
            record = {"kind": "exact_prompt_replay", "prompt_sha256": record["prompt_sha256"]}
        else:
            paid += 1
            folder = case / arm / "generation" / f"call-{paid}"
            replay.write(folder / "request.json", request)
            replay.write(folder / "response.json", {"text": json.dumps(response)})
        records.append(record)
    replay.write(case / arm / "generation/calls.json", records)
    return {"id": name, "question": "Requested fact", "expectation": {
        "outcome": "answer", "required_conclusions": [{"id": "fact", "statement": QUOTE}],
        "forbidden_conclusions": [],
    }}


@pytest.fixture
def archived(tmp_path):
    root = tmp_path / "archive"
    tasks = [archive_case(root, name, mode) for name, mode in (
        ("new", "new_complete"), ("changed", "repair"), ("failed", "both_fail"),
    )]
    archive_case(root, "changed", "repair", arm="A1", cached=True)
    replay.write(root / "tasks.json", tasks)
    return root


def test_offline_replay_uses_first_valid_slot_and_writes_only_new_output(
    archived, tmp_path, monkeypatch,
):
    import socket

    from app.llms.provider import LangChainChatClient

    def forbidden(*args, **kwargs):
        raise AssertionError("Network/model access forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(LangChainChatClient, "invoke", forbidden)
    before = {p.relative_to(archived): p.read_bytes() for p in archived.rglob("*") if p.is_file()}
    out = tmp_path / "replay"
    summary = replay.replay(archived, out)
    assert summary["new_model_calls"] == 0
    assert summary["transitions"] == {
        "completed -> completed": 2, "needs_review -> completed": 1,
        "needs_review -> needs_review": 1,
    }
    assert summary["newly_completed"] == 1
    assert summary["completed_body_changed"] == 2
    assert len(summary["body_changed_runs"]) == summary["judge_packets"] == 3
    rows = {(r["case"], r["arm"]): r for r in replay.read(out / "results.json")}
    assert rows["new", "A0"]["v8"]["attempted_slots"] == [0]
    assert rows["failed", "A0"]["v10"]["status"] == "needs_review"
    for arm in ("A0", "A1"):
        row = rows["changed", arm]
        assert row["v8"]["used_slot"] == 1
        assert row["v8"]["attempted_slots"] == [0, 1]
        assert row["v10"]["used_slot"] == 0
        assert row["v10"]["attempted_slots"] == [0]
        assert "Different repaired assertion" not in row["v10"]["markdown"]
        assert replay.read(out / "judge-packets" / f"changed__{arm}.json")["report"] == (
            out / "changed" / arm / "report-v10.md"
        ).read_text(encoding="utf-8")
    assert rows["changed", "A1"]["calls"][0]["kind"] == "exact_prompt_replay"
    assert before == {
        p.relative_to(archived): p.read_bytes() for p in archived.rglob("*") if p.is_file()
    }
    with pytest.raises(ValueError, match="new directory"):
        replay.replay(archived, out)
    with pytest.raises(ValueError, match="separate"):
        replay.replay(archived, archived / "out")


@pytest.mark.parametrize("corruption", [
    "snapshot", "request", "binding", "cache", "response_missing", "calls_missing",
    "baseline", "labels",
])
def test_replay_fails_closed_on_archive_mismatch(archived, tmp_path, corruption):
    case = archived / "changed"
    if corruption in {"snapshot", "binding"}:
        path = case / "snapshot-A0.json"
        raw = replay.read(path)
        raw["knowledge"]["claim_bundles"][0]["evidence"][0]["quote"] += " changed"
        if corruption == "binding":
            seal(raw)
        replay.write(path, raw)
    elif corruption == "request":
        path = case / "A0/generation/call-3/request.json"
        raw = replay.read(path)
        raw["prompt"] += " altered"
        replay.write(path, raw)
    elif corruption == "cache":
        path = case / "A1/generation/calls.json"
        raw = replay.read(path)
        raw[0]["prompt_sha256"] = "unknown"
        replay.write(path, raw)
    elif corruption == "response_missing":
        (case / "A0/generation/call-3/response.json").unlink()
    elif corruption == "calls_missing":
        (case / "A0/generation/calls.json").unlink()
    elif corruption == "baseline":
        replay.write(archived / "results.json", [])
    else:
        task = replay.read(archived / "tasks.json")[0]
        wrong = replay.packet(task, "Report")
        wrong["question"] = "Changed question"
        replay.write(archived / "judge-packets/new__A0.json", wrong)
    out = tmp_path / "replay"
    with pytest.raises((ValueError, FileNotFoundError)):
        replay.replay(archived, out)
    assert not out.exists()


def test_transport_failure_does_not_use_repair_slot():
    package, value = shared_fixture()
    slots = [{"slot": 0, "status": "failed", "response": None},
             {"slot": 1, "status": "returned", "response": json.dumps(value)}]
    result = replay.simulate(slots, package, sr.SharedEvidenceResearchWorkflow)
    assert result["status"] == "needs_review"
    assert result["attempted_slots"] == [0]
