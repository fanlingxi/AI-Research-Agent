import math
import sqlite3

import pytest

from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.knowledge.query import KnowledgeQueryService
from app.knowledge.schemas import ChunkSearchHit
from app.retrieval.hybrid import bm25_rank, reciprocal_rank_fusion, tokenize
from tests.test_context_builder import _VectorCandidates
from tests.test_query_consistency import CHUNK, _other_paper, _Search, _vector, _write
from tests.test_report_commit import _stack
from tests.test_reports import _repository_with_paper
from tests.test_retrieval_audit import _context_setup


def test_bm25_single_document_positive_idf_and_identifier_cjk_matching():
    assert bm25_rank("term", {"one": "term"})[0][1] == pytest.approx(math.log(4 / 3))
    assert tokenize("ＧＴＥ－３０００") == tokenize("gte3000")
    result = bm25_rank(
        "GTE3000 租约恢复",
        {
            "a": "GTE-3000 provides 租约恢复",
            "b": "GTE-4000 遥测",
            "empty": "",
        },
    )
    assert result[0][0] == "a"
    assert bm25_rank("never", {"a": "other"}) == []


def test_rrf_uses_unique_channel_ranks_and_deterministic_ties():
    result = reciprocal_rank_fusion({"bm25": ["a", "a", "b"], "vector": ["b", "c"]})
    assert [row[0] for row in result] == ["b", "a", "c"]
    assert result[0][1] == pytest.approx(1 / 62 + 1 / 61)
    assert result[0][2] == {"bm25": 2, "vector": 1}
    assert result[1][1] == pytest.approx(1 / 61)


@pytest.mark.parametrize("strategy", ["bm25-v1", "hybrid-v1"])
def test_report_bm25_can_recall_sqlite_chunk_without_vector_hit(tmp_path, strategy):
    repository, ingestion = _repository_with_paper(tmp_path)
    service = KnowledgeQueryService(repository, chunk_search=_Search(), graph_search=_Search())
    service.settings = service.settings.model_copy(update={"report_retrieval_strategy": strategy})
    result = service.search("approved evidence", topic_slugs=[ingestion.topic_slug])
    assert [item["chunk_id"] for item in result["evidence"]] == [CHUNK]
    audit = result["retrieval_diagnostics"]["retrieval_audit"]
    assert audit["strategy_id"] == f"quick-report-{strategy}"
    assert audit["parameters"]["rankings"][CHUNK]["bm25_rank"] == 1
    assert "vector_rank" not in audit["parameters"]["rankings"][CHUNK]
    assert any(c["channel"] == "bm25" and c["matches"] for c in audit["candidates"])


def test_hybrid_rank_does_not_let_duplicate_or_unknown_vector_candidate_add_votes(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)
    service = KnowledgeQueryService(
        repository,
        chunk_search=_Search(
            lambda *_: [
                ChunkSearchHit(chunk_id="outside", score=1),
                ChunkSearchHit(chunk_id=CHUNK, score=0.9),
                ChunkSearchHit(chunk_id=CHUNK, score=0.9),
            ]
        ),
        graph_search=_Search(),
    )
    service.settings = service.settings.model_copy(
        update={"report_retrieval_strategy": "hybrid-v1"}
    )
    result = service.search("evidence", topic_slugs=[ingestion.topic_slug])
    ranks = result["retrieval_diagnostics"]["retrieval_audit"]["parameters"]["rankings"]
    assert set(ranks) == {CHUNK}
    assert ranks[CHUNK]["rrf_score"] == pytest.approx(2 / 61)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE sources SET version = 'later'",
        "DELETE FROM collection_memberships",
        "INSERT INTO chunks SELECT id || ':new', document_id, legacy_chunk_id || ':new', content, "
        "content_sha256, chunk_index + 100, page_start, page_end, location_json, embedding_ref, "
        "metadata_json, created_at, updated_at "
        "FROM chunks LIMIT 1",
    ],
)
def test_hybrid_report_submission_rechecks_corpus_and_versions(tmp_path, mutation):
    repository, service, report, llm = _stack(tmp_path)
    service.query_service.settings = service.query_service.settings.model_copy(
        update={"report_retrieval_strategy": "hybrid-v1"},
    )
    llm.callback = lambda: _write(repository, mutation)
    failed = service.run(report.id)
    assert failed.status == "failed", failed.error
    assert "report_knowledge_changed" in failed.error
    assert failed.content == ""


