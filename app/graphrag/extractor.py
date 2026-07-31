from __future__ import annotations

import hashlib
import itertools
import re
from collections import defaultdict

from app.schemas.documents import DocumentChunk
from app.schemas.graph import GraphBuildResult, GraphEntity, GraphRelation

ENTITY_PATTERNS: dict[str, list[str]] = {
    "Concept": [
        "GraphRAG",
        "knowledge graph",
        "retrieval augmented generation",
        "RAG",
        "multi-hop reasoning",
        "citation",
        "entity",
        "relation",
        "evidence",
    ],
    "Method": [
        "vector search",
        "graph traversal",
        "chunking",
        "embedding",
        "entity extraction",
        "relation extraction",
        "evaluation",
        "reflection",
    ],
    "Dataset": [
        "arXiv",
        "Semantic Scholar",
        "OpenReview",
        "benchmark",
        "dataset",
    ],
    "Metric": [
        "faithfulness",
        "recall",
        "precision",
        "citation coverage",
        "retrieval quality",
    ],
    "Task": [
        "literature review",
        "scientific discovery",
        "research report",
        "question answering",
    ],
}


class GraphExtractor:
    """Heuristic entity and relation extractor for the local GraphRAG demo.

    The extractor intentionally avoids mandatory LLM calls so that GraphRAG can
    be validated offline. Later phases can replace this with prompt-based or
    model-based extraction behind the same output schema.
    """

    def __init__(self, max_entities_per_chunk: int = 8) -> None:
        self.max_entities_per_chunk = max_entities_per_chunk

    def extract(self, chunks: list[DocumentChunk]) -> GraphBuildResult:
        entity_map: dict[str, GraphEntity] = {}
        relation_map: dict[str, GraphRelation] = {}

        for chunk in chunks:
            paper = self._paper_entity(chunk)
            entity_map[paper.id] = self._merge_entity(entity_map.get(paper.id), paper)

            chunk_entities = self._extract_chunk_entities(chunk)
            for entity in chunk_entities:
                entity_map[entity.id] = self._merge_entity(entity_map.get(entity.id), entity)

                relation = self._relation(
                    source_id=paper.id,
                    target_id=entity.id,
                    relation_type="DISCUSSES",
                    source_chunk_id=chunk.id,
                    description=f"{paper.name} discusses {entity.name}.",
                )
                relation_map[relation.id] = self._merge_relation(
                    relation_map.get(relation.id),
                    relation,
                )

            for left, right in itertools.combinations(chunk_entities, 2):
                relation = self._relation(
                    source_id=left.id,
                    target_id=right.id,
                    relation_type="CO_OCCURS_WITH",
                    source_chunk_id=chunk.id,
                    description=f"{left.name} co-occurs with {right.name}.",
                    weight=0.5,
                )
                relation_map[relation.id] = self._merge_relation(
                    relation_map.get(relation.id),
                    relation,
                )

        return GraphBuildResult(
            entities=sorted(entity_map.values(), key=lambda entity: entity.id),
            relations=sorted(relation_map.values(), key=lambda relation: relation.id),
            metadata={"chunks": len(chunks)},
        )

    def _paper_entity(self, chunk: DocumentChunk) -> GraphEntity:
        return GraphEntity(
            id=f"paper:{self._stable_key(chunk.paper_id)}",
            name=chunk.title,
            type="Paper",
            description=f"Paper or source document: {chunk.title}",
            source_chunk_ids=[chunk.id],
            metadata={
                "paper_id": chunk.paper_id,
                "source": chunk.metadata.get("source"),
                "url": chunk.metadata.get("url"),
                "year": chunk.metadata.get("year"),
            },
        )

    def _extract_chunk_entities(self, chunk: DocumentChunk) -> list[GraphEntity]:
        text = f"{chunk.title}\n{chunk.text}"
        candidates: list[GraphEntity] = []

        for entity_type, phrases in ENTITY_PATTERNS.items():
            for phrase in phrases:
                if re.search(rf"\b{re.escape(phrase)}\b", text, flags=re.IGNORECASE):
                    candidates.append(
                        self._entity(
                            name=self._canonical_name(phrase),
                            entity_type=entity_type,
                            source_chunk_id=chunk.id,
                        )
                    )

        for name in self._capitalized_terms(text):
            candidates.append(
                self._entity(
                    name=name,
                    entity_type="Concept",
                    source_chunk_id=chunk.id,
                )
            )

        deduped: dict[str, GraphEntity] = {}
        for entity in candidates:
            deduped[entity.id] = self._merge_entity(deduped.get(entity.id), entity)

        return list(deduped.values())[: self.max_entities_per_chunk]

    def _entity(self, name: str, entity_type: str, source_chunk_id: str) -> GraphEntity:
        normalized = self._normalize_name(name)
        return GraphEntity(
            id=f"{entity_type.lower()}:{self._stable_key(normalized)}",
            name=name,
            type=entity_type,  # type: ignore[arg-type]
            description=f"{entity_type}: {name}",
            source_chunk_ids=[source_chunk_id],
        )

    def _relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        source_chunk_id: str,
        description: str,
        weight: float = 1.0,
    ) -> GraphRelation:
        relation_key = "|".join([source_id, relation_type, target_id])
        return GraphRelation(
            id=f"rel:{self._stable_key(relation_key)}",
            source_id=source_id,
            target_id=target_id,
            type=relation_type,
            description=description,
            weight=weight,
            source_chunk_ids=[source_chunk_id],
        )

    def _merge_entity(
        self,
        existing: GraphEntity | None,
        incoming: GraphEntity,
    ) -> GraphEntity:
        if existing is None:
            return incoming

        source_chunk_ids = sorted(set(existing.source_chunk_ids + incoming.source_chunk_ids))
        metadata = {**existing.metadata, **incoming.metadata}
        existing.source_chunk_ids = source_chunk_ids
        existing.metadata = metadata
        if not existing.description and incoming.description:
            existing.description = incoming.description
        return existing

    def _merge_relation(
        self,
        existing: GraphRelation | None,
        incoming: GraphRelation,
    ) -> GraphRelation:
        if existing is None:
            return incoming

        existing.source_chunk_ids = sorted(
            set(existing.source_chunk_ids + incoming.source_chunk_ids)
        )
        existing.weight += incoming.weight
        return existing

    def _capitalized_terms(self, text: str) -> list[str]:
        pattern = r"\b(?:[A-Z][A-Za-z0-9]+(?:[-\s][A-Z][A-Za-z0-9]+){0,3})\b"
        counts: dict[str, int] = defaultdict(int)
        for match in re.findall(pattern, text):
            cleaned = match.strip()
            if self._is_metadata_noise(cleaned):
                continue
            counts[cleaned] += 1

        return [
            name
            for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ][:4]

    def _canonical_name(self, phrase: str) -> str:
        canonical = {
            "rag": "RAG",
            "graphrag": "GraphRAG",
            "arxiv": "arXiv",
        }
        return canonical.get(phrase.lower(), phrase)

    def _is_metadata_noise(self, value: str) -> bool:
        value_lower = value.lower()
        blocked = {
            "title",
            "authors",
            "source",
            "abstract",
            "year",
            "unknown authors",
            "ai-research-agent demo",
        }
        return len(value) < 4 or value_lower in blocked or value_lower.endswith("demo")

    def _normalize_name(self, name: str) -> str:
        return re.sub(r"\s+", " ", name.strip().lower())

    def _stable_key(self, raw: str) -> str:
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
