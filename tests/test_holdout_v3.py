"""Offline v3 construction, version isolation, D030 and archive dataset selection."""

import json
import shutil

import pytest

from app.benchmarking import holdout_v2, holdout_v2_score
from scripts.build_holdout_v3 import _corpus, _tasks, build
from scripts.validate_holdout_v2 import (
    DATA_V3,
    DATASET,
    dataset_path,
    load_tasks,
    validate_dataset,
)

local_v3 = pytest.mark.skipif(
    not (DATA_V3 / "work/draft.json").is_file(), reason="local v3 inputs are not distributed",
)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@local_v3
def test_build_and_validate_every_local_v3_quote(tmp_path):
    root = tmp_path / "holdout-v3"
    built = build(DATA_V3, root)
    result = validate_dataset(root, data=DATA_V3)
    corpus = json.loads((root / "corpus.json").read_text(encoding="utf-8"))
    assert result["dataset_version"] == "research-holdout-v3"
    assert result["papers"] == 8
    assert result["quotes_checked_against_pdf_text"] == len(corpus["spans"]) > 0
    assert all("text" not in s and s["pdf_path"] == f"papers/{s['version']}.pdf"
               for s in corpus["sources"])
    assert all(t.annotation.drafted_by == "Codex" for t in load_tasks(root))
    if not (DATA_V3 / "work/consensus.json").exists():
        assert built["pending_adjudication"] == result["tasks"]
        assert "待仲裁" in built["notice"]
        assert all(v.answerability is None for t in load_tasks(root) for v in t.annotation.verdicts)

    # Use the same inputs again: freeze hashes must be reproducible.
    frozen = (root / "freeze.json").read_bytes()
    build(DATA_V3, root)
    assert (root / "freeze.json").read_bytes() == frozen


def test_d030_preset_has_default_and_v8_candidate():
    a, c = holdout_v2.arm_preset("d030")
    assert a == {"name": "A-default", "workflow": "research", "context": {}}
    assert c == {"name": "C-candidate", "workflow": "research_v8",
                 "context": holdout_v2.resume_closeout.SHARED_CONTEXT}


@local_v3
def test_builder_rejects_a_quote_missing_from_its_pdf_page():
    with pytest.raises(ValueError, match="bad-span: quote not found verbatim"):
        _corpus(DATA_V3, [{"id": "bad-span", "source_id": "rankgpt", "pdf_page": 1,
                          "quote": "THIS IS NOT A QUOTE FROM THE PAPER. \u2603"}])


@local_v3
def test_v3_plan_and_corpus_select_v3():
    planned = holdout_v2.plan("dev", arms="d030", dataset="v3")
    assert planned["dataset"] == "research-holdout-v3"
    assert planned["arms"] == ["A-default", "C-candidate"]
    assert planned["workflows"] == ["research", "research_v8"]
    assert planned["runs"] == 2 * len(holdout_v2.cases("dev", "v3"))
    assert len(holdout_v2.cases("all", "v3")) == len(load_tasks(dataset_path("v3")))
    corpus = holdout_v2.load_corpus(DATA_V3, {"rankgpt"}, dataset="v3")
    assert corpus.sources[0].pdf_path == "papers/2023.emnlp-main.923.pdf"


def test_validator_rejects_mixed_versions_and_explicit_mismatch(tmp_path):
    shutil.copytree(DATASET, tmp_path, dirs_exist_ok=True)
    with pytest.raises(ValueError, match="dataset version mismatch"):
        validate_dataset(tmp_path, data=None, version="v3")
    path = tmp_path / "tasks.dev.json"
    tasks = json.loads(path.read_text(encoding="utf-8"))
    tasks.update(dataset_version="research-holdout-v3", protocol_version="research-holdout-v3-task")
    _write(path, tasks)
    with pytest.raises(ValueError, match="research-holdout-v2"):
        validate_dataset(tmp_path, data=None, verify_freeze=False)


@local_v3
def test_consensus_overrides_and_drops_are_applied_before_annotation():
    draft = json.loads((DATA_V3 / "work/draft.json").read_text(encoding="utf-8"))
    first, second = draft["tasks"][:2]
    verdicts = [
        {"labeler": labeler, "answerability": "answerable", "answerability_agrees": True,
         "points": "full", "quote_overlap": "all"}
        for labeler in ("Codex", "DeepSeek")
    ]
    records = {
        first["id"]: {"status": "adjudicated", "adjudication": "Claude 已仲裁。",
                      "verdicts": verdicts, "overrides": {"question": "覆盖后的问题？",
                                                         "expectation": {"rationale": "修订"}}},
        second["id"]: {"drop": True, "adjudication": "删除重复题"},
    }
    tasks, dropped, pending, _ = _tasks(draft["tasks"], records, draft["evidence_spans"])
    assert tasks[0]["question"] == "覆盖后的问题？"
    assert tasks[0]["expectation"]["rationale"] == "修订"
    assert tasks[0]["expectation"]["required_conclusions"] == first["expectation"][
        "required_conclusions"
    ]
    assert tasks[0]["annotation"]["status"] == "adjudicated"
    assert tasks[0]["annotation"]["verdicts"] == verdicts
    assert dropped == [{"id": second["id"], "reason": "删除重复题"}]
    assert len(pending) == len(draft["tasks"]) - 2


@pytest.mark.parametrize("dataset", ["v3", "research-holdout-v3"])
def test_scorer_uses_archive_dataset_and_applies_c_gate(tmp_path, monkeypatch, dataset):
    # Same task ID in both roots catches accidental fallback to v2 labels.
    v2, v3, run = (tmp_path / name for name in ("v2", "v3", "run"))
    task = json.loads((DATASET / "tasks.holdout.json").read_text(encoding="utf-8"))["tasks"][0]
    for root, question in ((v2, "v2 question"), (v3, "v3 question")):
        _write(root / "tasks.dev.json", {"tasks": []})
        _write(root / "tasks.holdout.json", {"tasks": [{**task, "question": question}]})
    monkeypatch.setattr(holdout_v2_score, "DATASET", v2)
    monkeypatch.setattr(holdout_v2_score, "dataset_path", lambda name: v3 if "v3" in name else v2)
    _write(run / "config.json", {"dataset": dataset})
    _write(run / "results.json", [
        {"case": task["id"], "workflow": arm, "status": "completed"}
        for arm in ("A-default", "C-candidate")
    ])
    for arm in ("A-default", "C-candidate"):
        archive = run / task["id"] / arm / "archive"
        archive.mkdir(parents=True)
        (archive / "report.md").write_text("report", encoding="utf-8")
        verdict = {"covered": {c["id"]: arm == "C-candidate"
                               for c in task["expectation"]["required_conclusions"]},
                   "violated": [False], "outcome_ok": True}
        for judge in ("api", "claude"):
            _write(run / f"judge-{judge}" / f"{task['id']}__{arm}.json", verdict)
    assert holdout_v2_score.write_packets(run) == 2
    packet = json.loads((run / "judge-packets" / f"{task['id']}__C-candidate.json").read_text(
        encoding="utf-8",
    ))
    assert packet["question"] == "v3 question"
    assert holdout_v2_score.summary(run)["gate_D030"]["switch_default_to_C"] is True
    # Summary must use the selected dataset's conclusions as well as its question.
    task["expectation"]["required_conclusions"][0]["id"] = "new-v3-point"
    _write(v3 / "tasks.holdout.json", {"tasks": [task]})
    result = holdout_v2_score.summary(run)
    assert result["arms"]["C-candidate"]["covered"] == len(
        task["expectation"]["required_conclusions"]
    ) - 1
