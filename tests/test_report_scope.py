"""Report scope follows an aggregate's ingestion and its exact documents."""

import pytest

from app.context.knowledge_reader import KnowledgeContextReader
from app.knowledge.query import KnowledgeQueryService, KnowledgeRetrievalError
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, ChunkSearchHit, EvidenceSpan
from tests.core_fixtures import persist_evidence_chunk


@pytest.fixture
def merged_paper(tmp_path):
    repository = KnowledgeRepository(str(tmp_path / "scope.db"))
    ingestions, evidence = {}, {}
    paper = None
    for label in ("a", "b"):
        ingestion = repository.create_ingestion(
            collection=f"Collection {label}", sources=[f"{label}.pdf"],
            pdf_max_pages=5, enqueue=False,
        )
        repository.update_ingestion(ingestion.id, status="needs_review")
        span = EvidenceSpan(
            paper_id=f"paper:{label}", chunk_id=f"paper:{label}:chunk:0",
            page_start=1, page_end=1,
            quote=f"Approved evidence from collection {label} supports research synthesis.",
        )
        persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=span)
        candidate = CandidateEntity(
            id=f"candidate-{label}", ingestion_id=ingestion.id,
            topic_slug=ingestion.collection_slug, name="Shared Paper", type="Paper",
            summary="An approved research paper.", confidence=0.95, evidence=span,
        )
        repository.add_candidate_entity(candidate)
        published = repository.publish_entity(
            candidate.id, canonical_id=paper.id if paper else None,
        )
        if paper:
            assert published.id == paper.id
        paper = published
        ingestions[label], evidence[label] = ingestion, span
    assert len(repository.list_published_entities()) == 1
    assert {span.paper_id for span in paper.evidence} == {"paper:a", "paper:b"}
    return repository, ingestions, evidence


def _scopes(ingestions, labels):
    return [ingestions[label].collection_slug for label in labels]


def _hits(evidence):
    return [ChunkSearchHit(chunk_id=span.chunk_id, score=0.9) for span in evidence.values()]


@pytest.mark.parametrize("selected,expected", [
    ("a", {"paper:a"}), ("b", {"paper:b"}),
    ("ab", {"paper:a", "paper:b"}), ("", {"paper:a", "paper:b"}),
])
def test_merged_paper_authorization_corpus_and_hydration(merged_paper, selected, expected):
    repository, ingestions, evidence = merged_paper
    scopes = _scopes(ingestions, selected)
    core = repository.core_repository
    assert core.published_paper_ids(scopes) == expected
    with repository.database.connect() as connection:
        allowed = core.authorized_report_paper_ids_tx(connection, scopes)
        assert allowed == expected
        assert set(core.report_corpus_ids_tx(connection, allowed)) == {
            span.chunk_id for span in evidence.values() if span.paper_id in expected
        }
        hydrated, audits = core.rehydrate_report_candidates_tx(
            connection, _hits(evidence), allowed_paper_ids=allowed,
        )
        assert {item.paper_id for item in hydrated} == expected
        assert [audit.status for audit in audits] == [
            "verified" if span.paper_id in expected else "rejected" for span in evidence.values()
        ]
        if scopes:
            bundles = KnowledgeContextReader().read_formal_bundles_tx(connection, scopes)
            documents = {item.document["id"] for bundle in bundles for item in bundle.evidence}
            assert documents == expected
    hydrated, _ = core.rehydrate_report_candidates(_hits(evidence), allowed_paper_ids=allowed)
    assert {item.paper_id for item in hydrated} == expected


class _Projection:
    def __init__(self, hits=()):
        self.hits, self.allowed, self.calls = list(hits), None, 0

    def search(self, query, **kwargs):
        self.calls += 1
        self.allowed = kwargs.get("allowed_paper_ids")
        # Deliberately ignore the filter, like a stale or untrusted projection.
        return self.hits


@pytest.mark.parametrize("strategy", ["legacy", "dense-v1", "bm25-v1", "hybrid-v1"])
@pytest.mark.parametrize("selected,expected", [
    ("a", {"paper:a"}), ("ab", {"paper:a", "paper:b"}), ("", {"paper:a", "paper:b"}),
])
def test_merged_paper_retrieval_cannot_expand_document_scope(
    merged_paper, strategy, selected, expected,
):
    repository, ingestions, evidence = merged_paper
    vector = _Projection(_hits(evidence))
    service = KnowledgeQueryService(repository, chunk_search=vector, graph_search=_Projection())
    service.settings = service.settings.model_copy(update={"report_retrieval_strategy": strategy})
    result = service.search("approved evidence research", topic_slugs=_scopes(ingestions, selected))
    assert {item["paper_id"] for item in result["evidence"]} == expected
    if strategy != "bm25-v1":
        assert vector.allowed == expected


