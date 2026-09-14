from __future__ import annotations

import sqlite3

import pytest

import app.knowledge.query as query_module
from app.knowledge.query import KnowledgeQueryService, KnowledgeRetrievalError
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.schemas import CandidateEntity, ChunkSearchHit, EvidenceSpan
from tests.core_fixtures import persist_evidence_chunk
from tests.test_context_builder import _formal_relation
from tests.test_reports import _repository_with_paper

CHUNK = "paper:formal:page:2:chunk:0"


class _Search:
    def __init__(self, callback=None):
        self.callback = callback
        self.calls = 0

    def search(self, query, **kwargs):
        self.calls += 1
        return self.callback(self.calls, kwargs) if self.callback else []


def _vector(callback=None):
    return _Search(callback or (lambda *_: [ChunkSearchHit(chunk_id=CHUNK, score=0.9)]))


def _write(repository, sql, params=()):
    with repository.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(sql, params)


def _audit(result):
    return result["retrieval_diagnostics"]["retrieval_audit"]


def _graph_setup(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)
    paper = repository.list_published_entities(ingestion.topic_slug)[0]
    evidence = paper.evidence[0]
    candidate = CandidateEntity(
        id="candidate-method", ingestion_id=ingestion.id, topic_slug=ingestion.topic_slug,
        name="Grounded Method", type="Method", summary="An approved method.",
        confidence=0.9, evidence=evidence,
    )
    repository.add_candidate_entity(candidate)
    method = repository.publish_entity(candidate.id)
    _formal_relation(
        repository, collection_slug=ingestion.topic_slug, ingestion_id=ingestion.id,
        relation_id="query-edge", source_entity_id=paper.id,
        target_entity_id=method.id, evidence=evidence,
    )
    return repository, ingestion


def test_projection_io_and_ranking_have_no_open_connection(tmp_path, monkeypatch):
    repository, ingestion = _repository_with_paper(tmp_path)
    core = repository.core_repository
    scope_reader = core.authorized_report_paper_ids_tx
    connections = []

    def scope(connection, topics):
        connections.append(connection)
        return scope_reader(connection, topics)

    def check_closed():
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")

    def vector(*args):
        check_closed()
        return [ChunkSearchHit(chunk_id=CHUNK, score=0.9)]

    def graph(*args):
        check_closed()
        _write(repository, "UPDATE documents SET title = 'Latest before observation'")
        return []

    original_rank = query_module._rank_evidence

    def rank(*args, **kwargs):
        check_closed()
        return original_rank(*args, **kwargs)

    monkeypatch.setattr(core, "authorized_report_paper_ids_tx", scope)
    monkeypatch.setattr(query_module, "_rank_evidence", rank)
    result = KnowledgeQueryService(
        repository, chunk_search=_Search(vector), graph_search=_Search(graph),
    ).search("evidence", topic_slugs=[ingestion.topic_slug])
    assert result["evidence"][0]["title"] == "Latest before observation"
    assert len(connections) == 2
    assert _audit(result)["parameters"]["scope_attempts"] == 1


def test_authorization_chunks_and_graph_share_one_sqlite_snapshot(tmp_path, monkeypatch):
    repository, ingestion = _graph_setup(tmp_path)
    builder = KnowledgeQueryService(
        repository, chunk_search=_vector(),
        graph_search=_Search(lambda *_: [{"edge_id": "query-edge"}]),
    )
    original_chunks = repository.core_repository.rehydrate_report_candidates_tx
    original_graph = query_module.rehydrate_graph_candidates_tx
    observed = []
    changed = False

    def chunks(connection, *args, **kwargs):
        nonlocal changed
        observed.append(connection)
        result = original_chunks(connection, *args, **kwargs)
        if not changed:
            changed = True
            with repository.database.connect() as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE documents SET title = 'Future title'")
                writer.execute("UPDATE entities SET name = 'Future entity'")
                writer.execute("UPDATE sources SET version = 'future-version'")
        return result

    def graph(connection, *args, **kwargs):
        assert connection is observed[-1]
        return original_graph(connection, *args, **kwargs)

    monkeypatch.setattr(repository.core_repository, "rehydrate_report_candidates_tx", chunks)
    monkeypatch.setattr(query_module, "rehydrate_graph_candidates_tx", graph)
    result = builder.search("evidence", topic_slugs=[ingestion.topic_slug])
    assert len(result["graph"]) == 1
    assert "Future" not in str(result) and "future-version" not in str(result)
    later = builder.search("evidence", topic_slugs=[ingestion.topic_slug])
    assert later["evidence"][0]["title"] == "Future title"
    assert later["graph"][0]["source_name"] == "Future entity"
    assert "future-version" in str(_audit(later))


