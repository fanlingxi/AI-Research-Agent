from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.context.knowledge_reader import KnowledgeContextReader
from app.knowledge.candidate_repository import DecisionAlreadyApplied
from app.knowledge.core_repository import ClaimEvidenceValidationError
from app.knowledge.map_view import KnowledgeMapReader
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, CandidateRelation, EvidenceSpan
from tests.core_fixtures import persist_evidence_chunk


def seed(repo, suffix, canonical=None):
    ingestion = repo.create_ingestion(
        collection=suffix, sources=[suffix + ".pdf"], pdf_max_pages=1, enqueue=False
    )
    evidence = EvidenceSpan(
        paper_id=suffix,
        chunk_id=suffix + "-chunk",
        page_start=1,
        page_end=1,
        quote="This method uses a tool. " + suffix,
    )
    persist_evidence_chunk(repo, ingestion_id=ingestion.id, evidence=evidence)
    endpoints = []
    for i in range(2):
        candidate = CandidateEntity(
            id=f"{suffix}-{i}",
            ingestion_id=ingestion.id,
            topic_slug=ingestion.collection_slug,
            name=f"Entity {i}",
            type="Method",
            summary="A reviewed method.",
            confidence=0.8,
            evidence=evidence,
        )
        repo.add_candidate_entity(candidate)
        entity = repo.publish_entity(candidate.id, canonical_id=canonical[i] if canonical else None)
        endpoints.append(entity.id)
    relation = CandidateRelation(
        id=suffix + "-relation",
        ingestion_id=ingestion.id,
        topic_slug=ingestion.collection_slug,
        source_candidate_id=f"{suffix}-0",
        target_candidate_id=f"{suffix}-1",
        type="USES",
        summary="The method uses the tool.",
        confidence=0.8,
        evidence=evidence,
    )
    repo.add_candidate_relation(relation)
    return relation, endpoints


@pytest.fixture
def prepared(tmp_path):
    repo = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    first, endpoints = seed(repo, "alpha")
    second, _ = seed(repo, "beta", endpoints)
    return repo, first, second


def test_publication_aggregates_claim_evidence_and_audit(prepared):
    repo, first, second = prepared
    one = repo.publish_relation(first.id)
    two = repo.publish_relation(second.id)
    assert one.id == two.id
    assert len(two.evidence) == 2
    assert set(two.collection_slugs) == {"alpha", "beta"}
    assert repo.get_review_decision(second.id)["canonical_id"] == one.id
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM claims WHERE claim_type='relation_assertion'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM claim_evidence_links l "
                "JOIN claims c ON c.id=l.claim_id WHERE c.claim_type='relation_assertion'"
            ).fetchone()[0]
            == 2
        )
        assert {
            r[0]
            for r in db.execute(
                "SELECT aggregate_id FROM projection_outbox WHERE aggregate_type='relation'"
            )
        } == {one.id}
    with pytest.raises(DecisionAlreadyApplied):
        repo.publish_relation(first.id)
    assert len(repo.get_published_relation(one.id).evidence) == 2


def test_duplicate_evidence_and_summary_confidence(prepared):
    repo, first, _ = prepared
    one = repo.publish_relation(first.id)
    duplicate = first.model_copy(
        update={"id": "duplicate", "summary": "New summary", "confidence": 0.95}
    )
    repo.add_candidate_relation(duplicate)
    two = repo.publish_relation(duplicate.id)
    assert two.id == one.id and len(two.evidence) == 1
    assert two.summary == one.summary and two.confidence == 0.95


@pytest.mark.parametrize(
    "changes",
    [{"type": "SUPPORTS"}, {"source_candidate_id": "alpha-1", "target_candidate_id": "alpha-0"}],
)
def test_direction_and_type_remain_distinct(prepared, changes):
    repo, first, _ = prepared
    one = repo.publish_relation(first.id)
    other = first.model_copy(update={"id": "different", **changes})
    repo.add_candidate_relation(other)
    assert repo.publish_relation(other.id).id != one.id
    assert len(repo.list_published_relations()) == 2


def test_concurrent_publication(prepared):
    repo, first, second = prepared
    barrier = Barrier(2)

    def publish(candidate):
        barrier.wait(timeout=10)
        return repo.publish_relation(candidate.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(publish, [first, second]))
    assert result[0].id == result[1].id
    assert len(repo.list_published_relations()) == 1
    assert len(repo.list_published_relations()[0].evidence) == 2


def test_scope_filters_each_evidence(prepared):
    repo, first, second = prepared
    repo.publish_relation(first.id)
    repo.publish_relation(second.id)
    with repo._connect() as db:
        bundles = KnowledgeContextReader().read_formal_bundles_tx(db, ["alpha"])
    relations = [b for b in bundles if b.relation]
    assert len(relations) == 1
    assert [e.document["id"] for e in relations[0].evidence] == ["alpha"]
    assert [e.paper_id for e in repo.list_published_relations("alpha")[0].evidence] == ["alpha"]
    graph = KnowledgeMapReader(repo.database).graph(collection="alpha")
    assert [e.weight for e in graph.edges if e.kind == "relation"] == [1]
    with repo._connect() as db:
        db.execute("UPDATE claims SET status='draft' WHERE claim_type='relation_assertion'")
        bundles = KnowledgeContextReader().read_formal_bundles_tx(db, ["alpha"])
    assert not [b for b in bundles if b.relation]


def test_invalid_append_rolls_back(prepared):
    repo, first, second = prepared
    one = repo.publish_relation(first.id)
    with repo._connect() as db:
        db.execute(
            "UPDATE chunks SET content = 'invalid' WHERE legacy_chunk_id = ? OR id = ?",
            (second.evidence.chunk_id, second.evidence.chunk_id),
        )
    with pytest.raises(ClaimEvidenceValidationError):
        repo.publish_relation(second.id)
    assert len(repo.get_published_relation(one.id).evidence) == 1
    assert repo.get_candidate(second.id)["candidate"]["status"] == "draft"


def test_historical_duplicates_are_merged_without_writes(prepared):
    repo, first, second = prepared
    one = repo.publish_relation(first.id)
    historical = one.model_copy(
        update={"id": "historical", "evidence": [first.evidence, second.evidence]}
    )
    with repo._connect() as db:
        db.execute(
            "INSERT INTO published_relations SELECT ?, source_entity_id, target_entity_id, "
            "relation_type, ?, created_at, updated_at FROM published_relations WHERE id=?",
            (historical.id, historical.model_dump_json(), one.id),
        )
        repo.core_repository.synchronize_published_relation_tx(db, historical)
    assert len(repo.list_published_relations()) == 1
    assert len(repo.get_published_relation(historical.id).evidence) == 2
    reader = KnowledgeMapReader(repo.database)
    assert [e.weight for e in reader.graph().edges if e.kind == "relation"] == [2]
    detail = reader.node_detail(one.source_entity_id)
    assert len(detail["relations"]) == 1
    assert len(detail["relations"][0]["evidence"]) == 2
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM published_relations").fetchone()[0] == 2
