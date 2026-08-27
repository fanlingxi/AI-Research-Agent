from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateDecision, CandidateEntity, EvidenceSpan
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from tests.core_fixtures import persist_evidence_chunk


def _stack(tmp_path):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    service = KnowledgeIngestionService(
        repository,
        settings=settings,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(settings.knowledge_vault_path),
        require_live_llm=False,
    )
    return repository, service


def _candidate(ingestion, candidate_id: str, name: str) -> CandidateEntity:
    return CandidateEntity(
        id=candidate_id,
        ingestion_id=ingestion.id,
        topic_slug=ingestion.collection_slug,
        name=name,
        type="Concept",
        summary=f"{name} 在当前论文中的可审核研究语境说明。",
        paper_context="该词在本文中用于描述一个受限研究语境。",
        role="帮助解释论文的研究对象。",
        conditions="不推断到其他研究领域。",
        confidence=0.9,
        evidence=EvidenceSpan(
            paper_id=f"paper:{ingestion.id}",
            chunk_id=f"paper:{ingestion.id}:page:1:chunk:0",
            page_start=1,
            page_end=1,
            quote="This text provides source-scoped evidence for a research term.",
        ),
    )


def _persist_candidate_evidence(repository, ingestion) -> None:
    persist_evidence_chunk(
        repository,
        ingestion_id=ingestion.id,
        evidence=_candidate(ingestion, "fixture", "Fixture").evidence,
    )


def test_blank_collection_uses_inbox_and_topic_alias_is_compatible(tmp_path) -> None:
    repository, service = _stack(tmp_path)
    app = create_app(knowledge_repository=repository, knowledge_service=service)
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge/ingestions",
            json={"sources": ["paper.pdf"], "pdf_max_pages": 2},
        )
        collections = client.get("/api/knowledge/collections")
        collection = client.get("/api/knowledge/collections/inbox")
        conflict = client.post(
            "/api/knowledge/ingestions",
            json={
                "topic": "旧主题",
                "collection": "新集合",
                "sources": ["paper.pdf"],
                "pdf_max_pages": 2,
            },
        )

    assert response.status_code == 202
    ingestion = response.json()
    assert ingestion["collection"] == "收件箱"
    assert ingestion["collection_slug"] == "inbox"
    assert ingestion["topic"] == "收件箱"
    assert any(item["slug"] == "inbox" and item["is_system"] for item in collections.json())
    assert collection.json()["collection"]["name"] == "收件箱"
    assert conflict.status_code == 422


