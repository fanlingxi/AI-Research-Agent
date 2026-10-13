import base64
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers.experiments import build_experiments_router
from app.experiments.review import ReviewInputError, ReviewService
from app.experiments.service import ExperimentReadService


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def reference(relative):
    return base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "evaluation"
    run = root / "holdout-v3" / "runs" / "test-run"
    packet = {
        "question": "有哪些证据？", "expected_outcome": "qualified_answer",
        "outcome_guide": {"qualified_answer": "回答并保留限定条件"},
        "required_conclusions": [{"id": "c1", "statement": "保留限定条件"},
                                 {"id": "c2", "statement": "说明结果"}],
        "forbidden_conclusions": ["总是成立"], "report": "报告正文",
        "missing_information": [], "instructions": "逐条判断",
    }
    verdict = {"covered": {"c1": True, "c2": False}, "violated": [False],
               "outcome_ok": True, "note": "AI 私有备注", "judge": "test-model"}
    for key in ("q1__A", "q1__B", "q2__A"):
        write(run / "judge-packets" / f"{key}.json", packet)
        write(run / "judge-api" / f"{key}.json", verdict)
    write(run / "judge-codex" / "q1__A.json", {**verdict, "outcome_ok": False})
    write(run / "judge-codex" / "q1__B.json", verdict)
    app = FastAPI()
    app.include_router(build_experiments_router(ExperimentReadService(root=root)))
    ref = reference("holdout-v3/runs/test-run")
    return TestClient(app), root, run, ref, verdict


def test_blind_review_sorting_save_and_overwrite(archive):
    client, _, run, ref, verdict = archive
    base = f"/api/experiments/review/runs/{ref}"
    catalog = client.get("/api/experiments/review/runs").json()["runs"]
    assert catalog == [{"run_ref": ref, "dataset": "holdout-v3", "name": "test-run",
                        "item_count": 3, "question_count": 2, "human_count": 0,
                        "ai_judge_count": 2}]
    items = client.get(f"{base}/items")
    assert items.status_code == 200
    assert [row["ai_agree"] for row in items.json()["items"]] == [False, True, None]
    assert "covered" not in items.text and "AI 私有备注" not in items.text
    detail = client.get(f"{base}/items/q1__A").json()
    assert detail["human"] is None and "ai_judges" not in detail
    assert detail["packet"]["report"] == "报告正文"
    original = {p: p.read_bytes() for p in run.rglob("*.json")}
    human = {**verdict, "judge": "human", "note": "人工检查", "corrected_note": "标签待修正"}
    assert client.put(f"{base}/items/q1__A/human", json=human).status_code == 200
    assert json.loads((run / "judge-human/q1__A.json").read_text(encoding="utf-8")) == human
    revealed = client.get(f"{base}/items/q1__A").json()
    assert set(revealed["ai_judges"]) == {"api", "codex"}
    assert revealed["human"] == human
    assert "ai_judges" not in client.get(f"{base}/items/q1__B").json()
    assert client.put(f"{base}/items/q1__A/human",
                      json={**human, "outcome_ok": False}).status_code == 200
    assert client.get(f"{base}/items/q1__A").json()["human"]["outcome_ok"] is False
    ordered = client.get(f"{base}/items").json()
    assert ordered["human_count"] == 1
    assert ordered["items"][0]["key"] == "q1__A"  # disagreement has first priority
    assert all(path.read_bytes() == content for path, content in original.items())
    assert list((run / "judge-human").iterdir()) == [run / "judge-human/q1__A.json"]


@pytest.mark.parametrize("change,status", [
    ({"covered": {"unknown": True}}, 400),
    ({"covered": {"c1": True}}, 400),
    ({"violated": []}, 400),
    ({"violated": [False, False]}, 400),
    ({"outcome_ok": "false"}, 422),
    ({"covered": {"c1": 1, "c2": False}}, 422),
    ({"judge": "api"}, 422),
    ({"unexpected": "field"}, 422),
])
def test_write_validation_does_not_create_human_file(archive, change, status):
    client, _, run, ref, verdict = archive
    value = {**verdict, "judge": "human", **change}
    response = client.put(f"/api/experiments/review/runs/{ref}/items/q1__A/human", json=value)
    assert response.status_code == status
    assert not (run / "judge-human").exists()


