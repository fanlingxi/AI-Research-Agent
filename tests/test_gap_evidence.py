import json
from unittest.mock import Mock

import pytest

from app.agent.repository import _sha256
from app.context.models import ContextBuildRequest
from app.context.retrieval import CandidateReference
from app.context.retrieval_audit import evidence_identity
from app.knowledge.schemas import CandidateEntity, CandidateRelation, EvidenceSpan
from tests.core_fixtures import persist_evidence_chunk
from tests.test_follow_up import stack as _parent_stack
from tests.test_research_workflow import _analysis, _draft, _plan

parent_stack = _parent_stack


@pytest.fixture
def stack(parent_stack):
    repo, service, worker, project, task, run, client = parent_stack
    service.context_builder.vector_retriever = Mock(retrieve=Mock(return_value=[]))
    service.context_builder.graph_retriever = Mock(retrieve=Mock(return_value=[]))
    service.settings.research_default_profile = "classic"
    _set_draft(repo, service, run, answer_status="partial")
    return parent_stack


def _set_draft(repo, service, run, **changes):
    """Rewrite the parent report as a structured one; only those state per-question gaps."""
    output = service.repository.get_output(run.id)
    structured = {**output.structured, "draft": {**output.structured["draft"], **changes}}
    digest = _sha256({"output_type": output.output_type, "structured": structured,
                      "rendered_text": output.rendered_text})
    with repo._connect() as connection:
        connection.execute(
            "UPDATE agent_run_outputs SET structured_json = ?, output_sha256 = ? WHERE run_id = ?",
            (json.dumps(structured), digest, run.id),
        )


@pytest.mark.parametrize("status", [None, "complete"])
def test_reports_without_stated_gaps_are_rejected(stack, status):
    repo, service, _, _, _, run, _ = stack
    _set_draft(repo, service, run, answer_status=status)
    response = _search(stack)
    assert response.status_code == 409
    assert "没有可补充证据的缺口" in response.json()["detail"]


def _request(service, run):
    return {"output_sha256": service.repository.get_output(run.id).output_sha256, "gap_index": 0}


def _search(stack, **changes):
    _, service, _, _, _, run, client = stack
    return client.post(f"/api/v1/agent-runs/{run.id}/gap-evidence",
                       json={**_request(service, run), **changes})


def _candidate(repo, key, *, collection="research-scope", summary=None):
    ingestion = repo.create_ingestion(
        collection=collection, sources=[f"{key}.pdf"], pdf_max_pages=1, enqueue=False,
    )
    repo.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id=f"paper-{key}", chunk_id=f"chunk-{key}", page_start=1, page_end=1,
        quote=summary or f"Governed context evidence adds independent support for {key}.",
    )
    persist_evidence_chunk(repo, ingestion_id=ingestion.id, evidence=evidence, title=f"Paper {key}")
    candidate = CandidateEntity(
        id=key, ingestion_id=ingestion.id, topic_slug=collection, name=f"Context {key}",
        type="Method", summary=evidence.quote, confidence=0.9, evidence=evidence,
    )
    repo.add_candidate_entity(candidate)
    return candidate


def _command(stack):
    _, service, _, project, task, run, _ = stack
    claim_ids = [item["claim_id"] for item in _search(stack).json()["reviewed"]]
    if not claim_ids:
        package = service.load_context_snapshot(run.id)
        claim_ids = [package.knowledge.claim_bundles[0].claim.claim_id]
    return {
        "mode": "project_run", "instruction": task.goal, "project_id": project.id,
        "task": {"kind": "existing", "task_id": task.id},
        "gap_evidence_of": {"run_id": run.id, **_request(service, run), "claim_ids": claim_ids},
    }


