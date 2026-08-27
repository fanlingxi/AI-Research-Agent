from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.config.settings import Settings
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.rebuild import KnowledgeProjectionRebuildService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateDecision, CandidateEntity, EvidenceSpan
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.schemas.documents import DocumentChunk
from tests.core_fixtures import persist_evidence_chunk


@dataclass
class _Indexer:
    calls: list[tuple[list[DocumentChunk], str | None]] = field(default_factory=list)

    def replace(
        self,
        chunks: list[DocumentChunk],
        *,
        collection_slug: str | None = None,
    ) -> None:
        self.calls.append((chunks, collection_slug))


@dataclass
class _Projector:
    calls: list[tuple[list, list]] = field(default_factory=list)

    def replace_all(self, entities: list, relations: list) -> None:
        self.calls.append((entities, relations))


@dataclass
class _Vault:
    calls: list[dict] = field(default_factory=list)

    def render_topic(self, **kwargs):
        self.calls.append(kwargs)
        return object()


def _published_paper(
    repository: KnowledgeRepository,
    service: KnowledgeIngestionService,
    *,
    collection: str,
    suffix: str,
) -> tuple[str, str]:
    ingestion = repository.create_ingestion(
        collection=collection,
        sources=[f"paper-{suffix}.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id=f"paper:{suffix}",
        chunk_id=f"paper:{suffix}:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote=f"Grounded source text for published paper {suffix}.",
    )
    persist_evidence_chunk(
        repository,
        ingestion_id=ingestion.id,
        evidence=evidence,
        title=f"Published Paper {suffix}",
    )
    candidate = CandidateEntity(
        id=f"candidate-{suffix}",
        ingestion_id=ingestion.id,
        topic_slug=ingestion.collection_slug,
        name=f"Published Paper {suffix}",
        type="Paper",
        summary=f"用于验证 {collection} 投影重建范围的正式论文。",
        confidence=0.95,
        evidence=evidence,
    )
    repository.add_candidate_entity(candidate)
    service.decide(candidate.id, CandidateDecision(decision="approve"))
    return ingestion.collection_slug, evidence.paper_id


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
    indexer = _Indexer()
    projector = _Projector()
    vault = _Vault()
    rebuild = KnowledgeProjectionRebuildService(
        repository,
        indexer=indexer,
        projector=projector,
        vault_exporter=vault,
    )
    return repository, service, rebuild, indexer, projector, vault


def test_collection_rebuild_scopes_vectors_and_preserves_complete_graph(tmp_path) -> None:
    repository, service, rebuild, indexer, projector, vault = _stack(tmp_path)
    first_slug, first_paper = _published_paper(
        repository,
        service,
        collection="第一集合",
        suffix="first",
    )
    _, second_paper = _published_paper(
        repository,
        service,
        collection="第二集合",
        suffix="second",
    )

    result = rebuild.rebuild(target="all", collection_slug=first_slug)

    chunks, scope = indexer.calls[0]
    assert scope == first_slug
    assert {chunk.paper_id for chunk in chunks} == {first_paper}
    assert all(chunk.metadata["collection_slugs"] == [first_slug] for chunk in chunks)
    graph_entities, _ = projector.calls[0]
    graph_papers = {
        evidence.paper_id
        for entity in graph_entities
        if entity.type == "Paper"
        for evidence in entity.evidence
    }
    assert graph_papers == {first_paper, second_paper}
    assert [call["topic_slug"] for call in vault.calls] == [first_slug]
    assert result.collection_slug == first_slug
    assert result.chunks == 1
    assert result.entities == 1
    assert result.topics == 1


def test_full_rebuild_projects_every_collection_from_sqlite(tmp_path) -> None:
    repository, service, rebuild, indexer, projector, vault = _stack(tmp_path)
    first_slug, first_paper = _published_paper(
        repository,
        service,
        collection="全量集合一",
        suffix="all-first",
    )
    second_slug, second_paper = _published_paper(
        repository,
        service,
        collection="全量集合二",
        suffix="all-second",
    )

    result = rebuild.rebuild(target="all")

    chunks, scope = indexer.calls[0]
    assert scope is None
    assert {chunk.paper_id for chunk in chunks} == {first_paper, second_paper}
    assert len(projector.calls) == 1
    assert {call["topic_slug"] for call in vault.calls} >= {first_slug, second_slug}
    assert result.chunks == 2
    assert result.entities == 2
    assert result.topics == len(repository.list_collections())


def test_rebuild_rejects_unknown_collection_without_external_writes(tmp_path) -> None:
    _, _, rebuild, indexer, projector, vault = _stack(tmp_path)

    with pytest.raises(KeyError, match="missing"):
        rebuild.rebuild(target="all", collection_slug="missing")

    assert indexer.calls == []
    assert projector.calls == []
    assert vault.calls == []