def test_agreement_counts_only_paired_labels_and_each_conclusion(archive):
    client, _, run, ref, verdict = archive
    base = f"/api/experiments/review/runs/{ref}"
    before = client.get(f"{base}/agreement").json()
    assert before["human_count"] == 0
    assert before["judges"]["api"]["outcome_ok"] == {"matched": 0, "total": 0, "rate": None}
    for key in ("q1__A", "q2__A"):
        value = {**verdict, "judge": "human", "covered": {"c1": True, "c2": True}}
        assert client.put(f"{base}/items/{key}/human", json=value).status_code == 200
    # Orphan judgments never count as reviewed packets.
    write(run / "judge-human/orphan.json", verdict)
    result = client.get(f"{base}/agreement").json()
    assert result["human_count"] == 2
    api, codex = result["judges"]["api"], result["judges"]["codex"]
    assert (api["paired_count"], codex["paired_count"]) == (2, 1)
    assert api["covered"] == {"matched": 2, "total": 4, "rate": 0.5}
    assert api["covered_by_id"]["c2"] == {"matched": 0, "total": 2, "rate": 0}
    assert api["violated"] == {"matched": 2, "total": 2, "rate": 1}
    assert codex["outcome_ok"] == {"matched": 0, "total": 1, "rate": 0}


@pytest.mark.parametrize("relative", [
    "../outside", "/tmp/outside", "D:/outside", "holdout-v3/../runs/test-run",
    "holdout-v3\\runs\\test-run", "holdout-v3/runs/CON", "holdout-v3//runs/test-run",
])
def test_encoded_run_traversal_rejected(archive, relative):
    client, _, _, _, _ = archive
    response = client.get(f"/api/experiments/review/runs/{reference(relative)}/items")
    assert response.status_code == 400


@pytest.mark.parametrize("key", ["..", "../q1__A", "/tmp/file", "D:\\file", "q:stream", "NUL"])
def test_invalid_keys_cannot_read_or_write(archive, key):
    _, root, _, ref, _ = archive
    with pytest.raises(ReviewInputError):
        ReviewService(root).item(ref, key)


@pytest.mark.parametrize("linked", [
    "run", "judge-packets", "judge-api", "judge-human", "human-file",
])
def test_symlink_escape_rejected(archive, tmp_path, linked):
    client, root, run, ref, verdict = archive
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "q1__A.json"
    write(target, {**verdict, "judge": "human"})
    if linked == "run":
        link = run.parent / "alias"
        ref = reference("holdout-v3/runs/alias")
        destination = run
    elif linked == "human-file":
        (run / "judge-human").mkdir()
        link, destination = run / "judge-human/q1__A.json", target
    else:
        link, destination = run / linked, outside
        if link.exists():
            link.rename(run / f"original-{linked}")
    try:
        link.symlink_to(destination, target_is_directory=destination.is_dir())
    except OSError:
        if os.name == "nt" and destination.is_dir():
            import _winapi

            _winapi.CreateJunction(str(destination), str(link))
        else:
            pytest.skip("OS does not allow file symlink creation")
    url = f"/api/experiments/review/runs/{ref}/items/q1__A"
    # A human directory is also checked before reading/revealing labels.
    assert client.get(url).status_code == 400
    if linked in {"run", "judge-packets", "judge-human", "human-file"}:
        assert client.put(f"{url}/human", json={**verdict, "judge": "human"}).status_code == 400
    assert json.loads(target.read_text(encoding="utf-8"))["note"] == verdict["note"]


def test_hard_link_write_replaces_only_human_file(archive):
    client, _, run, ref, verdict = archive
    folder = run / "judge-human"
    folder.mkdir()
    ai_file = run / "judge-api/q1__A.json"
    os.link(ai_file, folder / "q1__A.json")
    response = client.put(f"/api/experiments/review/runs/{ref}/items/q1__A/human",
                          json={**verdict, "judge": "human", "note": "人工"})
    assert response.status_code == 200
    assert json.loads(ai_file.read_text(encoding="utf-8")) == verdict


def test_evaluation_root_can_be_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("ARA_EVALUATION_ROOT", str(tmp_path))
    assert ExperimentReadService().root == tmp_path.resolve()
    assert ReviewService(tmp_path).runs() == {"runs": []}
