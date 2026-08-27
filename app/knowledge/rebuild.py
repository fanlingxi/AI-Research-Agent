from __future__ import annotations

import argparse
import json
from typing import Literal, Protocol

from pydantic import BaseModel

from app.config.settings import Settings, get_settings
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.projector import Neo4jKnowledgeProjector, QdrantKnowledgeIndexer
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import PublishedEntity, PublishedRelation
from app.schemas.documents import DocumentChunk

ProjectionTarget = Literal["qdrant", "neo4j", "obsidian", "all"]


class ChunkProjectionRebuilder(Protocol):
    def replace(
        self,
        chunks: list[DocumentChunk],
        *,
        collection_slug: str | None = None,
    ) -> None: ...


class GraphProjectionRebuilder(Protocol):
    def replace_all(
        self,
        entities: list[PublishedEntity],
        relations: list[PublishedRelation],
    ) -> None: ...


class VaultProjectionRebuilder(Protocol):
    def render_topic(
        self,
        *,
        topic: str,
        topic_slug: str,
        entities: list[PublishedEntity],
        relations: list[PublishedRelation],
    ) -> object: ...


class ProjectionRebuildResult(BaseModel):
    target: ProjectionTarget
    collection_slug: str | None = None
    chunks: int = 0
    entities: int = 0
    relations: int = 0
    topics: int = 0


class KnowledgeProjectionRebuildService:
    """Rebuild derived projections exclusively from reviewed SQLite facts."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        indexer: ChunkProjectionRebuilder,
        projector: GraphProjectionRebuilder,
        vault_exporter: VaultProjectionRebuilder,
    ) -> None:
        self.repository = repository
        self.indexer = indexer
        self.projector = projector
        self.vault_exporter = vault_exporter

    def rebuild(
        self,
        *,
        target: ProjectionTarget = "all",
        collection_slug: str | None = None,
    ) -> ProjectionRebuildResult:
        selected_collection = (
            self.repository.get_collection(collection_slug) if collection_slug else None
        )
        selected_entities = self.repository.list_published_entities(collection_slug)
        selected_relations = self.repository.list_published_relations(collection_slug)
        result = ProjectionRebuildResult(
            target=target,
            collection_slug=collection_slug,
            entities=len(selected_entities),
            relations=len(selected_relations),
        )

        if target in {"qdrant", "all"}:
            chunks = self.repository.list_projection_chunks(collection_slug)
            self.indexer.replace(chunks, collection_slug=collection_slug)
            result.chunks = len(chunks)

        if target in {"neo4j", "all"}:
            # Neo4j relationships and shared entities can cross Collection boundaries.
            # Rebuild the complete managed graph even when the command was scoped, so a
            # Collection repair cannot delete facts belonging to another Collection.
            self.projector.replace_all(
                self.repository.list_published_entities(),
                self.repository.list_published_relations(),
            )

        if target in {"obsidian", "all"}:
            collections = (
                [selected_collection]
                if selected_collection is not None
                else self.repository.list_collections()
            )
            for collection in collections:
                self.vault_exporter.render_topic(
                    topic=collection.name,
                    topic_slug=collection.slug,
                    entities=self.repository.list_published_entities(collection.slug),
                    relations=self.repository.list_published_relations(collection.slug),
                )
            result.topics = len(collections)

        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 SQLite 正式事实重建 Qdrant、Neo4j 和 Obsidian 投影。"
    )
    parser.add_argument(
        "--target",
        choices=("qdrant", "neo4j", "obsidian", "all"),
        default="all",
        help="要重建的投影，默认为全部。",
    )
    parser.add_argument(
        "--collection",
        dest="collection_slug",
        help="仅重建指定 Collection；Neo4j 会安全地重建完整受管图。",
    )
    return parser


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    args = _parser().parse_args(argv)
    resolved_settings = settings or get_settings()
    repository = KnowledgeRepository(resolved_settings.knowledge_db_path)
    projector = Neo4jKnowledgeProjector(resolved_settings)
    service = KnowledgeProjectionRebuildService(
        repository,
        indexer=QdrantKnowledgeIndexer(resolved_settings),
        projector=projector,
        vault_exporter=KnowledgeVaultExporter(resolved_settings.knowledge_vault_path),
    )
    try:
        result = service.rebuild(
            target=args.target,
            collection_slug=args.collection_slug,
        )
    finally:
        projector.close()
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
