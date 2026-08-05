from app.config.settings import Settings
from app.knowledge.projector import Neo4jKnowledgeProjector, QdrantKnowledgeIndexer
from app.knowledge.schemas import EvidenceSpan, PublishedEntity, PublishedRelation
from app.schemas.documents import DocumentChunk


class _Session:
    def __init__(self, calls) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def run(self, query: str, **params) -> None:
        self.calls.append((query, params))


class _Driver:
    def __init__(self) -> None:
        self.calls = []

    def session(self):
        return _Session(self.calls)

    def close(self) -> None:
        return None


def test_neo4j_projector_uses_only_v2_labels_and_controlled_edge(monkeypatch) -> None:
    driver = _Driver()
    monkeypatch.setattr("neo4j.GraphDatabase.driver", lambda *args, **kwargs: driver)
    projector = Neo4jKnowledgeProjector(Settings())
    evidence = EvidenceSpan(
        paper_id="paper:test",
        chunk_id="paper:test:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="A grounded statement from the paper.",
    )
    paper = PublishedEntity(
        id="entity-paper",
        name="Readable Paper",
        type="Paper",
        summary="一篇用于测试正式知识投影的可读论文笔记。",
        evidence=[evidence],
        topic_slugs=["agent"],
    )
    method = PublishedEntity(
        id="entity-method",
        name="Tool Agent",
        type="Method",
        summary="使用外部工具执行多步骤任务的智能体方法。",
        evidence=[evidence],
        topic_slugs=["agent"],
    )
    relation = PublishedRelation(
        id="relation-presents",
        source_entity_id=paper.id,
        target_entity_id=method.id,
        type="PRESENTS",
        summary="论文提出了该工具智能体方法。",
        confidence=0.91,
        evidence=[evidence],
        topic_slugs=["agent"],
    )

    projector.upsert_entities([paper, method])
    projector.upsert_relations([relation])

    queries = "\n".join(query for query, _ in driver.calls)
    assert "KnowledgeEntityV2" in queries
    assert "KnowledgeTopicV2" in queries
    assert "KG_RELATION_V2" in queries
    assert "MATCH (node:KnowledgeEntityV2 {id: $entity_id})" in queries
    assert "ResearchEntity" not in queries
    assert "CO_OCCURS_WITH" not in queries
    assert "RELATED" not in queries


def test_qdrant_indexer_sanitizes_chunk_before_embedding_and_json_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _EmbeddingProvider:
        def embed_documents(self, texts):
            captured["embedded_texts"] = texts
            return [[0.0, 1.0]]

    class _QdrantClient:
        def __init__(self, **kwargs) -> None:
            captured["url"] = kwargs["url"]

        def collection_exists(self, collection_name: str) -> bool:
            return True

        def upsert(self, *, collection_name: str, points) -> None:
            captured["collection"] = collection_name
            captured["payload"] = points[0].payload

    monkeypatch.setattr(
        "app.retrieval.embeddings.get_embedding_provider", lambda settings: _EmbeddingProvider()
    )
    monkeypatch.setattr("qdrant_client.QdrantClient", _QdrantClient)
    settings = Settings(
        qdrant_url="http://qdrant.test",
        knowledge_qdrant_collection="safe-chunks",
        embedding_dimension=2,
    )
    chunk = DocumentChunk(
        id="chunk\ud835",
        paper_id="paper\ud835",
        title="Title \ud835",
        text="Evidence \ud835",
        chunk_index=0,
        token_count=2,
        metadata={"nested": ["value \ud835"]},
    )

    QdrantKnowledgeIndexer(settings).index([chunk])

    assert captured["embedded_texts"] == ["Evidence �"]
    assert captured["payload"] == {
        "id": "chunk�",
        "paper_id": "paper�",
        "title": "Title �",
        "text": "Evidence �",
        "chunk_index": 0,
        "token_count": 2,
        "source_tier": "unknown",
        "metadata": {"nested": ["value �"]},
    }