@pytest.mark.parametrize("sql", [
    "UPDATE entities SET status = 'retracted' WHERE entity_type = 'Paper'",
    "UPDATE claims SET status = 'retracted'",
    "DELETE FROM collection_memberships",
    "DELETE FROM legacy_record_map WHERE core_table = 'entities'",
])
def test_revoked_scope_blocks_before_io_and_cannot_fall_back_to_legacy(tmp_path, sql):
    repository, ingestion = _repository_with_paper(tmp_path)
    _write(repository, sql)
    if sql.startswith("UPDATE"):
        # The compatibility/shadow entry remains available for migration; it
        # still sees legacy content, which must not authorize this query.
        assert repository.published_paper_ids([ingestion.topic_slug]) == {"paper:formal"}
    vector, graph = _vector(), _Search()
    with pytest.raises(KnowledgeRetrievalError, match="没有已审核"):
        KnowledgeQueryService(repository, chunk_search=vector, graph_search=graph).search(
            "evidence", topic_slugs=[ingestion.topic_slug],
        )
    assert vector.calls == graph.calls == 0


@pytest.mark.parametrize("during", ["vector", "graph"])
def test_revocation_during_external_io_cannot_return_evidence(tmp_path, during):
    repository, ingestion = _repository_with_paper(tmp_path)

    def revoke(*args):
        _write(repository, "UPDATE claims SET status = 'retracted'")
        return [ChunkSearchHit(chunk_id=CHUNK, score=0.9)] if during == "vector" else []

    vector = _Search(revoke) if during == "vector" else _vector()
    graph = _Search(revoke) if during == "graph" else _Search()
    with pytest.raises(KnowledgeRetrievalError) as error:
        KnowledgeQueryService(repository, chunk_search=vector, graph_search=graph).search(
            "evidence", topic_slugs=[ingestion.topic_slug],
        )
    assert error.value.attempts[-1]["outcome"] == "scope_empty"
    assert vector.calls == graph.calls == 1


@pytest.mark.parametrize("pinned", [False, True])
def test_final_source_version_binding_after_projection_io(tmp_path, pinned):
    repository, ingestion = _repository_with_paper(tmp_path)
    _, observations = repository.core_repository.rehydrate_report_candidates(
        [ChunkSearchHit(chunk_id=CHUNK, score=0.9)], allowed_paper_ids={"paper:formal"},
    )
    source = observations[0].matches[0].sources[0]

    def vector(*args):
        _write(repository, "UPDATE sources SET version = 'new-version'")
        return [ChunkSearchHit(
            chunk_id=CHUNK, score=0.9, source_identity=source if pinned else None,
        )]

    result = KnowledgeQueryService(
        repository, chunk_search=_Search(vector), graph_search=_Search(),
    ).search("evidence", topic_slugs=[ingestion.topic_slug])
    audit = _audit(result)
    assert len(result["evidence"]) == (0 if pinned else 1)
    assert audit["candidates"][0]["reason"] == (
        "source_version_mismatch" if pinned else "sqlite_bound_provider_version_unknown"
    )
    if not pinned:
        assert audit["selections"][0]["sources"][0]["source_version"] == "new-version"


def test_invalid_content_at_final_read_is_rejected(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)

    def graph(*args):
        _write(repository, "UPDATE chunks SET content = 'Changed without matching hash'")
        return []

    result = KnowledgeQueryService(
        repository, chunk_search=_vector(), graph_search=_Search(graph),
    ).search("evidence", topic_slugs=[ingestion.topic_slug])
    assert result["evidence"] == []
    assert _audit(result)["candidates"][0]["status"] == "rejected"


@pytest.mark.parametrize(("channel", "error_type", "failures", "calls"), [
    ("vector", TimeoutError, 1, 2),
    ("vector", TimeoutError, 5, 2),
    ("vector", ValueError, 5, 1),
    ("graph", TimeoutError, 1, 2),
    ("graph", ConnectionError, 5, 2),
    ("graph", ValueError, 5, 1),
])
def test_finite_channel_retries_and_failure_accounting(
    tmp_path, channel, error_type, failures, calls,
):
    repository, ingestion = _repository_with_paper(tmp_path)

    def flaky(attempt, kwargs):
        if attempt <= failures:
            raise error_type("secret credentials must not be recorded")
        return [ChunkSearchHit(chunk_id=CHUNK, score=0.9)] if channel == "vector" else []

    failed = _Search(flaky)
    service = KnowledgeQueryService(
        repository, chunk_search=failed if channel == "vector" else _vector(),
        graph_search=failed if channel == "graph" else _Search(),
    )
    if channel == "vector" and failures > 1:
        with pytest.raises(KnowledgeRetrievalError) as error:
            service.search("evidence", topic_slugs=[ingestion.topic_slug])
        attempts = error.value.attempts
        assert "secret" not in str(error.value)
    else:
        result = service.search("evidence", topic_slugs=[ingestion.topic_slug])
        attempts = _audit(result)["parameters"]["attempts"]
        assert len(result["evidence"]) == 1
        assert bool(result["warnings"]) == (channel == "graph" and failures > 1)
    assert failed.calls == calls
    assert len([a for a in attempts if a.get("channel") == channel]) == calls
    assert "secret" not in str(attempts)


def test_malformed_graph_degrades_without_accepting_projection_text(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)
    result = KnowledgeQueryService(
        repository, chunk_search=_vector(), graph_search=_Search(lambda *_: [{
            "edge_id": "edge", "source_identity": {"invalid": True},
            "source_name": "FORGED",
        }]),
    ).search("evidence", topic_slugs=[ingestion.topic_slug])
    assert len(result["evidence"]) == 1 and result["graph"] == []
    assert result["warnings"] and "FORGED" not in str(result)
    assert _audit(result)["parameters"]["attempts"][-1]["outcome"] == "graph_invalid"


