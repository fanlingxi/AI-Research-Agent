from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.agent.models import AgentRunCreateRequest
from app.agent.service import AgentRunService
from app.context.models import ContextBuildRequest, canonical_package_sha256
from app.context.neighbors import adjacent_bundles
from app.context.ranking import rank_bundles
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan
from app.schemas.documents import DocumentChunk
from tests.test_query_planning import ScriptedLLM


def _setup(tmp_path, denied_first=False, parts=None, publish_last=False):
    repo = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    scope, denied = repo.create_collection("Allowed"), repo.create_collection("Denied")
    ingestion = repo.create_ingestion(collection=scope.slug, sources=["paper.pdf"],
                                      pdf_max_pages=1, enqueue=False)
    repo.update_ingestion(ingestion.id, status="needs_review")
    parts = parts or ["Left supporting information. " * 5, "Needle method definition. " * 5,
                      "Right supporting information. " * 5,
                      "Unreviewed following information. " * 5]
    repo.add_document(ingestion_id=ingestion.id, document_id="paper", title="Paper",
                      source="pdf", source_url=None, local_path="paper.pdf", pages=1, metadata={})
    repo.core_repository.record_source_document(
        document_id="paper", title="Paper", uri="paper.pdf", content="".join(parts),
        parser_version="fixture-v1", metadata={"source_version": "v1"})
    offset, claims = 0, []
    for index, part in enumerate(parts):
        chunk_id = f"piece-{index}"
        repo.core_repository.upsert_chunks([DocumentChunk(
            id=chunk_id, paper_id="paper", title="Paper", text=part, chunk_index=index,
            token_count=50, source_tier="primary_fulltext",
            metadata={"page_start": 1, "page_end": 1, "source_start": offset,
                      "source_end": offset + len(part)})])
        offset += len(part)
        if index == len(parts) - 1 and not publish_last:
            continue
        collection = denied if index == 0 and denied_first else scope
        candidate = CandidateEntity(
            id=f"candidate-{index}", ingestion_id=ingestion.id, topic_slug=collection.slug,
            name=f"Excerpt {index}", type="Paper", summary=part, confidence=1,
            evidence=EvidenceSpan(paper_id="paper", chunk_id=chunk_id, quote=part,
                                  page_start=1, page_end=1))
        repo.add_candidate_entity(candidate)
        published = repo.publish_entity(candidate.id, review_note="test literal evidence")
        claims.append(repo.core_repository.get_claim_by_legacy_id(
            f"published_entity_definition:{published.id}").id)
    memory = repo.memory_repository
    project = memory.create_project(name="Research", goal="Needle", domain="research", metadata={})
    memory.replace_project_knowledge_scopes(project.id, [scope.slug],
                                           expected_project_revision=project.revision)
    task = memory.create_workspace_task(project_id=project.id, title="Needle", goal="Needle",
                                        priority="high", metadata={})
    return repo, task, claims


def _request(task, **kwargs):
    return ContextBuildRequest(task_id=task.id, max_tokens=16000, query_planning="task-v1",
                               context_neighbors="adjacent-v1", **kwargs)


def _chunks(package):
    return {c.chunk_id for b in package.knowledge.claim_bundles for c in b.chunks}


def test_neighbors_add_only_reviewed_touching_bundles_and_preserve_ids(tmp_path):
    repo, task, claims = _setup(tmp_path)
    builder = ContextBuilderService(repo)
    base = builder.build_context(_request(task).model_copy(update={"context_neighbors": "off"}))
    assert _chunks(base) == {"piece-1"}
    package = builder.build_context(_request(task))
    assert _chunks(package) == {"piece-0", "piece-1", "piece-2"}
    assert {b.claim.claim_id for b in package.knowledge.claim_bundles} == set(claims)
    groups = package.retrieval_audit.parameters["context_neighbors"]["groups"]
    assert len(groups) == 1 and groups[0]["selected"]
    assert set(groups[0]["claim_ids"]) == set(claims)
    assert canonical_package_sha256(builder.snapshot_repository.get(base.snapshot_id)) == (
        base.package_sha256)
    assert package.token_usage.used <= 16000


def test_budget_never_keeps_half_of_a_new_neighbor_group(tmp_path):
    repo, task, _ = _setup(tmp_path)
    builder = ContextBuilderService(repo)
    full = builder.preview_context(_request(task))
    small = builder.build_context(_request(task).model_copy(
        update={"max_tokens": full.token_usage.used - 64}))
    assert not small.knowledge.claim_bundles
    assert small.token_usage.used <= small.token_usage.budget
    assert small.diagnostics.dropped_for_budget == 3
    assert not small.retrieval_audit.parameters["context_neighbors"]["groups"][0]["selected"]


def test_neighbor_cannot_cross_membership_even_in_same_document(tmp_path):
    repo, task, _ = _setup(tmp_path, denied_first=True)
    package = ContextBuilderService(repo).build_context(_request(task))
    assert _chunks(package) == {"piece-1", "piece-2"}