def test_move_collection_reassigns_membership_and_queues_durable_sync(tmp_path) -> None:
    repository, service = _stack(tmp_path)
    ingestion = repository.create_ingestion(
        collection="初始集合", sources=["paper.pdf"], pdf_max_pages=2, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    _persist_candidate_evidence(repository, ingestion)
    repository.add_candidate_entity(_candidate(ingestion, "candidate-move", "可移动概念"))
    entity = service.decide("candidate-move", CandidateDecision(decision="approve"))
    service.drain_projections()

    moved = repository.move_ingestion_collection(ingestion.id, "目标集合")

    assert moved.collection == "目标集合"
    assert repository.list_published_entities("初始集合") == []
    assert [item.id for item in repository.list_published_entities("目标集合")] == [
        entity.candidate["candidate"]["canonical_id"]
    ]
    sync = repository.claim_job()
    assert sync is not None
    assert sync.kind == "collection_sync"
    assert set(sync.payload["collection_slugs"]) == {"初始集合", "目标集合"}


def test_move_collection_authorizes_new_retrieval_scope_after_v9_backfill(tmp_path) -> None:
    repository, service = _stack(tmp_path)
    ingestion = repository.create_ingestion(
        collection="旧检索集合",
        sources=["paper.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    paper = _candidate(ingestion, "candidate-paper-move", "可移动论文").model_copy(
        update={"type": "Paper"}
    )
    persist_evidence_chunk(
        repository,
        ingestion_id=ingestion.id,
        evidence=paper.evidence,
        title=paper.name,
    )
    repository.add_candidate_entity(paper)
    service.decide(paper.id, CandidateDecision(decision="approve"))
    service.drain_projections()
    repository.core_repository.backfill_legacy_documents(
        [
            {
                "id": paper.evidence.chunk_id,
                "paper_id": paper.evidence.paper_id,
                "title": paper.name,
                "text": paper.evidence.quote,
                "chunk_index": 0,
                "token_count": len(paper.evidence.quote.split()),
                "source_tier": "primary_fulltext",
                "metadata": {"page_start": 1, "page_end": 1},
            }
        ]
    )

    assert repository.core_repository.is_v0009_backfill_ready()
    assert repository.published_paper_ids([ingestion.collection_slug]) == {paper.evidence.paper_id}
    before_shadow = repository.knowledge_core_shadow_read([ingestion.collection_slug])
    assert before_shadow["cutover_ready"]
    assert before_shadow["legacy_only"] == before_shadow["core_only"] == []

    moved = repository.move_ingestion_collection(ingestion.id, "新检索集合")

    assert repository.published_paper_ids([ingestion.collection_slug]) == set()
    assert repository.published_paper_ids([moved.collection_slug]) == {paper.evidence.paper_id}
    old_shadow = repository.knowledge_core_shadow_read([ingestion.collection_slug])
    new_shadow = repository.knowledge_core_shadow_read([moved.collection_slug])
    assert old_shadow["authorized_core_paper_ids"] == []
    assert new_shadow["authorized_core_paper_ids"] == [paper.evidence.paper_id]


def test_defer_keeps_source_mention_out_of_formal_graph(tmp_path) -> None:
    repository, service = _stack(tmp_path)
    ingestion = service.submit(sources=["paper.pdf"], pdf_max_pages=2)
    repository.update_ingestion(ingestion.id, status="needs_review")
    _persist_candidate_evidence(repository, ingestion)
    paper = _candidate(ingestion, "candidate-paper", "Evidence Paper").model_copy(
        update={"type": "Paper"}
    )
    repository.add_candidate_entity(paper)
    repository.add_candidate_entity(_candidate(ingestion, "candidate-defer", "待定术语"))
    service.decide("candidate-paper", CandidateDecision(decision="approve"))

    result = service.decide(
        "candidate-defer", CandidateDecision(decision="defer", review_note="需要领域专家确认")
    )

    assert result.projection_status == "not_required"
    assert repository.get_candidate("candidate-defer")["candidate"]["status"] == "deferred"
    assert [item.name for item in repository.list_published_entities()] == ["Evidence Paper"]
    assert paper.evidence.paper_id in repository.published_paper_ids()
    mention = next(
        item for item in repository.list_source_mentions() if item.candidate_id == "candidate-defer"
    )
    assert mention.status == "source_only"
    assert mention.paper_context
    service.drain_projections()
    assert repository.get_ingestion(ingestion.id).status == "completed"


def test_same_name_can_be_published_as_distinct_senses_only_by_explicit_choice(tmp_path) -> None:
    repository, service = _stack(tmp_path)
    first_ingestion = service.submit(collection="领域甲", sources=["one.pdf"], pdf_max_pages=2)
    second_ingestion = service.submit(collection="领域乙", sources=["two.pdf"], pdf_max_pages=2)
    repository.update_ingestion(first_ingestion.id, status="needs_review")
    repository.update_ingestion(second_ingestion.id, status="needs_review")
    _persist_candidate_evidence(repository, first_ingestion)
    _persist_candidate_evidence(repository, second_ingestion)
    repository.add_candidate_entity(_candidate(first_ingestion, "candidate-sense-a", "Alignment"))
    repository.add_candidate_entity(_candidate(second_ingestion, "candidate-sense-b", "Alignment"))

    first = service.decide("candidate-sense-a", CandidateDecision(decision="approve"))
    second = service.decide("candidate-sense-b", CandidateDecision(decision="approve"))
    linked_ingestion = repository.create_ingestion(
        collection="领域甲", sources=["three.pdf"], pdf_max_pages=2, enqueue=False
    )
    repository.update_ingestion(linked_ingestion.id, status="needs_review")
    _persist_candidate_evidence(repository, linked_ingestion)
    repository.add_candidate_entity(
        _candidate(linked_ingestion, "candidate-sense-link", "Alignment")
    )
    linked = service.decide(
        "candidate-sense-link",
        CandidateDecision(
            decision="link", canonical_id=first.candidate["candidate"]["canonical_id"]
        ),
    )

    assert (
        first.candidate["candidate"]["canonical_id"]
        != second.candidate["candidate"]["canonical_id"]
    )
    assert len(repository.list_published_entities()) == 2
    assert (
        repository.get_concept_sense(first.candidate["candidate"]["canonical_id"]).status
        == "published"
    )
    assert (
        repository.get_concept_sense(second.candidate["candidate"]["canonical_id"]).status
        == "published"
    )
    assert linked.candidate["candidate"]["status"] == "merged"
    assert len(repository.list_published_entities()) == 2
    assert len(repository.list_source_mentions(first.candidate["candidate"]["canonical_id"])) == 2
