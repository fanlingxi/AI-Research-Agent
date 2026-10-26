"""Three-version replay, quote-local omission and exact runtime repair diagnostics."""

import json
import socket
from copy import deepcopy

import pytest

from app.agent import structured_research as sr
from app.benchmarking import replay_v10 as archive_tools
from app.benchmarking import replay_v11 as replay
from app.llms.provider import LangChainChatClient
from tests.test_replay_v10 import archive_case
from tests.test_replay_v10 import archived as archived  # reused immutable archive fixture
from tests.test_research_v10 import shared_fixture


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real model/network calls are forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(LangChainChatClient, "invoke", forbidden)


def test_replay_partial_insufficient_and_inexact_repair(tmp_path):
    archive = tmp_path / "archive"
    tasks = []
    for name in ("partial", "insufficient", "inexact"):
        tasks.append(archive_case(archive, name, "repair" if name == "inexact" else "new_complete"))
        if name == "inexact":
            continue
        path = archive / name / "A0/generation/call-3/response.json"
        report = json.loads(archive_tools.read(path)["text"])
        findings = report["answer_parts"][0]["findings"]
        findings[0]["claim_bundle_id"] = "second"
        if name == "partial":
            findings.append(deepcopy(findings[0]))
        findings[0]["assertion"] = "Unsupported numerical result 999"
        archive_tools.write(path, {"text": json.dumps(report)})
    archive_tools.write(archive / "tasks.json", tasks)
    before = {p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    out = tmp_path / "replay"
    summary = replay.replay(archive, out)
    assert summary["new_model_calls"] == 0
    assert summary["v10_states"] == summary["v11_states"] == {"completed": 3}
    assert summary["v8_states"] == {"completed": 2, "inexact_repair": 1}
    assert summary["inexact_repair"] == {"v8": 1, "v10": 0, "v11": 0}
    assert summary["numeric_omitted"] == summary["judge_packets"] == 2
    assert summary["answer_transitions"] == {
        "complete -> partial": 1, "complete -> insufficient_evidence": 1, "complete -> complete": 1,
    }
    rows = {r["case"]: r for r in archive_tools.read(out / "results.json")}
    assert rows["partial"]["v11"]["answer_status"] == "partial"
    assert rows["insufficient"]["v11"]["answer_status"] == "insufficient_evidence"
    for name in ("partial", "insufficient"):
        row = rows[name]
        assert row["v11"]["used_slot"] == 0
        assert row["v11"]["attempted_slots"] == [0]
        assert "999" not in row["v11"]["markdown"]
        assert "999" in row["v10"]["markdown"]
        packet = archive_tools.read(out / "judge-packets" / f"{name}__A0.json")
        assert packet["report"] == row["v11"]["markdown"]
    assert rows["inexact"]["v8"]["markdown"] is None
    assert rows["inexact"]["v8"]["attempted_slots"] == [0]
    assert before == {p.relative_to(archive): p.read_bytes()
                      for p in archive.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="new directory"):
        replay.replay(archive, out)
    with pytest.raises(ValueError, match="separate"):
        replay.replay(archive, archive / "out")


@pytest.mark.parametrize("first,diagnostic", [
    ("invalid json", "Invalid JSON/schema, excessive findings or inconsistent answer state"),
    ('{"title":"Bad"}',
     "Invalid schema: respect required fields, IDs, types and the 12-finding limit"),
    (None, "Support must be an exact contiguous quote from cited evidence in its own bundle"),
])
@pytest.mark.parametrize("workflow", list(replay.VERSIONS.values()))
def test_exact_repair_uses_runtime_safe_diagnostic(first, diagnostic, workflow):
    package, report = shared_fixture()
    report["answer_parts"][0]["findings"][0]["claim_bundle_id"] = "second"
    if first is None:
        invalid = deepcopy(report)
        invalid["answer_parts"][0]["findings"][0]["support"][0]["quote"] = (
            "Forged quote not in source.")
        first = json.dumps(invalid)
    base_prompt = "verified base prompt"
    repair = {"system_prompt": sr.REPORT_SYSTEM_PROMPT, "prompt": base_prompt + (
        f"\nPrevious attempt invalid: {diagnostic}. "
        "Produce a corrected complete object within the limits.")}
    slots = [
        {"slot": 0, "status": "returned", "response": first,
         "request": {"prompt": base_prompt}},
        {"slot": 1, "status": "returned", "response": json.dumps(report),
         "request": repair, "prompt_sha256": archive_tools.request_digest(repair)},
    ]
    result = replay.simulate(slots, package, workflow)
    assert result["status"] == "completed"
    assert result["exact"] and result["used_slot"] == 1
    slots[1]["prompt_sha256"] = "different"
    result = replay.simulate(slots, package, workflow)
    assert result["status"] == "inexact_repair"
    assert not result["exact"] and result["markdown"] is None
    slots.pop()
    result = replay.simulate(slots, package, workflow)
    assert result["status"] == "unavailable_repair" and not result["exact"]
    slots[0]["status"] = "failed"
    assert replay.simulate(slots, package, workflow)["status"] == "needs_review"


@pytest.mark.parametrize("corruption", ["snapshot", "request", "cache", "calls", "labels"])
def test_replay_reuses_archive_integrity_checks(archived, tmp_path, corruption):
    case = archived / "changed"
    if corruption == "snapshot":
        path = case / "snapshot-A0.json"
        value = archive_tools.read(path)
        value["task"]["title"] = "Changed task"
        archive_tools.write(path, value)
    elif corruption == "request":
        path = case / "A0/generation/call-3/request.json"
        value = archive_tools.read(path)
        value["prompt"] += " changed"
        archive_tools.write(path, value)
    elif corruption == "cache":
        path = case / "A1/generation/calls.json"
        value = archive_tools.read(path)
        value[0]["prompt_sha256"] = "unknown"
        archive_tools.write(path, value)
    elif corruption == "calls":
        (case / "A0/generation/calls.json").unlink()
    else:
        packet = archive_tools.packet(archive_tools.read(archived / "tasks.json")[0], "Report")
        packet["question"] = "Changed question"
        archive_tools.write(archived / "judge-packets/new__A0.json", packet)
    out = tmp_path / "replay"
    with pytest.raises((ValueError, FileNotFoundError)):
        replay.replay(archived, out)
    assert not out.exists()