@pytest.mark.parametrize("missing", ["document_binding", "membership", "mapping"])
def test_unbound_legacy_scope_rejected_at_submission_and_before_projection_io(
    merged_paper, missing,
):
    repository, ingestions, _ = merged_paper
    with repository.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if missing == "document_binding":
            connection.execute(
                "DELETE FROM ingestion_documents WHERE ingestion_id = ?", (ingestions["a"].id,),
            )
        elif missing == "membership":
            connection.execute(
                "DELETE FROM collection_memberships WHERE ingestion_id = ?", (ingestions["a"].id,),
            )
        else:
            connection.execute("DELETE FROM legacy_record_map WHERE core_table = 'entities'")
    scopes = _scopes(ingestions, "a")
    assert repository.core_repository.published_paper_ids(scopes) == set()
    assert repository.core_repository.published_paper_ids([]) == {"paper:a", "paper:b"}
    if missing != "mapping":
        remaining = repository.core_repository.published_paper_ids(_scopes(ingestions, "ab"))
        assert remaining == {"paper:b"}
    vector, graph = _Projection(), _Projection()
    query = KnowledgeQueryService(repository, chunk_search=vector, graph_search=graph)
    with pytest.raises(KnowledgeRetrievalError, match="没有已审核"):
        query.search("evidence", topic_slugs=scopes)
    service = KnowledgeReportService(repository, query_service=query, require_live_llm=False)
    with pytest.raises(ValueError, match="没有已审核"):
        service.submit(query="evidence", topic_slugs=scopes)
    assert vector.calls == graph.calls == 0
    assert repository.reports.list_reports() == []


def test_shared_document_can_be_bound_to_more_than_one_ingestion(merged_paper):
    repository, ingestions, evidence = merged_paper
    # Reimport the existing A document into B through the real document write path.
    persist_evidence_chunk(repository, ingestion_id=ingestions["b"].id, evidence=evidence["a"])
    with repository.database.connect() as connection:
        assert connection.execute(
            "SELECT ingestion_id FROM documents WHERE id = 'paper:a'",
        ).fetchone()[0] == ingestions["a"].id
        scopes = _scopes(ingestions, "b")
        assert repository.core_repository.authorized_report_paper_ids_tx(connection, scopes) == {
            "paper:a", "paper:b",
        }
        bundles = KnowledgeContextReader().read_formal_bundles_tx(connection, scopes)
        assert {item.document["id"] for bundle in bundles for item in bundle.evidence} == {
            "paper:a", "paper:b",
        }


class _LLM:
    provider_name = "fixture"

    def __init__(self, callback):
        self.callback = callback

    def invoke(self, *args, **kwargs):
        self.callback()
        return "## 背景\n支持结论。[E1]\n## 比较\n有原文证据。[E1]\n## 局限\n限于证据。[E1]"


@pytest.mark.parametrize("strategy", ["legacy", "hybrid-v1"])
def test_submission_revalidates_ingestion_document_binding_after_generation(merged_paper, strategy):
    repository, ingestions, evidence = merged_paper
    query = KnowledgeQueryService(
        repository, chunk_search=_Projection(_hits(evidence)), graph_search=_Projection(),
    )
    query.settings = query.settings.model_copy(update={"report_retrieval_strategy": strategy})

    def revoke_binding():
        with repository.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM ingestion_documents WHERE ingestion_id = ?", (ingestions["a"].id,),
            )

    service = KnowledgeReportService(repository, query_service=query, llm=_LLM(revoke_binding))
    report = service.submit(query="approved evidence", topic_slugs=_scopes(ingestions, "a"))
    failed = service.run(report.id)
    assert failed.status == "failed", failed.error
    assert failed.content == ""
    assert {item.paper_id for item in failed.evidence} == {"paper:a"}
    assert failed.run_metadata["submission_validation"]["outcome"] == "report_knowledge_changed"
    assert repository.jobs.get_resource_job("report", report.id).status == "failed"
