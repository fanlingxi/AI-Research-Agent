from __future__ import annotations

import json
import uuid
from typing import Protocol

from app.config.settings import Settings, get_settings
from app.knowledge.schemas import PublishedEntity, PublishedRelation
from app.schemas.documents import DocumentChunk
from app.tools.text_safety import sanitize_json_value


class KnowledgeProjector(Protocol):
    def upsert_entities(self, entities: list[PublishedEntity]) -> None: ...

    def upsert_relations(self, relations: list[PublishedRelation]) -> None: ...


class Neo4jKnowledgeProjector:
    """Write published knowledge into the stable physical labels used by current data."""

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        self._constraints_ready = False

    def close(self) -> None:
        self.driver.close()

    def _ensure_constraints(self) -> None:
        if self._constraints_ready:
            return
        with self.driver.session() as session:
            session.run(
                "CREATE CONSTRAINT knowledge_entity_v2_id IF NOT EXISTS "
                "FOR (node:KnowledgeEntityV2) REQUIRE node.id IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT knowledge_topic_v2_slug IF NOT EXISTS "
                "FOR (topic:KnowledgeTopicV2) REQUIRE topic.slug IS UNIQUE"
            )
        self._constraints_ready = True

    def upsert_entities(self, entities: list[PublishedEntity]) -> None:
        if not entities:
            return
        self._ensure_constraints()
        with self.driver.session() as session:
            for entity in entities:
                session.run(
                    """
                    MERGE (node:KnowledgeEntityV2 {id: $id})
                    SET node.name = $name,
                        node.entity_type = $entity_type,
                        node.summary = $summary,
                        node.aliases = $aliases,
                        node.evidence_json = $evidence_json,
                        node.metadata_json = $metadata_json
                    """,
                    id=entity.id,
                    name=entity.name,
                    entity_type=entity.type,
                    summary=entity.summary,
                    aliases=entity.aliases,
                    evidence_json=_dump(entity.evidence),
                    metadata_json=_dump(entity.metadata),
                )
                collection_slugs = entity.collection_slugs or entity.topic_slugs
                session.run(
                    """
                    MATCH (node:KnowledgeEntityV2 {id: $entity_id})
                    OPTIONAL MATCH (old:KnowledgeTopicV2)-[edge:INCLUDES_V2]->(node)
                    WHERE NOT old.slug IN $collection_slugs
                    DELETE edge
                    """,
                    entity_id=entity.id,
                    collection_slugs=collection_slugs,
                )
                for topic_slug in collection_slugs:
                    session.run(
                        """
                        MERGE (topic:KnowledgeTopicV2 {slug: $slug})
                        WITH topic
                        MATCH (node:KnowledgeEntityV2 {id: $entity_id})
                        MERGE (topic)-[:INCLUDES_V2]->(node)
                        """,
                        slug=topic_slug,
                        entity_id=entity.id,
                    )

    def upsert_relations(self, relations: list[PublishedRelation]) -> None:
        if not relations:
            return
        self._ensure_constraints()
        with self.driver.session() as session:
            for relation in relations:
                result = session.run(
                    """
                    MATCH (source:KnowledgeEntityV2 {id: $source_id})
                    MATCH (target:KnowledgeEntityV2 {id: $target_id})
                    MERGE (source)-[edge:KG_RELATION_V2 {id: $id}]->(target)
                    SET edge.relation_type = $relation_type,
                        edge.summary = $summary,
                        edge.confidence = $confidence,
                        edge.evidence_json = $evidence_json,
                        edge.metadata_json = $metadata_json
                    RETURN edge.id AS projected_relation_id
                    """,
                    id=relation.id,
                    source_id=relation.source_entity_id,
                    target_id=relation.target_entity_id,
                    relation_type=relation.type,
                    summary=relation.summary,
                    confidence=relation.confidence,
                    evidence_json=_dump(relation.evidence),
                    metadata_json=_dump(relation.metadata),
                )
                if result.single() is None:
                    raise RuntimeError(
                        "Neo4j relation projection failed because the source or target "
                        f"entity is missing: relation={relation.id}, "
                        f"source={relation.source_entity_id}, target={relation.target_entity_id}"
                    )

    def replace_all(
        self,
        entities: list[PublishedEntity],
        relations: list[PublishedRelation],
    ) -> None:
        """Explicitly rebuild the managed Neo4j labels from SQLite facts."""

        with self.driver.session() as session:
            session.run("MATCH (node:KnowledgeEntityV2) DETACH DELETE node")
            session.run("MATCH (topic:KnowledgeTopicV2) DETACH DELETE topic")
        self.upsert_entities(entities)
        self.upsert_relations(relations)


class QdrantKnowledgeIndexer:
    """Index source chunks in the configured Qdrant store without a memory fallback."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def index(self, chunks: list[DocumentChunk]) -> None:
        if not chunks:
            return
        from qdrant_client import QdrantClient, models

        from app.retrieval.embeddings import get_embedding_provider

        safe_chunks = [
            DocumentChunk.model_validate(sanitize_json_value(chunk.model_dump()))
            for chunk in chunks
        ]
        embeddings = get_embedding_provider(self.settings).embed_documents(
            [chunk.text for chunk in safe_chunks]
        )
        client = QdrantClient(url=self.settings.qdrant_url)
        if not client.collection_exists(self.settings.knowledge_qdrant_collection):
            client.create_collection(
                collection_name=self.settings.knowledge_qdrant_collection,
                vectors_config=models.VectorParams(
                    size=self.settings.embedding_dimension,
                    distance=models.Distance.COSINE,
                ),
            )
        client.upsert(
            collection_name=self.settings.knowledge_qdrant_collection,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, chunk.id)),
                    vector=embedding,
                    payload=chunk.model_dump(),
                )
                for chunk, embedding in zip(safe_chunks, embeddings, strict=True)
            ],
        )

    def replace(
        self,
        chunks: list[DocumentChunk],
        *,
        collection_slug: str | None = None,
    ) -> None:
        """Explicitly replace all or one Collection's managed vector points."""

        from qdrant_client import QdrantClient, models

        client = QdrantClient(url=self.settings.qdrant_url)
        exists = client.collection_exists(self.settings.knowledge_qdrant_collection)
        if collection_slug is None:
            if exists:
                client.delete_collection(self.settings.knowledge_qdrant_collection)
        elif exists:
            client.delete(
                collection_name=self.settings.knowledge_qdrant_collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="metadata.collection_slugs",
                                match=models.MatchValue(value=collection_slug),
                            )
                        ]
                    )
                ),
                wait=True,
            )
        self.index(chunks)


def _dump(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return json.dumps(
        value,
        ensure_ascii=False,
        default=lambda item: item.model_dump() if hasattr(item, "model_dump") else str(item),
    )