def test_formal_and_pending_review_transition_are_read_only_and_snapshot_safe(stack):
    repo, service, _, project, task, run, client = stack
    original_output = service.repository.get_output(run.id).model_dump()
    original_snapshot = service.load_context_snapshot(run.id).model_dump()
    reviewed = _candidate(repo, "reviewed")
    repo.publish_entity(reviewed.id)
    pending = _candidate(repo, "pending")
    rejected = _candidate(repo, "rejected")
    repo.reject_candidate(rejected.id)
    repo.set_prereview(pending.id, {"verdict": "unsure", "reason": "需要人工检查"})
    outside = repo.create_collection("Outside")
    hidden = _candidate(repo, "hidden", collection=outside.slug)
    repo.publish_entity(hidden.id)
    _candidate(repo, "hidden-pending", collection=outside.slug)
    service.llm = Mock(invoke=Mock(side_effect=AssertionError("No LLM during discovery")))
    with repo._connect() as connection:
        before = list(connection.iterdump())
    response = _search(stack)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scopes"] == ["research-scope"]
    assert len(data["reviewed"]) == 1
    assert "reviewed" in data["reviewed"][0]["statement"]
    assert data["reviewed"][0]["evidence"][0]["document_title"] == "Paper reviewed"
    assert data["reviewed_in_snapshot_count"] == 1
    assert [p["candidate_id"] for p in data["pending"]] == [pending.id]
    assert data["pending"][0]["prereview"] == {"decision": "uncertain", "reason": "需要人工检查"}
    assert data["can_rerun"] is True
    assert data["search"]["method"] == "lexical"
    assert data["search"]["notices"]
    service.llm.invoke.assert_not_called()
    with repo._connect() as connection:
        assert list(connection.iterdump()) == before
    fresh = service.context_builder.build_context(ContextBuildRequest(
        task_id=task.id, project_id=project.id, max_tokens=16000,
    ))
    assert not any("pending" in b.claim.statement for b in fresh.knowledge.claim_bundles)
    decision = client.post(f"/api/knowledge/candidates/{pending.id}/decision",
                           json={"decision": "approve"})
    assert decision.status_code == 200, decision.text
    data = _search(stack).json()
    assert data["pending"] == []
    assert any("pending" in b["statement"] for b in data["reviewed"])
    assert service.repository.get_output(run.id).model_dump() == original_output
    assert service.load_context_snapshot(run.id).model_dump() == original_snapshot


@pytest.mark.parametrize("change", ["hash", "negative", "index", "unfinished", "missing-output",
                                   "empty-scope", "disjoint-scope", "malformed-draft"])
def test_gap_conflicts(stack, change):
    repo, service, _, project, _, run, _ = stack
    request = _request(service, run)
    if change == "hash":
        request["output_sha256"] = "stale"
    elif change in {"negative", "index"}:
        request["gap_index"] = -1 if change == "negative" else 999
    elif change in {"empty-scope", "disjoint-scope"}:
        scopes = [] if change == "empty-scope" else [repo.create_collection("Other").slug]
        current = repo.memory_repository.get_project(project.id)
        repo.memory_repository.replace_project_knowledge_scopes(
            project.id, scopes, expected_project_revision=current.revision,
        )
    else:
        with repo._connect() as connection:
            if change == "unfinished":
                connection.execute(
                    "UPDATE agent_runs SET status = 'running' WHERE id = ?", (run.id,),
                )
            elif change == "missing-output":
                connection.execute("DELETE FROM agent_run_outputs WHERE run_id = ?", (run.id,))
            else:
                connection.execute(
                    "UPDATE agent_run_outputs SET structured_json = '{}' WHERE run_id = ?",
                    (run.id,),
                )
    response = stack[-1].post(f"/api/v1/agent-runs/{run.id}/gap-evidence", json=request)
    assert response.status_code == 409, response.text


def test_missing_run_and_snapshot_only_matches(stack):
    _, service, _, _, _, run, client = stack
    assert client.post("/api/v1/agent-runs/missing/gap-evidence",
                       json=_request(service, run)).status_code == 404
    assert client.get("/api/v1/agent-runs/missing/gap-reruns").status_code == 404
    data = _search(stack).json()
    assert data["reviewed"] == []
    assert data["reviewed_in_snapshot_count"] == 1
    assert data["can_rerun"] is False