def test_caller_and_adapter_scope_mutation_does_not_change_effective_request(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)
    topics = [ingestion.topic_slug]

    def vector(attempt, kwargs):
        topics.clear()
        kwargs["allowed_paper_ids"].clear()
        return [ChunkSearchHit(chunk_id=CHUNK, score=0.9)]

    def graph(attempt, kwargs):
        assert kwargs["topic_slugs"] == [ingestion.topic_slug]
        kwargs["topic_slugs"].clear()
        return []

    result = KnowledgeQueryService(
        repository, chunk_search=_Search(vector), graph_search=_Search(graph),
    ).search("evidence", topic_slugs=topics)
    assert result["topic_slugs"] == [ingestion.topic_slug]
    assert len(result["evidence"]) == 1


def _other_paper(repository):
    ingestion = repository.create_ingestion(
        topic="Other scope", sources=["other.pdf"], pdf_max_pages=5, enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="paper:other", chunk_id="paper:other:page:1:chunk:0",
        page_start=1, page_end=1, quote="Other approved evidence supports retrieval.",
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    candidate = CandidateEntity(
        id="candidate-other-paper", ingestion_id=ingestion.id, topic_slug=ingestion.topic_slug,
        name="Other Paper", type="Paper", summary="Different approved paper.",
        confidence=0.9, evidence=evidence,
    )
    repository.add_candidate_entity(candidate)
    paper = repository.publish_entity(candidate.id)
    return ingestion, paper, evidence


@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("transient", [False, True])
def test_scope_change_requeries_and_continual_change_is_bounded(tmp_path, continuous, transient):
    repository, ingestion = _repository_with_paper(tmp_path)
    other, paper, evidence = _other_paper(repository)
    observed = []

    def vector(attempt, kwargs):
        if transient and attempt % 2:
            raise TimeoutError("transient failure before returning candidates")
        attempt = attempt // 2 if transient else attempt
        observed.append(kwargs["allowed_paper_ids"])
        if attempt == 1 or continuous:
            target = ingestion.topic_slug if attempt == 1 else other.topic_slug
            _write(repository,
                   "UPDATE collection_memberships SET collection_slug = ? WHERE aggregate_id = ?",
                   (target, paper.id))
        return [ChunkSearchHit(chunk_id=CHUNK if attempt == 1 else evidence.chunk_id, score=0.9)]

    vector_search, graph_search = _Search(vector), _Search()
    query = KnowledgeQueryService(repository, chunk_search=vector_search, graph_search=graph_search)
    if continuous:
        with pytest.raises(KnowledgeRetrievalError, match="连续两次") as error:
            query.search("evidence", topic_slugs=[ingestion.topic_slug])
        attempts = error.value.attempts
        assert len([a for a in attempts if a["outcome"] == "scope_changed"]) == 2
    else:
        result = query.search("evidence", topic_slugs=[ingestion.topic_slug])
        assert result["evidence"][0]["chunk_id"] == evidence.chunk_id
        assert [a["target_id"] for a in _audit(result)["candidates"]] == [evidence.chunk_id]
        assert _audit(result)["parameters"]["scope_attempts"] == 2
        assert len([a for a in _audit(result)["parameters"]["attempts"]
                    if a["outcome"] == "scope_changed"]) == 1
    assert vector_search.calls == (4 if transient else 2)
    assert graph_search.calls == 2
    assert observed == [{"paper:formal"}, {"paper:formal", "paper:other"}]


@pytest.mark.parametrize("failure", ["unavailable", "invalid_content"])
def test_report_failure_persists_retrieval_attempts_or_rejection_audit(tmp_path, failure):
    repository, ingestion = _repository_with_paper(tmp_path)

    def vector(*args):
        if failure == "unavailable":
            raise TimeoutError("private transport details")
        _write(repository, "UPDATE chunks SET content = 'Invalid changed content'")
        return [ChunkSearchHit(chunk_id=CHUNK, score=0.9)]

    class NoLLM:
        def invoke(self, *args, **kwargs):
            pytest.fail("A failed retrieval must not generate a report")

    service = KnowledgeReportService(
        repository, llm=NoLLM(), require_live_llm=False,
        query_service=KnowledgeQueryService(
            repository, chunk_search=_Search(vector), graph_search=_Search(),
        ),
    )
    report = service.submit(query="evidence", topic_slugs=[ingestion.topic_slug])
    result = service.run(report.id)
    assert result.status == repository.jobs.get_resource_job("report", report.id).status == "failed"
    assert result.run_metadata["generation_calls"] == 0
    diagnostics = result.run_metadata["retrieval_diagnostics"]
    if failure == "unavailable":
        assert len(diagnostics["retrieval_attempts"]) == 2
    else:
        assert diagnostics["retrieval_audit"]["candidates"][0]["status"] == "rejected"
    assert "private transport" not in result.model_dump_json()
