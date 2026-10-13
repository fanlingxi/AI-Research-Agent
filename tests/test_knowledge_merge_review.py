"""Merging a reviewed entity candidate into an existing published entity."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.extractor import ExtractionResult
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.repository import KnowledgeRepository, merge_compatible_types
from app.knowledge.schemas import (
    BulkCandidateDecision,
    EvidenceSpan,
    ExtractedEntity,
    ExtractedRelation,
    KnowledgeExtraction,
    PaperReading,
)
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.schemas.documents import ParsedDocument

QUOTE = "Retrieval augmented generation grounds answers in retrieved evidence."


def test_nfkc_paper_duplicate_group_and_merge_suggestions(tmp_path):
    app, repository, ingestion_id = _build(tmp_path)
    papers = [item["candidate"] for item in repository.list_candidates(ingestion_id)
              if item["kind"] == "entity" and item["candidate"]["type"] == "Paper"]
    repository.update_candidate(papers[0]["id"], {"name": "检索增强：展望 Ａ"})
    repository.update_candidate(papers[1]["id"], {"name": "  检索增强:展望 a  "})
    draft_group = next(group for group in repository.duplicate_groups(ingestion_id)["groups"]
                       if group["type"] == "Paper")
    assert len(draft_group["candidates"]) == 2
    with TestClient(app) as client:
        response = client.post(
            f"/api/knowledge/candidates/{papers[0]['id']}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 200
    group = next(group for group in repository.duplicate_groups(ingestion_id)["groups"]
                 if group["type"] == "Paper")
    assert group["target"]["match_kind"] == "exact"
    assert group["target"]["similarity"] == 1
    assert group["candidates"][0]["id"] == papers[1]["id"]
    assert repository.find_merge_suggestions(
        name="检索增强:展望 a", entity_type="Paper", aliases=[],
    )[0].match_kind == "exact"


class _SharedConceptExtractor:
    """Every paper mentions the same concept, as real cross-paper corpora do."""

    def __init__(self, rag_type_in_paper_b: str = "Method") -> None:
        self.rag_type_in_paper_b = rag_type_in_paper_b

    def extract(self, paper, chunks):
        rag_type = self.rag_type_in_paper_b if paper.title.endswith("paper-b") else "Method"
        evidence = EvidenceSpan(
            paper_id=paper.id, chunk_id=chunks[0].id, page_start=1, page_end=1, quote=QUOTE
        )

        def entity(name: str, entity_type: str) -> ExtractedEntity:
            return ExtractedEntity(
                name=name,
                type=entity_type,
                summary=f"{name}是用于合并审核测试的知识实体说明。",
                confidence=0.9,
                evidence=evidence,
            )

        return ExtractionResult(
            KnowledgeExtraction(
                reading=PaperReading(
                    research_problem="研究检索增强生成如何利用证据回答问题。",
                    core_contributions=["提供可审核的知识合并流程。"],
                    method_summary="先检索证据，再基于证据生成回答。",
                    evidence=evidence,
                ),
                entities=[entity("检索增强生成", rag_type), entity("开放域问答", "Task")],
                relations=[
                    ExtractedRelation(
                        source_name="检索增强生成",
                        target_name="开放域问答",
                        type="APPLIES_TO",
                        summary="检索增强生成用于开放域问答。",
                        confidence=0.9,
                        evidence=evidence,
                    )
                ],
            )
        )


def _parser(*, source: str, max_pages: int) -> ParsedDocument:
    return ParsedDocument(
        source=source,
        title=f"RAG Paper {Path(source).stem}",
        text=QUOTE,
        pages=1,
        page_texts=[QUOTE],
        page_numbers=[1],
    )


def _build(tmp_path, extractor: _SharedConceptExtractor | None = None):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
        chunk_size=40,
        chunk_overlap=10,
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    service = KnowledgeIngestionService(
        repository,
        settings=settings,
        extractor=extractor or _SharedConceptExtractor(),
        parser=_parser,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(settings.knowledge_vault_path),
        require_live_llm=False,
    )
    ingestion = service.submit(
        topic="检索增强合并", sources=["paper-a.pdf", "paper-b.pdf"], pdf_max_pages=1
    )
    assert service.run(ingestion.id).status == "needs_review"
    app = create_app(knowledge_repository=repository, knowledge_service=service)
    return app, repository, ingestion.id


def _entities(repository: KnowledgeRepository, ingestion_id: str) -> dict[tuple[str, str], str]:
    """Map (paper title suffix, entity name) to candidate id."""

    papers = {}
    for item in repository.list_candidates(ingestion_id):
        candidate = item["candidate"]
        if item["kind"] == "entity" and candidate["type"] == "Paper":
            papers[candidate["evidence"]["paper_id"]] = candidate["name"][-1]
    return {
        (papers[item["candidate"]["evidence"]["paper_id"]], item["candidate"]["name"]): item[
            "candidate"
        ]["id"]
        for item in repository.list_candidates(ingestion_id)
        if item["kind"] == "entity"
    }


def _review_events(path: str, candidate_id: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT decision, canonical_id FROM review_events WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchall()


def test_merge_attaches_evidence_to_canonical_entity_without_duplicates(tmp_path) -> None:
    app, repository, ingestion_id = _build(tmp_path)
    ids = _entities(repository, ingestion_id)
    first, second = ids[("a", "检索增强生成")], ids[("b", "检索增强生成")]

    with TestClient(app) as client:
        groups = client.get(f"/api/knowledge/ingestions/{ingestion_id}/duplicate-groups").json()
        rag_group = next(group for group in groups["groups"] if group["name"] == "检索增强生成")
        assert rag_group["target"] is None
        assert {item["id"] for item in rag_group["candidates"]} == {first, second}

        approved = client.post(
            f"/api/knowledge/candidates/{first}/decision", json={"decision": "approve"}
        )
        assert approved.status_code == 200, approved.text
        canonical_id = approved.json()["candidate"]["candidate"]["canonical_id"]

        # Stored suggestions were computed before anything was published; the
        # review page and duplicate view must read them live.
        page = client.get(
            f"/api/knowledge/ingestions/{ingestion_id}/candidate-page?kind=entity&limit=100"
        ).json()
        live = next(item for item in page["items"] if item["candidate"]["id"] == second)
        assert live["candidate"]["merge_suggestions"][0]["entity_id"] == canonical_id
        assert live["candidate"]["merge_suggestions"][0]["match_kind"] == "exact"
        groups = client.get(f"/api/knowledge/ingestions/{ingestion_id}/duplicate-groups").json()
        rag_group = next(group for group in groups["groups"] if group["name"] == "检索增强生成")
        assert rag_group["target"]["entity_id"] == canonical_id
        assert [item["id"] for item in rag_group["candidates"]] == [second]

        missing = client.post(
            f"/api/knowledge/candidates/{second}/decision",
            json={"decision": "merge", "canonical_id": "entity-missing"},
        )
        assert missing.status_code == 409
        assert repository.get_candidate(second)["candidate"]["status"] == "draft"
        assert _review_events(repository.path, second) == []

        merged = client.post(
            f"/api/knowledge/candidates/{second}/decision",
            json={"decision": "merge", "canonical_id": canonical_id},
        )
        assert merged.status_code == 200, merged.text
        assert merged.json()["applied"] is True
        candidate = repository.get_candidate(second)["candidate"]
        assert candidate["status"] == "merged"
        assert candidate["canonical_id"] == canonical_id
        assert _review_events(repository.path, second) == [("merge", canonical_id)]

        replay = client.post(
            f"/api/knowledge/candidates/{second}/decision",
            json={"decision": "merge", "canonical_id": canonical_id},
        ).json()
        assert replay["replayed"] is True and replay["applied"] is False
        conflicting = client.post(
            f"/api/knowledge/candidates/{second}/decision",
            json={"decision": "merge", "canonical_id": "entity-other"},
        )
        assert conflicting.status_code == 409
        assert _review_events(repository.path, second) == [("merge", canonical_id)]

    rag = [
        entity
        for entity in repository.list_published_entities()
        if entity.name == "检索增强生成"
    ]
    assert [entity.id for entity in rag] == [canonical_id]
    papers = {evidence.paper_id for evidence in rag[0].evidence}
    assert len(papers) == 2
    detail = repository.entity_detail(canonical_id)
    assert {mention["candidate_id"] for mention in detail["source_mentions"]} == {first, second}
    assert {mention.candidate_id for mention in repository.list_source_mentions(canonical_id)} == {
        first,
        second,
    }


def test_relation_from_merged_endpoint_attaches_to_canonical_and_links_papers(tmp_path) -> None:
    app, repository, ingestion_id = _build(tmp_path)
    ids = _entities(repository, ingestion_id)

    with TestClient(app) as client:

        def decide(candidate_id: str, body: dict) -> dict:
            response = client.post(f"/api/knowledge/candidates/{candidate_id}/decision", json=body)
            assert response.status_code == 200, response.text
            return response.json()

        for key in (("a", "RAG Paper paper-a"), ("b", "RAG Paper paper-b")):
            decide(ids[key], {"decision": "approve"})
        rag = decide(ids[("a", "检索增强生成")], {"decision": "approve"})
        task = decide(ids[("a", "开放域问答")], {"decision": "approve"})
        rag_id = rag["candidate"]["candidate"]["canonical_id"]
        task_id = task["candidate"]["candidate"]["canonical_id"]

        # Bulk merge of the second paper's duplicates into the canonical entities.
        for name, target in (("检索增强生成", rag_id), ("开放域问答", task_id)):
            bulk = client.post(
                f"/api/knowledge/ingestions/{ingestion_id}/candidates/bulk-decision",
                json={
                    "candidate_ids": [ids[("b", name)]],
                    "decision": "merge",
                    "canonical_id": target,
                },
            )
            assert bulk.status_code == 200, bulk.text
            assert bulk.json()["applied"] == 1
        assert _review_events(repository.path, ids[("b", "检索增强生成")]) == [("merge", rag_id)]

        page = client.get(
            f"/api/knowledge/ingestions/{ingestion_id}/candidate-page?kind=relation&limit=100"
        ).json()
        for item in page["items"]:
            decide(item["candidate"]["id"], {"decision": "approve"})

        graph = client.get("/api/knowledge/map").json()

    applies = [
        relation
        for relation in repository.list_published_relations()
        if relation.type == "APPLIES_TO"
    ]
    assert len(applies) == 1
    assert len(applies[0].evidence) == 2
    assert {(item.source_entity_id, item.target_entity_id) for item in applies} == {
        (rag_id, task_id)
    }
    paper_ids = {
        entity.id for entity in repository.list_published_entities() if entity.type == "Paper"
    }
    presenters = {
        relation.source_entity_id
        for relation in repository.list_published_relations()
        if relation.target_entity_id == rag_id
    }
    assert presenters >= paper_ids
    linked_to_rag = {
        edge["source"]
        for edge in graph["edges"]
        if edge["target"] == rag_id and edge["source"] in paper_ids
    }
    assert linked_to_rag == paper_ids
    assert len([node for node in graph["nodes"] if node["label"] == "检索增强生成"]) == 1


def test_cross_type_suggestion_is_flagged_and_merge_keeps_target_type(tmp_path) -> None:
    """The same idea published as a Method is offered to a Concept draft, flagged."""

    app, repository, ingestion_id = _build(
        tmp_path, _SharedConceptExtractor(rag_type_in_paper_b="Concept")
    )
    ids = _entities(repository, ingestion_id)
    method_draft, concept_draft = ids[("a", "检索增强生成")], ids[("b", "检索增强生成")]

    with TestClient(app) as client:
        # Before publication the two drafts of interchangeable types share a group.
        groups = client.get(f"/api/knowledge/ingestions/{ingestion_id}/duplicate-groups").json()
        rag_group = next(group for group in groups["groups"] if group["name"] == "检索增强生成")
        assert {item["id"] for item in rag_group["candidates"]} == {method_draft, concept_draft}

        approved = client.post(
            f"/api/knowledge/candidates/{method_draft}/decision", json={"decision": "approve"}
        )
        method_id = approved.json()["candidate"]["candidate"]["canonical_id"]
        page = client.get(
            f"/api/knowledge/ingestions/{ingestion_id}/candidate-page?kind=entity&limit=100"
        ).json()
        live = next(item for item in page["items"] if item["candidate"]["id"] == concept_draft)
        suggestion = live["candidate"]["merge_suggestions"][0]
        assert suggestion["entity_id"] == method_id
        assert suggestion["type"] == "Method"
        assert suggestion["match_kind"] == "exact"
        assert suggestion["type_differs"] is True
        # Strict types (Paper/Dataset/Metric) never see cross-type matches.
        assert repository.find_merge_suggestions(
            name="检索增强生成", entity_type="Dataset", aliases=[]
        ) == []

        # Nothing auto-merges: bulk approval leaves the cross-type duplicate for review.
        ready = client.post(f"/api/knowledge/ingestions/{ingestion_id}/approve-ready")
        assert ready.status_code == 200, ready.text
        assert repository.get_candidate(concept_draft)["candidate"]["status"] == "draft"

        merged = client.post(
            f"/api/knowledge/candidates/{concept_draft}/decision",
            json={"decision": "merge", "canonical_id": method_id},
        )
        assert merged.status_code == 200, merged.text

    rag = [e for e in repository.list_published_entities() if e.name == "检索增强生成"]
    assert [(entity.id, entity.type) for entity in rag] == [(method_id, "Method")]


def test_merge_type_compatibility() -> None:
    assert merge_compatible_types("Dataset") == {"Dataset"}
    assert merge_compatible_types("Paper") == {"Paper"}
    assert merge_compatible_types("Task") == {"Concept", "Method", "Task"}


def test_bulk_merge_requires_target() -> None:
    with pytest.raises(ValidationError):
        BulkCandidateDecision(candidate_ids=["candidate-1"], decision="merge")
    with pytest.raises(ValidationError):
        BulkCandidateDecision(
            candidate_ids=["candidate-1"], decision="approve", canonical_id="entity-1"
        )
