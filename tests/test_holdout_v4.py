"""D036 contracts using temporary SQLite, local fixtures and no external services."""

import copy
import inspect
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.benchmarking import holdout_v2_score as score
from app.benchmarking import holdout_v4 as runner
from app.benchmarking import resume_closeout
from app.benchmarking.feedback_trial import backup_archive
from app.benchmarking.passage_dataset import import_passages
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository

write = resume_closeout.write
read = resume_closeout.read
digest = resume_closeout.digest


@pytest.fixture(autouse=True)
def no_network_or_models(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network/model access is forbidden in v4 offline tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(resume_closeout, "get_llm_client", forbidden)
    monkeypatch.setattr(runner, "QdrantKnowledgeIndexer", forbidden)


@pytest.fixture
def frozen(tmp_path):
    root, data = tmp_path / "dataset", tmp_path / "data"
    (data / "store").mkdir(parents=True)
    (data / "papers").mkdir()
    repo = KnowledgeRepository(str(tmp_path / "original.db"))
    papers = []
    for sid in ("allowed", "denied"):
        text = f"The {sid} method provides a deterministic result."
        pdf = data / "papers" / f"{sid}.pdf"
        pdf.write_bytes(f"synthetic-{sid}".encode())
        papers.append(SimpleNamespace(
            id=sid, title=sid, version="v1", pdf_url=f"synthetic://{sid}",
            pdf_path=f"papers/{sid}.pdf", pdf_pages=1, pdf_sha256=digest(pdf),
            license="synthetic", text=text,
            pages=[SimpleNamespace(start=0, end=len(text), pdf_page=1)],
        ))
    # Test-only fixture construction; the v4 runner never invokes the passage importer.
    legacy = import_passages(repo, SimpleNamespace(sources=papers), data)
    # Real extraction publishes non-Paper knowledge too; Paper-only projection would omit it.
    with repo.database.connect() as db:
        db.execute("UPDATE entities SET entity_type='Method'")
        db.execute("UPDATE published_entities SET entity_type='Method', "
                   "payload_json=json_set(payload_json, '$.type', 'Method')")
    assert repo.list_projection_chunks() == []
    store = data / "store/knowledge.frozen.db"
    backup_archive(Path(repo.database.path), store)
    sources = [{"id": p.id, "collection_scope": legacy["sources"][p.id]["scope"],
                "pdf_path": p.pdf_path, "pdf_sha256": p.pdf_sha256} for p in papers]
    write(root / "corpus.json", {"sources": sources})
    write(root / "tasks.holdout.json", [
        {"id": "case", "question": "Describe the allowed method",
         "allowed_source_ids": ["allowed"], "expectation": {"secret": True}},
    ])
    write(data / "approved-evidence.json", {"synthetic": True})
    freeze = {
        "tasks_sha256": digest(root / "tasks.holdout.json"),
        "corpus_sha256": digest(root / "corpus.json"),
        "pdf_sha256": {p.id: p.pdf_sha256 for p in papers},
        "local_store": "data/evaluation/holdout-v4/store/knowledge.frozen.db",
        "local_store_sha256": digest(store),
        "approved_evidence_sha256": digest(data / "approved-evidence.json"),
    }
    write(root / "freeze.json", freeze)
    return SimpleNamespace(root=root, data=data, store=store, repo=repo, legacy=legacy)


def snapshot(repo, scopes):
    mem = repo.memory_repository
    project = mem.create_project(name="D036 test", goal="method", domain="research", metadata={})
    mem.replace_project_knowledge_scopes(
        project.id, scopes, expected_project_revision=project.revision,
    )
    task = mem.create_workspace_task(
        project_id=project.id, title="method", goal="method", priority="high", metadata={},
    )
    return ContextBuilderService(repo).build_context(
        ContextBuildRequest(task_id=task.id, max_tokens=16000),
    )


def fake_prepare(frozen, monkeypatch):
    indexed = []
    monkeypatch.setattr(resume_closeout, "guard", lambda _: None)
    monkeypatch.setattr(runner, "QdrantKnowledgeIndexer", lambda settings: SimpleNamespace(
        index=lambda chunks: indexed.extend(chunks),
    ))
    result = runner.prepare(root=frozen.root, data=frozen.data)
    return Path(result["output"]), indexed


def test_prepare_backs_up_frozen_store_and_indexes_formal_chunks(frozen, monkeypatch):
    protected = {p: digest(p) for folder in (frozen.root, frozen.data)
                 for p in folder.rglob("*") if p.is_file()}
    output, indexed = fake_prepare(frozen, monkeypatch)
    config = read(output / "config.json")
    mapping = read(output / "seed/source-map.json")
    assert mapping == {"sources": {
        sid: {"scope": entry["scope"]} for sid, entry in frozen.legacy["sources"].items()
    }}
    assert set(mapping) == {"sources"}
    assert {c.id for c in indexed} == set(frozen.legacy["chunks"])
    assert config["prepared"] and config["dataset"] == "v4"
    assert config["label_provenance"] == "ai-consensus"
    assert config["max_calls"] == 9 and config["arms"] == list(runner.ARMS)
    assert config["production_code"] == resume_closeout.fingerprints()
    assert config["cases"] == [{"id": "case", "question": "Describe the allowed method",
                                "source_ids": ["allowed"]}]
    assert all(digest(output / "seed" / n) == h for n, h in config["seed_files"].items())
    assert all(digest(p) == h for p, h in protected.items())
    assert not list(frozen.store.parent.glob("*-wal"))
    assert not list(frozen.store.parent.glob("*-shm"))
    # Repeated preparation uses a fresh archive and collection, never overwrites a seed.
    second, _ = fake_prepare(frozen, monkeypatch)
    assert second != output
    assert read(second / "config.json")["collection"] != config["collection"]


@pytest.mark.parametrize("field", [
    "tasks_sha256", "corpus_sha256", "local_store_sha256", "approved_evidence_sha256",
    "pdf:allowed", "pdf:denied",
])
def test_each_freeze_hash_mismatch_fails_before_any_side_effect(frozen, monkeypatch, field):
    freeze = read(frozen.root / "freeze.json")
    if field.startswith("pdf:"):
        freeze["pdf_sha256"][field.split(":")[1]] = "0" * 64
    else:
        freeze[field] = "0" * 64
    write(frozen.root / "freeze.json", freeze)
    protected = {p: digest(p) for folder in (frozen.root, frozen.data)
                 for p in folder.rglob("*") if p.is_file()}
    monkeypatch.setattr(resume_closeout, "guard", lambda _: None)
    with pytest.raises(ValueError, match="Freeze hash mismatch"):
        runner.prepare(root=frozen.root, data=frozen.data)
    assert not (frozen.data / "runs").exists()
    assert all(digest(p) == h for p, h in protected.items())


@pytest.mark.parametrize("mutation", [
    "allowed", "denied", "evidence_source", "evidence_chunk", "evidence_claim",
    "chunk_hash", "chunk_content", "chunk_document", "claim", "unpublished",
])
def test_snapshot_uses_seed_scope_and_actual_chunk_content(frozen, monkeypatch, mutation):
    output, _ = fake_prepare(frozen, monkeypatch)
    mapping = read(output / "seed/source-map.json")
    scope = mapping["sources"]["denied" if mutation == "denied" else "allowed"]["scope"]
    package = snapshot(frozen.repo, [scope])
    assert package.knowledge.claim_bundles
    bundle = package.knowledge.claim_bundles[0]
    if mutation.startswith("evidence_"):
        setattr(bundle.evidence[0], mutation.removeprefix("evidence_") + "_id", "forged")
    elif mutation == "chunk_hash":
        bundle.chunks[0].content_sha256 = "0" * 64
    elif mutation == "chunk_content":
        bundle.chunks[0].content = "forged but retains the original digest"
    elif mutation == "chunk_document":
        bundle.chunks[0].document_id = "forged"
    elif mutation == "claim":
        bundle.claim.claim_id = "forged"
    elif mutation == "unpublished":
        seed_repo = KnowledgeRepository(str(output / "seed/knowledge.db"))
        with seed_repo.database.connect() as db:
            db.execute("UPDATE claims SET status='proposed'")
    args = (package, {"source_ids": ["allowed"]}, mapping, output / "seed")
    if mutation == "allowed":
        runner.validate_snapshot(*args)
    else:
        with pytest.raises(ValueError, match="Snapshot"):
            runner.validate_snapshot(*args)


def test_v2_v3_default_validator_retains_passage_contract(frozen):
    default = inspect.signature(resume_closeout.execute).parameters["snapshot_validator"].default
    assert default is resume_closeout.validate_passage_snapshot
    package = snapshot(frozen.repo, [frozen.legacy["sources"]["allowed"]["scope"]])
    case = {"source_ids": ["allowed"]}
    default(package, case, frozen.legacy, None)
    for field, value in [("source_id", "denied"), ("text_sha256", "wrong"),
                         ("claim_id", "wrong")]:
        mapping = copy.deepcopy(frozen.legacy)
        chunk_id = package.knowledge.claim_bundles[0].chunks[0].chunk_id
        mapping["chunks"][chunk_id][field] = value
        with pytest.raises(ValueError, match="Snapshot"):
            default(package, case, mapping, None)


def test_execute_delegates_with_v4_validator_and_rejects_seed_drift(frozen, monkeypatch):
    output, _ = fake_prepare(frozen, monkeypatch)
    calls = []
    monkeypatch.setattr(resume_closeout, "execute", lambda path, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(runner, "DATASET", frozen.root)
    monkeypatch.setattr(runner, "DATA", frozen.data)
    runner.execute(output)
    assert calls == [{"cases_path": frozen.root / "freeze.json",
                      "output_root": frozen.data / "runs",
                      "snapshot_validator": runner.validate_snapshot}]
    write(output / "seed/source-map.json", {})
    with pytest.raises(ValueError, match="Seed hash mismatch"):
        runner.execute(output)
    assert len(calls) == 1


def test_projection_excludes_unpublished_knowledge(frozen, monkeypatch):
    output, _ = fake_prepare(frozen, monkeypatch)
    seed = output / "seed"
    repo = KnowledgeRepository(str(seed / "knowledge.db"))
    with repo.database.connect() as db:
        db.execute("UPDATE claims SET status='proposed' WHERE id=?", (
            next(entry["claim_id"] for entry in frozen.legacy["chunks"].values()
                 if entry["source_id"] == "denied"),
        ))
    projected = runner.projection_chunks(seed, read(seed / "source-map.json"))
    assert {c.id for c in projected} == {
        cid for cid, entry in frozen.legacy["chunks"].items() if entry["source_id"] == "allowed"
    }


def test_plan_and_scoring_read_frozen_bare_array(tmp_path):
    plan = runner.plan()
    assert (plan["cases"], plan["runs"], plan["max_model_calls"]) == (75, 150, 675)
    assert plan["arms"] == ["A0-standard", "A1-compact"]
    assert all(set(c) == {"id", "question", "source_ids"} for c in runner.cases())
    for version in ("v4", "research-holdout-v4"):
        write(tmp_path / "config.json", {"dataset": version})
        assert len(score._tasks(tmp_path)) == 75


def test_live_entrypoints_are_guarded():
    with pytest.raises(ValueError, match="offline"):
        runner.prepare()
    with pytest.raises(ValueError, match="offline"):
        runner.execute("unused")


def metrics():
    base = {"completion_rate": 1, "point_coverage": 0.5, "overclaim_rate": 0,
            "refusal_correctness": 1, "mean_input_tokens": 100, "mean_latency_seconds": 10}
    return {"A0-standard": base, "A1-compact": {**base, "point_coverage": 0.6,
            "mean_input_tokens": 120, "mean_latency_seconds": 12}}


def test_d036_pass_and_missing_arms():
    arms = metrics()
    gate = score.gate_d036(arms)
    assert gate["switch_default"] and len(gate["checks"]) == 6
    assert all(gate["checks"].values())
    for name in arms:
        assert score.gate_d036({name: arms[name]}) is None
    assert score.gate_d036({}) is None


@pytest.mark.parametrize(("metric", "value", "check"), [
    ("completion_rate", 0.99, "completion_not_worse"),
    ("point_coverage", 0.599, "coverage_plus_10pp"),
    ("overclaim_rate", 0.01, "overclaim_not_worse"),
    ("refusal_correctness", 0.99, "refusal_not_worse"),
    ("mean_input_tokens", 121, "input_tokens_within_1_2x"),
    ("mean_latency_seconds", 12.01, "latency_within_1_2x"),
    ("mean_input_tokens", None, "input_tokens_within_1_2x"),
    ("mean_latency_seconds", float("nan"), "latency_within_1_2x"),
])
def test_d036_each_requirement_fails_closed(metric, value, check):
    arms = metrics()
    arms["A1-compact"][metric] = value
    gate = score.gate_d036(arms)
    assert not gate["switch_default"] and not gate["checks"][check]


def test_api_qwen_conservative_summary_and_d036(tmp_path):
    write(tmp_path / "config.json", {"dataset": "v4"})
    task = next(
        t for t in score._tasks(tmp_path).values() if t["expectation"]["outcome"] == "scope_refusal"
    )
    case = task["id"]
    ids = [c["id"] for c in task["expectation"]["required_conclusions"]]
    assert ids
    write(tmp_path / "results.json", [
        {"case": case, "workflow": arm, "status": "completed"} for arm in metrics()
    ])
    full = {"covered": dict.fromkeys(ids, True), "violated": [False], "outcome_ok": True}
    for arm in metrics():
        report = tmp_path / case / arm / "archive/report.md"
        report.parent.mkdir(parents=True)
        report.write_text("A synthetic refusal", encoding="utf-8")
        write(tmp_path / "judge-api" / f"{case}__{arm}.json", full)
        write(tmp_path / "judge-qwen" / f"{case}__{arm}.json", {
            **full, "covered": dict.fromkeys(ids, False), "violated": [True], "outcome_ok": False,
        })
    assert score.write_packets(tmp_path) == 2
    result = score.summary(tmp_path, judges=["api", "qwen"])
    assert result["gate_D036"]["switch_default"] is False
    assert result["gate_D035"] is result["gate_D029"] is result["gate_D030"] is None
    for arm in result["arms"].values():
        assert arm["covered"] == 0 and arm["violations"] == 1 and arm["refusal_ok"] == 0
    assert all(r["judges"] == 2 for r in result["runs"])