def test_vector_outside_transaction_revalidates_source_and_scope(stack):
    repo, service, _, _, _, _, _ = stack
    approved = _candidate(repo, "new-vector", summary="Independent zzzz findings.")
    repo.publish_entity(approved.id)
    with repo._connect() as connection:
        bundles = service.context_builder.knowledge_reader.read_formal_bundles_tx(
            connection, ["research-scope"],
        )
    evidence = next(b.evidence[0] for b in bundles if b.entity["name"] == approved.name)
    identity = evidence_identity(evidence)

    def retrieve(query, **kwargs):
        assert len(query) <= 2401
        assert kwargs["limit"] == 40
        assert kwargs["collection_scopes"] == ["research-scope"]
        with repo._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        return [CandidateReference("vector", "chunk", evidence.chunk["id"], 0.9, identity),
                CandidateReference("vector", "chunk", "outside", 1.0)]

    service.context_builder.vector_retriever = Mock(retrieve=Mock(side_effect=retrieve))
    data = _search(stack).json()
    assert data["search"]["method"] == "vector"
    assert len(data["reviewed"]) == 1
    service.context_builder.vector_retriever.retrieve.assert_called_once()

    def stale(query, **kwargs):
        references = retrieve(query, **kwargs)
        with repo._connect() as connection:
            connection.execute(
                "UPDATE sources SET version = 'changed' WHERE id = ?", (identity.source_id,),
            )
        return references

    service.context_builder.vector_retriever.retrieve.side_effect = stale
    assert _search(stack).json()["search"]["method"] == "lexical"


def test_vector_failure_and_chinese_pending_relation(stack):
    repo, service, _, _, _, run, _ = stack
    first = _candidate(repo, "chinese", summary="量子纠错实验还缺少长期稳定性数据。")
    second = first.model_copy(update={"id": "second", "name": "稳定性评估"})
    repo.add_candidate_entity(second)
    relation = CandidateRelation(
        id="relation", ingestion_id=first.ingestion_id, topic_slug=first.topic_slug,
        source_candidate_id=first.id, target_candidate_id=second.id, type="USES",
        summary="量子纠错使用稳定性评估。", confidence=0.8, evidence=first.evidence,
    )
    repo.add_candidate_relation(relation)
    output = service.repository.get_output(run.id)
    structured = {**output.structured, "draft": {**output.structured["draft"],
                  "limitations": ["量子纠错稳定性"]}}
    digest = _sha256({"output_type": output.output_type, "structured": structured,
                      "rendered_text": output.rendered_text})
    with repo._connect() as connection:
        connection.execute(
            "UPDATE agent_run_outputs SET structured_json = ?, output_sha256 = ? WHERE run_id = ?",
            (json.dumps(structured), digest, run.id),
        )
    service.context_builder.vector_retriever.retrieve.side_effect = RuntimeError("offline")
    data = _search(stack).json()
    assert data["search"]["method"] == "lexical"
    assert any("不可用" in notice for notice in data["search"]["notices"])
    assert {p["candidate_id"] for p in data["pending"]} == {first.id, second.id, relation.id}
    card = next(p for p in data["pending"] if p["kind"] == "relation")
    assert card["title"] == f"{first.name} —USES→ {second.name}"


