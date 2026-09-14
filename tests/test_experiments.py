import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers.experiments import build_experiments_router
from app.benchmarking.experiments import ExperimentDataError, ExperimentReadService
from scripts.validate_paper_benchmark import file_digest


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _service(tmp_path, family="a04"):
    root, dataset = tmp_path / "evaluation", tmp_path / "papers"
    text = "Approved evidence from a frozen paper."
    corpus = {
        "sources": [
            {
                "id": "paper",
                "version": "v1",
                "title": "Frozen Paper",
                "text": text,
                "pdf_sha256": "pdf-hash",
                "pages": [{"pdf_page": 1, "start": 0, "end": len(text)}],
            }
        ]
    }
    task = {
        "id": "test-q1",
        "question": "如何对比证据？",
        "split": "dev",
        "allowed_source_ids": ["paper"],
    }
    _write(dataset / "corpus.json", corpus)
    _write(dataset / "tasks.dev.json", {"tasks": [task]})
    _write(dataset / "tasks.holdout.json", {"tasks": []})
    _write(
        dataset / "freeze.json",
        {
            name: file_digest(dataset / name)
            for name in ["corpus.json", "tasks.dev.json", "tasks.holdout.json"]
        },
    )
    directory = root / family / "sample"
    digest = file_digest(dataset / "freeze.json")
    config = (
        {
            "schema": "a04-retrieval-comparison-v1",
            "split": "dev",
            "dataset_freeze_sha256": digest,
            "api_key": "must-not-be-returned",
        }
        if family == "a04"
        else {
            "version": "a02-live-v1",
            "approval": {"freeze_sha256": digest},
            "policy": {"request_model": "test-model", "api_key": "must-not-be-returned"},
        }
    )
    row = {
        "task_id": "test-q1",
        "path": "project_run",
        "strategy": "legacy",
        "status": "completed" if family == "a02" else "ok",
        "attempt_id": "sample/01-test-q1",
        "selected": [
            [
                {
                    "source_id": "paper",
                    "source_version": "v1",
                    "pdf_sha256": "pdf-hash",
                    "pdf_page": 1,
                    "start": 0,
                    "end": len(text),
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            ]
        ],
        "metrics": {"recall@10": 0.0},
        "budget_span_recall": 0.0,
        "latency_ms": 10,
        "cost_cny": 0,
        "model_calls": 0,
    }
    _write(directory / "config.json", config)
    _write(directory / "results.json", [row])
    _write(
        directory / "summary.json",
        {
            "planned_count": 1,
            "results": [row],
            "budget": {
                "calls": [
                    {
                        "attempt_id": "sample/01-test-q1",
                        "status": "settled",
                        "cost_upper_micro_cny": 2500,
                    },
                    {
                        "attempt_id": "different/01-test-q1",
                        "status": "settled",
                        "cost_upper_micro_cny": 999999,
                    },
                ]
            },
            "default_decision": "retain_legacy",
        },
    )
    return ExperimentReadService(root, dataset), directory, row


def test_missing_trials_are_preserved_and_safe_fields_are_allowlisted(tmp_path):
    service, _, _ = _service(tmp_path)
    result = service.describe("a04--sample")
    assert result["experiment"]["planned"] == 6
    assert result["experiment"]["recorded"] == 1
    assert result["experiment"]["status_counts"] == {"completed": 1, "missing": 5}
    assert "must-not-be-returned" not in json.dumps(result)
    detail = service.task_detail("a04--sample", "test-q1")
    assert len(detail["variants"]) == 3
    assert detail["variants"][0]["evidence"][0]["text"].startswith("Approved")
    assert detail["variants"][1]["span_recall"] is None


@pytest.mark.parametrize("change", ["source_version", "pdf_sha256", "text_sha256", "source_id"])
def test_unverified_or_out_of_scope_source_text_is_not_exposed(tmp_path, change):
    service, directory, row = _service(tmp_path)
    row["selected"][0][0][change] = "wrong"
    _write(directory / "results.json", [row])
    variant = service.task_detail("a04--sample", "test-q1")["variants"][0]
    assert not variant["evidence"] and variant["issues"]


def test_failed_and_missing_generation_artifacts_keep_unknown_bill_and_correct_call_scope(tmp_path):
    service, directory, row = _service(tmp_path, "a02")
    _write(directory / "01-test-q1" / "run.json", {"error_message": "token budget exhausted"})
    result = service.task_detail("a02--sample", "test-q1")["variants"][0]
    assert result["cost_cny"] is None and result["semantic_success"] is None
    assert result["cost_upper_cny"] == 0.0025 and result["model_calls"] == 1
    assert result["error"] == "token budget exhausted"
    assert any("产物文件缺失" in issue for issue in result["issues"])
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    summary["results"][0]["status"] = "failed"
    _write(directory / "summary.json", summary)
    assert service.describe("a02--sample")["experiment"]["status_counts"] == {"failed": 1}


def test_artifact_traversal_and_symlink_escape_are_rejected(tmp_path):
    service, directory, _ = _service(tmp_path)
    with pytest.raises(KeyError):
        service.describe("a04--../secret")
    secret = tmp_path / "secret.json"
    _write(secret, {"secret": "do not expose"})
    target = directory / "config.json"
    target.unlink()
    target.symlink_to(secret)
    with pytest.raises(ExperimentDataError, match="受管目录"):
        service.describe("a04--sample")
    assert service.list_experiments()["unavailable"]


def test_corrupt_duplicate_records_do_not_disappear_from_catalog(tmp_path):
    service, directory, row = _service(tmp_path)
    _write(directory / "results.json", [row, row])
    result = service.list_experiments()
    assert result["experiments"] == []
    assert len(result["unavailable"]) == 1


def test_new_generation_strategy_is_not_mislabeled_as_legacy(tmp_path):
    service, directory, _ = _service(tmp_path, "a02")
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    config.update(version="a02-live-v2", strategy="bm25-v1; vector=false; graph=false")
    _write(directory / "config.json", config)
    assert service.describe("a02--sample")["tasks"][0]["variants"][0]["strategy"] == "bm25-v1"


def test_read_only_api_and_empty_catalog(tmp_path):
    service, _, _ = _service(tmp_path)
    app = FastAPI()
    app.include_router(build_experiments_router(service))
    client = TestClient(app)
    assert client.get("/api/experiments").status_code == 200
    assert client.get("/api/experiments/a04--sample/tasks/test-q1").status_code == 200
    assert client.get("/api/experiments/a04--sample/tasks/test-q1?path=bad").status_code == 422
    assert client.get("/api/experiments/a04--missing").status_code == 404
    assert client.post("/api/experiments", json={}).status_code == 405
    assert ExperimentReadService(tmp_path / "empty").list_experiments() == {
        "experiments": [],
        "unavailable": [],
    }
