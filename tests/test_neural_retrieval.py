import json

import numpy as np
import pytest

from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.retrieval import QdrantContextCandidateRetriever
from app.context.service import ContextBuilderService
from app.retrieval import neural_embeddings as neural
from app.retrieval.embeddings import get_embedding_provider
from app.retrieval.semantic_projection import SemanticProjection
from tests.test_context_neighbors import _setup


class FixedEncoder:
    def __init__(self):
        self.inputs = []

    def tokenizer(self, texts, **kwargs):
        return {"input_ids": [[1] * (8193 if t == "too long" else 3) for t in texts]}

    def encode(self, texts, **kwargs):
        self.inputs.extend(texts)
        assert kwargs["prompt"] == "" and kwargs["normalize_embeddings"]
        return np.asarray([[1.0] + [0.0] * 1023 for _ in texts])


def test_pinned_encoder_roles_and_no_silent_truncation(monkeypatch):
    encoder = FixedEncoder()
    monkeypatch.setattr(neural, "_load", lambda *args: encoder)
    provider = get_embedding_provider(
        Settings(_env_file=None, embedding_provider="qwen3-local", embedding_dimension=1024)
    )
    provider.embed_query("什么是忠实性？")
    provider.embed_documents(["Faithfulness is grounded in the context."])
    assert encoder.inputs == [
        f"Instruct: {neural.QUERY_INSTRUCTION}\nQuery: 什么是忠实性？",
        "Faithfulness is grounded in the context.",
    ]
    with pytest.raises(ValueError, match="truncation"):
        provider.embed_documents(["too long"])
    assert "too long" not in encoder.inputs
    with pytest.raises(ValueError, match="dimension"):
        get_embedding_provider(Settings(_env_file=None, embedding_provider="bge-m3-local"))


def test_collection_identity_separates_models_and_preserves_old_default():
    base = Settings(_env_file=None, embedding_dimension=1024)
    names = [
        neural.collection_name(base.model_copy(update={"embedding_provider": p}))
        for p in ["hash", "qwen3-local", "bge-m3-local"]
    ]
    assert names[0] == base.knowledge_qdrant_collection
    assert len(set(names)) == 3
    for vectors in [[[0.0, 0.0]], [[float("nan"), 1.0]], [[1.0]]]:
        with pytest.raises(ValueError):
            neural.validate_vectors(vectors, 1, 2)


def test_dense_report_requires_vector_success_and_does_not_fall_back(tmp_path):
    from app.knowledge.query import KnowledgeQueryService, KnowledgeRetrievalError
    from tests.test_query_consistency import _Search
    from tests.test_reports import _repository_with_paper

    repo, ingestion = _repository_with_paper(tmp_path)
    settings = Settings(_env_file=None, report_retrieval_strategy="dense-v1")
    service = KnowledgeQueryService(
        repo, chunk_search=_Search(), graph_search=_Search(), settings=settings
    )
    assert service.search("approved method", topic_slugs=[ingestion.topic_slug])["evidence"] == []

    def fail(*args):
        raise ValueError("Encoder unavailable")

    service.chunk_search = _Search(fail)
    with pytest.raises(KnowledgeRetrievalError):
        service.search("approved method", topic_slugs=[ingestion.topic_slug])
    with pytest.raises(ValueError, match="enable_vector_candidates"):
        ContextBuildRequest(task_id="task", retrieval_strategy="dense-v1")


class FixedProvider:
    dimension = 2
    identity = {"model": "fixture-v1"}

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


def test_projection_is_model_bound_and_candidates_are_sqlite_revalidated(tmp_path):
    repo, task, claims = _setup(tmp_path)
    index = tmp_path / "index"
    projection = SemanticProjection(repo, FixedProvider(), index)
    assert projection.search("question", allowed_paper_ids=set(), top_k=10) == []
    reloaded = SemanticProjection(repo, FixedProvider(), index)
    builder = ContextBuilderService(
        repo, vector_retriever=QdrantContextCandidateRetriever(reloaded)
    )
    request = ContextBuildRequest(
        task_id=task.id,
        max_tokens=16000,
        enable_vector_candidates=True,
        retrieval_strategy="dense-v1",
    )
    package = builder.build_context(request)
    assert package.knowledge.claim_bundles
    # A stale projection can never resurrect a withdrawn authoritative claim.
    with repo.database.connect() as db:
        db.execute("UPDATE claims SET status='withdrawn' WHERE id=?", (claims[0],))
    after = builder.build_context(request)
    assert claims[0] not in {b.claim.claim_id for b in after.knowledge.claim_bundles}
    assert all(
        "bm25_rank" not in b.selection.score_breakdown for b in after.knowledge.claim_bundles
    )
    changed = FixedProvider()
    changed.identity = {"model": "fixture-v2"}
    with pytest.raises(ValueError, match="identity changed"):
        SemanticProjection(repo, changed, index)
    manifest = json.loads((index / "manifest.json").read_text(encoding="utf-8"))
    manifest["vectors_sha256"] = "wrong"
    (index / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        SemanticProjection(repo, FixedProvider(), index)
