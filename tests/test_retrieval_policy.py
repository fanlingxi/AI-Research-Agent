from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable
from qdrant_client.http.exceptions import ResponseHandlingException

from app.config.settings import Settings
from app.knowledge.query import Neo4jKnowledgeSearch, QdrantKnowledgeSearch
from app.retrieval.embeddings import OpenAIEmbeddingProvider, get_embedding_provider
from app.retrieval.policy import IO_TIMEOUT_SECONDS, is_transient_error


@pytest.mark.parametrize(("error", "expected"), [
    (ResponseHandlingException(httpx.ReadTimeout("timeout")), True),
    (httpx.ConnectError("connection"), True),
    (ServiceUnavailable("offline"), True),
    (AuthError("invalid credentials"), False),
    (httpx.UnsupportedProtocol("invalid scheme"), False),
    (ValueError("invalid configuration"), False),
    (ImportError("missing SDK"), False),
])
def test_retry_classification(error, expected):
    assert is_transient_error(error) is expected


def test_missing_optional_sdks_do_not_break_degradation(monkeypatch):
    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr("app.retrieval.policy.import_module", missing)
    assert not is_transient_error(ImportError("missing sdk"))
    assert is_transient_error(TimeoutError())


@pytest.mark.parametrize("fail", [False, True])
def test_qdrant_bounds_io_and_closes_client_even_on_failure(monkeypatch, fail):
    captured = {}

    def embeddings(settings, **kwargs):
        captured["embedding_options"] = kwargs
        return SimpleNamespace(embed_query=lambda text: [0.0, 1.0])

    class Client:
        def __init__(self, **kwargs):
            captured["client_options"] = kwargs

        def query_points(self, **kwargs):
            captured["query_options"] = kwargs
            if fail:
                raise TimeoutError("qdrant timed out")
            return SimpleNamespace(points=[SimpleNamespace(payload={"id": "chunk"}, score=0.8)])

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("app.retrieval.embeddings.get_embedding_provider", embeddings)
    monkeypatch.setattr("qdrant_client.QdrantClient", Client)
    search = QdrantKnowledgeSearch(Settings(embedding_provider="hash"))
    if fail:
        with pytest.raises(TimeoutError):
            search.search("context", allowed_paper_ids={"paper"}, top_k=8)
    else:
        hits = search.search("context", allowed_paper_ids={"paper"}, top_k=8)
        assert hits[0].chunk_id == "chunk"
    assert captured["embedding_options"] == {
        "request_timeout": IO_TIMEOUT_SECONDS, "max_retries": 0,
    }
    assert captured["client_options"]["timeout"] == IO_TIMEOUT_SECONDS
    assert captured["query_options"]["timeout"] == IO_TIMEOUT_SECONDS
    assert captured["closed"]


@pytest.mark.parametrize("fail", [False, True])
def test_neo4j_bounds_connection_and_query_and_closes_driver(monkeypatch, fail):
    captured = {}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            captured["session_closed"] = True

        def run(self, query, **kwargs):
            captured["query"] = query
            if fail:
                raise ServiceUnavailable("neo4j offline")
            return [{"edge_id": "edge"}]

    class Driver:
        def session(self):
            return Session()

        def close(self):
            captured["driver_closed"] = True

    def driver(*args, **kwargs):
        captured["driver_options"] = kwargs
        return Driver()

    monkeypatch.setattr("neo4j.GraphDatabase.driver", driver)
    search = Neo4jKnowledgeSearch(Settings())
    if fail:
        with pytest.raises(ServiceUnavailable):
            search.search("context", topic_slugs=["scope"])
    else:
        assert search.search("context", topic_slugs=["scope"]) == [{"edge_id": "edge"}]
    assert captured["query"].timeout == IO_TIMEOUT_SECONDS
    assert captured["driver_options"]["connection_timeout"] == IO_TIMEOUT_SECONDS
    assert captured["driver_options"]["connection_acquisition_timeout"] == IO_TIMEOUT_SECONDS
    assert captured["driver_options"]["max_transaction_retry_time"] == 0
    assert captured["session_closed"] and captured["driver_closed"]


def test_embedding_timeout_and_retry_override_only_apply_when_requested(monkeypatch):
    options = []

    def embeddings(**kwargs):
        options.append(kwargs)
        return SimpleNamespace(embed_documents=lambda texts: [[0.0]] * len(texts))

    monkeypatch.setattr("langchain_openai.OpenAIEmbeddings", embeddings)
    settings = Settings(embedding_provider="openai", openai_api_key="fake-test-key")
    default = get_embedding_provider(settings)
    bounded = get_embedding_provider(settings, request_timeout=IO_TIMEOUT_SECONDS, max_retries=0)
    assert isinstance(default, OpenAIEmbeddingProvider)
    assert default.embed_query("context") == bounded.embed_query("context") == [0.0]
    assert "request_timeout" not in options[0] and "max_retries" not in options[0]
    assert options[1]["request_timeout"] == IO_TIMEOUT_SECONDS and options[1]["max_retries"] == 0
