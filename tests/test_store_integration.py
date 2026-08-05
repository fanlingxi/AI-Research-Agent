from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.config.settings import Settings
from app.knowledge.projector import Neo4jKnowledgeProjector, QdrantKnowledgeIndexer
from app.knowledge.schemas import EvidenceSpan, PublishedEntity, PublishedRelation
from app.schemas.documents import DocumentChunk

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORE_INTEGRATION") != "1",
    reason="set RUN_STORE_INTEGRATION=1 with real Qdrant and Neo4j containers",
)


def test_real_qdrant_indexes_formal_pdf_chunk_idempotently() -> None:
    from qdrant_client import QdrantClient

    collection = f"knowledge_core_test_{uuid4().hex}"
    settings = Settings(
        qdrant_url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        knowledge_qdrant_collection=collection,
        embedding_provider="hash",
        embedding_dimension=64,
    )
    client = QdrantClient(url=settings.qdrant_url)
    chunk = DocumentChunk(
        id="paper:integration:page:1:chunk:0",
        paper_id="paper:integration",
        title="Integration Paper",
        text="Durable formal evidence is indexed in Qdrant before review candidates exist.",
        chunk_index=0,
        token_count=11,
        source_tier="primary_fulltext",
        metadata={"page_start": 1, "page_end": 1},
    )
    try:
        indexer = QdrantKnowledgeIndexer(settings)
        indexer.index([chunk])
        indexer.index([chunk])
        assert client.count(collection_name=collection, exact=True).count == 1
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)


def test_real_neo4j_projection_is_idempotent() -> None:
    from neo4j import GraphDatabase

    suffix = uuid4().hex
    configured = Settings()
    settings = Settings(
        neo4j_uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        neo4j_username=os.getenv("NEO4J_USERNAME", configured.neo4j_username),
        neo4j_password=os.getenv("NEO4J_PASSWORD") or configured.neo4j_password,
    )
    evidence = EvidenceSpan(
        paper_id=f"paper:{suffix}",
        chunk_id=f"paper:{suffix}:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="Formal evidence for an idempotent Neo4j integration projection.",
    )
    source = PublishedEntity(
        id=f"integration-source-{suffix}",
        name="Integration Source",
        type="Paper",
        summary="用于真实 Neo4j 投影集成测试的来源论文节点。",
        evidence=[evidence],
        topic_slugs=[f"integration-{suffix}"],
    )
    target = PublishedEntity(
        id=f"integration-target-{suffix}",
        name="Integration Target",
        type="Concept",
        summary="用于真实 Neo4j 投影集成测试的目标概念节点。",
        evidence=[evidence],
        topic_slugs=[f"integration-{suffix}"],
    )
    relation = PublishedRelation(
        id=f"integration-relation-{suffix}",
        source_entity_id=source.id,
        target_entity_id=target.id,
        type="SUPPORTS",
        summary="来源论文以正式证据支持目标概念。",
        confidence=0.9,
        evidence=[evidence],
        topic_slugs=[f"integration-{suffix}"],
    )
    projector = Neo4jKnowledgeProjector(settings)
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password),
    )
    projected = False
    try:
        projector.upsert_entities([source, target])
        projector.upsert_relations([relation])
        projected = True
        projector.upsert_entities([source, target])
        projector.upsert_relations([relation])
        with driver.session() as session:
            count = session.run(
                """
                MATCH (:KnowledgeEntityV2 {id: $source_id})
                      -[edge:KG_RELATION_V2 {id: $relation_id}]->
                      (:KnowledgeEntityV2 {id: $target_id})
                RETURN count(edge) AS count
                """,
                source_id=source.id,
                target_id=target.id,
                relation_id=relation.id,
            ).single()["count"]
        assert count == 1
    finally:
        if projected:
            with driver.session() as session:
                session.run(
                    "MATCH ()-[edge:KG_RELATION_V2 {id: $relation_id}]->() DELETE edge",
                    relation_id=relation.id,
                )
                session.run(
                    "MATCH (node:KnowledgeEntityV2) WHERE node.id IN $ids DETACH DELETE node",
                    ids=[source.id, target.id],
                )
                session.run(
                    "MATCH (topic:KnowledgeTopicV2 {slug: $slug}) DETACH DELETE topic",
                    slug=f"integration-{suffix}",
                )
        projector.close()
        driver.close()