def test_hybrid_bm25_calculation_has_no_open_sqlite_connection(tmp_path, monkeypatch):
    import app.knowledge.query as module

    repository, ingestion = _repository_with_paper(tmp_path)
    service = KnowledgeQueryService(repository, chunk_search=_vector(), graph_search=_Search())
    service.settings = service.settings.model_copy(
        update={"report_retrieval_strategy": "hybrid-v1"}
    )
    original = repository.core_repository.report_corpus_ids_tx
    connections = []

    def capture(connection, *args):
        connections.append(connection)
        return original(connection, *args)

    def rank(*args, **kwargs):
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        return bm25_rank(*args, **kwargs)

    monkeypatch.setattr(repository.core_repository, "report_corpus_ids_tx", capture)
    monkeypatch.setattr(module, "bm25_rank", rank)
    assert service.search("evidence", topic_slugs=[ingestion.topic_slug])["evidence"]


@pytest.mark.parametrize("strategy", ["bm25-v1", "hybrid-v1"])
def test_context_hybrid_keeps_complete_bundles_and_source_audit(tmp_path, strategy):
    repository, _, task, evidence = _context_setup(tmp_path)
    builder = ContextBuilderService(
        repository, vector_retriever=_VectorCandidates(evidence.chunk_id)
    )
    package = builder.build_context(
        ContextBuildRequest(
            task_id=task.id,
            retrieval_strategy=strategy,
            enable_vector_candidates=True,
        )
    )
    assert package.knowledge.claim_bundles
    bundle = package.knowledge.claim_bundles[0]
    assert bundle.evidence and bundle.chunks and bundle.sources
    assert package.retrieval_audit.strategy_id == f"project-context-{strategy}"
    assert any(c.channel == "bm25" for c in package.retrieval_audit.candidates)
    assert all(
        c.target_id != "stale-qdrant-id" or c.status == "rejected"
        for c in package.retrieval_audit.candidates
    )


def test_bm25_corpus_cannot_expand_to_other_published_scope(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)
    _, _, hidden = _other_paper(repository)
    service = KnowledgeQueryService(repository, chunk_search=_Search(), graph_search=_Search())
    service.settings = service.settings.model_copy(update={"report_retrieval_strategy": "bm25-v1"})
    result = service.search("other approved evidence", topic_slugs=[ingestion.topic_slug])
    assert (
        hidden.chunk_id not in result["retrieval_diagnostics"]["report_input"]["corpus_chunk_ids"]
    )
    assert all(item["paper_id"] == "paper:formal" for item in result["evidence"])


def test_stale_vector_pin_is_not_counted_as_a_fusion_vote(tmp_path):
    from app.benchmarking.retrieval_compare import LocalHashProjection

    repository, ingestion = _repository_with_paper(tmp_path)
    pin = (
        LocalHashProjection(repository)
        .search(
            "evidence",
            allowed_paper_ids={"paper:formal"},
            top_k=1,
        )[0]
        .source_identity.model_copy(update={"source_version": "stale"})
    )
    service = KnowledgeQueryService(
        repository,
        chunk_search=_Search(
            lambda *_: [
                ChunkSearchHit(chunk_id=CHUNK, score=1, source_identity=pin),
            ]
        ),
        graph_search=_Search(),
    )
    service.settings = service.settings.model_copy(
        update={"report_retrieval_strategy": "hybrid-v1"}
    )
    audit = service.search("evidence", topic_slugs=[ingestion.topic_slug])["retrieval_diagnostics"][
        "retrieval_audit"
    ]
    # Independent BM25 may select the current authorized text; the stale score has no vote.
    assert audit["parameters"]["rankings"][CHUNK]["rrf_score"] == pytest.approx(1 / 61)
    assert "vector_rank" not in audit["parameters"]["rankings"][CHUNK]
    assert audit["candidates"][0]["status"] == "rejected"