@pytest.mark.parametrize("mutation", ["scope", "review", "superseded"])
def test_external_retrieval_cannot_keep_revoked_authority(stack, mutation):
    repo, service, _, project, _, _, _ = stack
    candidate = _candidate(repo, "revoked")
    repo.publish_entity(candidate.id)

    def retrieve(*args, **kwargs):
        if mutation == "scope":
            current = repo.memory_repository.get_project(project.id)
            repo.memory_repository.replace_project_knowledge_scopes(
                project.id, [], expected_project_revision=current.revision,
            )
        else:
            with repo._connect() as connection:
                if mutation == "review":
                    connection.execute("UPDATE claims SET status = 'draft'")
                else:
                    source = connection.execute(
                        "SELECT source_id FROM documents WHERE id = ?",
                        (candidate.evidence.paper_id,),
                    ).fetchone()[0]
                    connection.execute(
                        "UPDATE sources SET superseded_by_source_id = ? WHERE id = ?",
                        (source, source),
                    )
        return [CandidateReference("vector", "chunk", candidate.evidence.chunk_id, 1)]

    service.context_builder.vector_retriever.retrieve.side_effect = retrieve
    response = _search(stack)
    if mutation == "scope":
        assert response.status_code == 409
    else:
        assert response.status_code == 200
        assert response.json()["reviewed"] == []
        assert response.json()["search"]["method"] == "lexical"


def test_pending_window_and_result_limits(stack, monkeypatch):
    import app.agent.gap_evidence as module

    repo = stack[0]
    # Reduce only the local window, then verify that the caller is told about
    # truncated candidates and never receives more than five reviewed results.
    monkeypatch.setattr(module, "PENDING_LIMIT", 6)
    for index in range(7):
        approved = _candidate(repo, f"approved-{index}")
        repo.publish_entity(approved.id)
        _candidate(repo, f"pending-{index}")
    data = _search(stack).json()
    assert len(data["reviewed"]) == len(data["pending"]) == 5
    assert any("最近 6 条" in notice for notice in data["search"]["notices"])


@pytest.mark.parametrize("change,code", [("new", 422), ("follow-up", 422), ("task", 409),
                                        ("project", 409), ("hash", 409), ("index", 409)])
def test_command_rejects_invalid_origin_before_inserting(stack, change, code):
    repo, _, _, _, _, _, client = stack
    payload = _command(stack)
    if change == "new":
        payload["task"] = {"kind": "new"}
    elif change == "follow-up":
        payload["follow_up_of"] = {"run_id": payload["gap_evidence_of"]["run_id"]}
    elif change == "task":
        payload["task"]["task_id"] = "other-task"
    elif change == "project":
        payload["project_id"] = "other-project"
    elif change == "hash":
        payload["gap_evidence_of"]["output_sha256"] = "stale"
    else:
        payload["gap_evidence_of"]["gap_index"] = 99
    response = client.post("/api/v1/research-commands", json=payload,
                           headers={"Idempotency-Key": "invalid-gap-key"})
    assert response.status_code == code, response.text
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_commands").fetchone()[0] == 0