@pytest.mark.parametrize("location", [
    {}, {"source_start": True, "source_end": 270},
    {"source_start": 0, "source_end": 10},
    {"source_start": -1, "source_end": 100},
    {"source_start": "140", "source_end": 265},
])
def test_missing_or_forged_offsets_disable_expansion_not_existing_evidence(tmp_path, location):
    repo, task, _ = _setup(tmp_path)
    with repo.database.connect() as db:
        db.execute("UPDATE chunks SET metadata_json = ? WHERE id = 'piece-1'",
                   (json.dumps({"page_start": 1, "page_end": 1, **location}),))
    package = ContextBuilderService(repo).build_context(_request(task))
    assert _chunks(package) == {"piece-1"}


def test_version_parser_gap_and_multichunk_are_not_adjacency(tmp_path):
    repo, task, _ = _setup(tmp_path)
    builder = ContextBuilderService(repo)
    with repo.database.connect() as db:
        raw = builder._read_inputs_tx(db, _request(task)).bundles
    for mutation in ("version", "parser", "gap", "multi_chunk"):
        bundles = deepcopy(raw)
        for bundle in bundles:
            item = bundle.evidence[0]
            if item.chunk["id"] != "piece-1":
                if mutation == "version":
                    item.source["version"] = "different"
                elif mutation == "parser":
                    item.document["parser_version"] = "different"
                elif mutation == "gap":
                    item.source_span = (item.source_span[0] + 1, item.source_span[1] - 1)
                else:
                    extra = deepcopy(item)
                    extra.chunk["id"] = "different-chunk"
                    bundle.evidence.append(extra)
        ranked = rank_bundles(bundles, query="Needle", candidate_references=[], strategy="bm25-v1")
        result, _ = adjacent_bundles(bundles, ranked)
        assert [r.bundle.evidence[0].chunk["id"] for r in result] == ["piece-1"]


def test_withdrawal_during_assembly_revalidates_group_and_keeps_old_snapshot(tmp_path):
    repo, task, claims = _setup(tmp_path)
    builder = ContextBuilderService(repo)
    old = builder.build_context(_request(task))
    assemble = builder._assemble_package
    calls = []

    def mutate(*args, **kwargs):
        package = assemble(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            with repo.database.connect() as db:
                db.execute("UPDATE claims SET status = 'withdrawn' WHERE id = ?", (claims[0],))
        return package

    builder._assemble_package = mutate
    new = builder.build_context(_request(task))
    assert len(calls) == 2 and _chunks(new) == {"piece-1", "piece-2"}
    assert _chunks(builder.snapshot_repository.get(old.snapshot_id)) == {
        "piece-0", "piece-1", "piece-2"}


def test_agent_request_forwards_neighbors_and_rejects_expanding_old_snapshot(tmp_path):
    repo, task, _ = _setup(tmp_path)
    llm = ScriptedLLM(RuntimeError("task ablation must not call"))
    service = AgentRunService(repo, llm=llm)
    run = service.create_run(task.project_id, task.id, AgentRunCreateRequest(
        token_budget=16000, query_planning="task-v1", context_neighbors="adjacent-v1"))
    assert _chunks(service.context_builder.snapshot_repository.get(run.context_snapshot_id)) == {
        "piece-0", "piece-1", "piece-2"}
    assert not llm.calls
    with pytest.raises(ValidationError, match="cannot expand"):
        AgentRunCreateRequest(context_snapshot_id=run.context_snapshot_id,
                              context_neighbors="adjacent-v1")


def test_neighbors_do_not_recurse_into_more_reviewed_chunks(tmp_path):
    repo, task, _ = _setup(tmp_path, publish_last=True)
    package = ContextBuilderService(repo).build_context(_request(task))
    assert _chunks(package) == {"piece-0", "piece-1", "piece-2"}


def test_overlapping_groups_deduplicate_and_keep_prior_context_after_budget_drop(tmp_path):
    parts = ["Left " * 20, "Needle " * 20, "Middle " * 20,
             "Needle additional " * 20, "Right " * 20]
    repo, task, _ = _setup(tmp_path, parts=parts, publish_last=True)
    builder = ContextBuilderService(repo)
    full = builder.preview_context(_request(task))
    assert len(full.knowledge.claim_bundles) == 5
    groups = full.retrieval_audit.parameters["context_neighbors"]["groups"]
    assert len(groups) == 2
    assert len(groups[1]["claim_ids"]) == 2 and len(groups[1]["context_claim_ids"]) == 3
    small = builder.build_context(_request(task).model_copy(
        update={"max_tokens": full.token_usage.used - 64}))
    assert {b.claim.claim_id for b in small.knowledge.claim_bundles} == set(groups[0]["claim_ids"])


def test_query_replay_rejects_changed_question_and_does_not_rebill_historical_usage():
    from app.benchmarking.context_neighbors import ReplayPlanner
    from app.retrieval.query_planning import task_plan

    plan = task_plan("original", status="planned")
    plan.model_calls, plan.usage = 1, {"input_tokens": 12, "output_tokens": 8}
    replay = ReplayPlanner(plan)
    current = replay.plan("original")
    assert current.model_calls == 0 and current.usage is None
    assert plan.model_calls == 1 and plan.usage["input_tokens"] == 12
    with pytest.raises(ValueError, match="does not match"):
        replay.plan("changed")
