import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers.experiments import build_experiments_router
from app.benchmarking.experiments import ExperimentDataError, ExperimentReadService
from app.benchmarking.semantic_calibration import annotation_pack


def archive(tmp_path):
    root = tmp_path / "semantic"
    root.mkdir()
    records = [{"task_id": "case", "status": "completed", "review": {
        "version": "finding-support-v1", "mode": "observation_only",
        "snapshot_sha256": "snapshot", "draft_sha256": "draft", "findings": [{
            "finding_id": "finding-1", "assertion": "Claim", "citations": [{
                "evidence_id": "e", "quote": "Quote", "source_version": "v1",
            }], "verdict": "supported", "status": "reviewed", "reason": "Reason",
        }],
    }}, {"task_id": "failed", "status": "judge_failed", "error": "api_key=hidden"}]
    (root / "results.json").write_text(json.dumps(records), encoding="utf-8")
    labels = annotation_pack(records)
    labels["cases"][0].update(label="insufficient_evidence", reviewer="operator", note="Missing")
    (root / "supervised-labels.json").write_text(json.dumps(labels), encoding="utf-8")
    return root, labels


def test_semantic_archive_preserves_failures_disagreement_and_no_import(tmp_path):
    root, _ = archive(tmp_path)
    app = FastAPI()
    app.include_router(build_experiments_router(ExperimentReadService(semantic_archive=root)))
    with TestClient(app) as client:
        response = client.get("/api/experiments/semantic-observation")
    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["planned_runs"] == 2
    assert data["summary"]["agreement_on_labeled"] == 0
    assert data["summary"]["false_support_count"] == 1
    assert data["records"][1]["status"] == "judge_failed"
    assert not data["publication_gate_enabled"]
    assert "hidden" not in response.text


def test_semantic_archive_empty_unlabeled_and_mismatched_labels(tmp_path):
    root = tmp_path / "missing"
    service = ExperimentReadService(semantic_archive=root)
    assert service.semantic_observation()["available"] is False
    root, labels = archive(tmp_path)
    service = ExperimentReadService(semantic_archive=root)
    labels["cases"][0]["assertion"] = "Tampered"
    (root / "supervised-labels.json").write_text(json.dumps(labels), encoding="utf-8")
    with pytest.raises(ExperimentDataError):
        service.semantic_observation()
    (root / "supervised-labels.json").unlink()
    assert service.semantic_observation()["summary"]["human_labeled"] == 0
    (root / "results.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ExperimentDataError):
        service.semantic_observation()


def test_semantic_archive_rejects_resolved_escape(tmp_path, monkeypatch):
    root, _ = archive(tmp_path)
    original = Path.resolve

    def escaped(path, *args, **kwargs):
        if path == root / "results.json":
            return tmp_path / "outside.json"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped)
    with pytest.raises(ExperimentDataError, match="受管目录"):
        ExperimentReadService(semantic_archive=root).semantic_observation()