def test_rerun_lineage_idempotency_new_snapshot_and_report(stack):
    repo, service, worker, _, task, parent, client = stack
    original = service.repository.get_output(parent.id).model_dump()
    original_package = service.load_context_snapshot(parent.id).model_dump()
    _candidate(repo, "pending")
    approved = _candidate(repo, "approved")
    repo.publish_entity(approved.id)
    payload = _command(stack)
    headers = {"Idempotency-Key": "gap-rerun-key"}
    response = client.post("/api/v1/research-commands", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    command_id = response.json()["id"]
    children = client.get(f"/api/v1/agent-runs/{parent.id}/gap-reruns").json()
    assert children == [{"gap_index": 0, "command_id": command_id, "run": None, "artifact": None}]
    assert worker.run_once()
    command = client.get(f"/api/v1/research-commands/{command_id}").json()
    assert command["status"] == "queued", command
    child = service.get_run(command["target_resource_id"])
    assert child.task_id == task.id
    assert child.context_snapshot_id != parent.context_snapshot_id
    package = service.load_context_snapshot(child.id)
    assert not any("pending" in b.claim.statement for b in package.knowledge.claim_bundles)
    assert any("approved" in b.claim.statement for b in package.knowledge.claim_bundles)
    replay = client.post("/api/v1/research-commands", json=payload, headers=headers)
    assert replay.json()["id"] == command_id
    events = service.repository.list_events(child.id)
    origins = [event for event in events if event.event_type == "gap_evidence_origin"]
    assert len(origins) == 1
    assert origins[0].output_summary == {
        "parent_run_id": parent.id, **_request(service, parent), "command_id": command_id,
        "claim_ids": payload["gap_evidence_of"]["claim_ids"],
    }
    bundle = package.knowledge.claim_bundles[0]
    service.llm.responses = [json.dumps(item) for item in (
        _analysis(), _plan(), _draft(bundle.claim.claim_id, bundle.evidence[0].evidence_id,
                                   valid=True, proposal=False),
    )]
    assert worker.run_once()
    assert service.get_run(child.id).status == "completed"
    children = client.get(f"/api/v1/agent-runs/{parent.id}/gap-reruns").json()
    assert len(children) == 1
    assert children[0]["run"]["id"] == child.id
    assert children[0]["artifact"]["metadata"]["agent_run_id"] == child.id
    assert service.repository.get_output(parent.id).model_dump() == original
    assert service.load_context_snapshot(parent.id).model_dump() == original_package
    # A replay is an observation of the existing command, even after scope removal.
    project = repo.memory_repository.get_project(parent.project_id)
    repo.memory_repository.replace_project_knowledge_scopes(
        project.id, [], expected_project_revision=project.revision,
    )
    replay = client.post("/api/v1/research-commands", json=payload, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["target_resource_id"] == child.id
    conflict = client.post("/api/v1/research-commands",
                           json={**payload, "instruction": "另一个研究问题"}, headers=headers)
    assert conflict.status_code == 409


def test_retry_replaces_stale_preparation_without_duplicate_run(stack, monkeypatch):
    repo, service, worker, _, _, parent, client = stack
    original_create = service.create_run
    created_snapshot = []

    def change_after_freeze(project_id, task_id, request, **kwargs):
        created_snapshot.append(request.context_snapshot_id)
        candidate = _candidate(repo, "late-evidence")
        repo.publish_entity(candidate.id)
        return original_create(project_id, task_id, request, **kwargs)

    monkeypatch.setattr(service, "create_run", change_after_freeze)
    response = client.post("/api/v1/research-commands", json=_command(stack),
                           headers={"Idempotency-Key": "retry-gap-key"})
    assert response.status_code == 202
    command_id = response.json()["id"]
    assert worker.run_once()
    command = client.get(f"/api/v1/research-commands/{command_id}").json()
    assert command["status"] == "failed", command
    assert command["target_resource_id"] is None
    frozen = service.context_builder.snapshot_repository.get(created_snapshot[0]).model_dump()
    monkeypatch.setattr(service, "create_run", original_create)
    assert client.post(f"/api/v1/research-commands/{command_id}/retry").status_code == 202
    assert worker.run_once()
    command = client.get(f"/api/v1/research-commands/{command_id}").json()
    assert command["status"] == "queued", command
    child = service.get_run(command["target_resource_id"])
    assert child.context_snapshot_id != created_snapshot[0]
    package = service.load_context_snapshot(child.id)
    assert any("late-evidence" in b.claim.statement for b in package.knowledge.claim_bundles)
    preserved = service.context_builder.snapshot_repository.get(created_snapshot[0])
    assert preserved.model_dump() == frozen
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 2
    children = client.get(f"/api/v1/agent-runs/{parent.id}/gap-reruns").json()
    assert len(children) == 1
    assert children[0]["run"]["id"] == child.id


@pytest.mark.parametrize("vector", [False, True])
def test_gap_query_excludes_goal_only_matches_and_snapshot_count(stack, monkeypatch, vector):
    repo, service, _, _, task, _, _ = stack
    goal_only = _candidate(repo, "orchard", summary="Orchard harvest measurements.")
    relevant = _candidate(repo, "photonic", summary="Photonic stability measurements.")
    repo.publish_entity(goal_only.id)
    repo.publish_entity(relevant.id)
    full = service.context_builder.preview_context(ContextBuildRequest(task_id=task.id))
    goal_bundle = next(b for b in full.knowledge.claim_bundles if "Orchard" in b.claim.statement)
    parent = service.gap_evidence.parent

    def scoped_parent(*args):
        run, package, _ = parent(*args)
        package = package.model_copy(update={
            "task": package.task.model_copy(update={"title": "Quasar", "goal": "Orchard " * 200}),
            "knowledge": package.knowledge.model_copy(update={"claim_bundles": [goal_bundle]}),
        })
        return run, package, "Photonic stability"

    monkeypatch.setattr(service.gap_evidence, "parent", scoped_parent)
    if vector:
        service.context_builder.vector_retriever.retrieve.return_value = [
            CandidateReference("vector", "chunk", relevant.evidence.chunk_id, 0.9),
        ]
    data = _search(stack).json()
    assert service.context_builder.vector_retriever.retrieve.call_args.args[0] == (
        "Photonic stability\nQuasar"
    )
    assert len(data["reviewed"]) == 1
    assert "Photonic" in data["reviewed"][0]["statement"]
    assert data["reviewed_in_snapshot_count"] == 0
    assert data["search"]["method"] == ("vector" if vector else "lexical")


@pytest.mark.parametrize("claim_ids", [None, [], [""], ["a", "b", "c", "d", "e", "f"], [{}]])
def test_gap_command_requires_one_to_five_nonempty_claim_ids(stack, claim_ids):
    payload = _command(stack)
    if claim_ids is None:
        payload["gap_evidence_of"].pop("claim_ids")
    else:
        payload["gap_evidence_of"]["claim_ids"] = claim_ids
    response = stack[-1].post("/api/v1/research-commands", json=payload,
                              headers={"Idempotency-Key": "invalid-pins"})
    assert response.status_code == 422, response.text


def test_gap_command_deduplicates_before_idempotency(stack):
    payload = _command(stack)
    headers = {"Idempotency-Key": "deduplicated-pins"}
    first = stack[-1].post("/api/v1/research-commands", json=payload, headers=headers)
    payload["gap_evidence_of"]["claim_ids"] *= 2
    second = stack[-1].post("/api/v1/research-commands", json=payload, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


def test_gap_rerun_pins_evidence_previously_dropped_for_budget(stack):
    repo, service, worker, _, task, parent, client = stack
    candidate = _candidate(repo, "budget-evidence")
    repo.publish_entity(candidate.id)
    builder = service.context_builder
    full = builder.preview_context(ContextBuildRequest(task_id=task.id, max_tokens=16000))
    target = full.knowledge.claim_bundles[-1].claim.claim_id
    budget = full.token_usage.used - 100
    request = ContextBuildRequest(task_id=task.id, max_tokens=budget)
    frozen = builder.build_context(request)
    assert frozen.knowledge.claim_bundles
    assert target not in {b.claim.claim_id for b in frozen.knowledge.claim_bundles}
    # Model a completed report whose frozen input omitted already-published evidence.
    with repo._connect() as connection:
        connection.execute("""UPDATE agent_runs SET context_snapshot_id = ?, context_sha256 = ?
            WHERE id = ?""", (frozen.snapshot_id, frozen.package_sha256, parent.id))
    ordinary = builder.build_research_context(request, profile="classic")
    assert target not in {b.claim.claim_id for b in ordinary.knowledge.claim_bundles}
    assert target in {b["claim_id"] for b in _search(stack).json()["reviewed"]}
    payload = _command(stack)
    payload["token_budget"] = budget
    payload["gap_evidence_of"]["claim_ids"] = [target]
    response = client.post("/api/v1/research-commands", json=payload,
                           headers={"Idempotency-Key": "budget-rerun"})
    assert response.status_code == 202, response.text
    assert worker.run_once()
    command = client.get(f"/api/v1/research-commands/{response.json()['id']}").json()
    assert command["status"] == "queued", command
    package = service.load_context_snapshot(command["target_resource_id"])
    assert package.knowledge.claim_bundles[0].claim.claim_id == target
    assert package.retrieval_audit.parameters["pinned_claims"] == {
        "requested": [target], "verified": [target], "selected": [target], "ignored": [],
    }
    assert builder.snapshot_repository.get(frozen.snapshot_id) == frozen


@pytest.mark.parametrize("kind", ["outside", "new-scope", "superseded", "draft"])
@pytest.mark.parametrize("partial", [False, True])
def test_gap_command_rejects_or_audits_invalid_pins(stack, kind, partial):
    repo, service, worker, project, _, _, client = stack
    payload = _command(stack)
    valid = payload["gap_evidence_of"]["claim_ids"][0]
    scope = repo.create_collection("Outside")
    candidate = _candidate(repo, "invalid-pin", collection=(
        scope.slug if kind in {"outside", "new-scope"} else "research-scope"
    ))
    invalid = candidate.id
    if kind != "draft":
        entity = repo.publish_entity(candidate.id)
        invalid = repo.core_repository.get_claim_by_legacy_id(
            f"published_entity_definition:{entity.id}",
        ).id
    if kind == "new-scope":
        project = repo.memory_repository.get_project(project.id)
        repo.memory_repository.replace_project_knowledge_scopes(
            project.id, ["research-scope", scope.slug], expected_project_revision=project.revision,
        )
    elif kind == "superseded":
        with repo._connect() as connection:
            connection.execute("""UPDATE sources SET superseded_by_source_id = id
                WHERE id = (SELECT source_id FROM documents WHERE id = ?)""",
                               (candidate.evidence.paper_id,))
    payload["gap_evidence_of"]["claim_ids"] = [invalid, valid] if partial else [invalid]
    response = client.post("/api/v1/research-commands", json=payload,
                           headers={"Idempotency-Key": "invalid-formal-pin"})
    if not partial:
        assert response.status_code == 409, response.text
        assert "所选证据均已失效" in response.text
        return
    assert response.status_code == 202, response.text
    assert worker.run_once()
    command = client.get(f"/api/v1/research-commands/{response.json()['id']}").json()
    assert command["status"] == "queued", command
    package = service.load_context_snapshot(command["target_resource_id"])
    assert invalid not in {b.claim.claim_id for b in package.knowledge.claim_bundles}
    assert package.retrieval_audit.parameters["pinned_claims"] == {
        "requested": [invalid, valid], "verified": [valid], "selected": [valid],
        "ignored": [{"claim_id": invalid, "reason": "not_formal_in_parent_visible_scope"}],
    }


@pytest.mark.parametrize("partial", [False, True])
def test_gap_command_revalidates_pins_at_execution(stack, partial):
    repo, service, worker, _, _, _, client = stack
    payload = _command(stack)
    candidate = _candidate(repo, "revoked-after-submit")
    entity = repo.publish_entity(candidate.id)
    revoked = repo.core_repository.get_claim_by_legacy_id(
        f"published_entity_definition:{entity.id}",
    ).id
    payload["gap_evidence_of"]["claim_ids"] = (
        [revoked, *payload["gap_evidence_of"]["claim_ids"]] if partial else [revoked]
    )
    response = client.post("/api/v1/research-commands", json=payload,
                           headers={"Idempotency-Key": "execution-revalidation"})
    assert response.status_code == 202, response.text
    with repo._connect() as connection:
        connection.execute("UPDATE claims SET status = 'draft' WHERE id = ?", (revoked,))
    assert worker.run_once()
    command = client.get(f"/api/v1/research-commands/{response.json()['id']}").json()
    if not partial:
        assert command["status"] == "failed", command
        assert "所选证据均已失效" in command["error"]
        assert command["snapshot_id"] is None
        assert command["target_resource_id"] is None
    else:
        assert command["status"] == "queued", command
        package = service.load_context_snapshot(command["target_resource_id"])
        assert revoked not in {b.claim.claim_id for b in package.knowledge.claim_bundles}
        assert package.retrieval_audit.parameters["pinned_claims"]["ignored"] == [
            {"claim_id": revoked, "reason": "not_formal_in_parent_visible_scope"},
        ]
