from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Protocol

from app.config.settings import Settings, get_settings
from app.schemas.graph import GraphEntity, GraphPath, GraphRelation


class GraphStore(Protocol):
    """Knowledge graph storage interface used by GraphRAG."""

    provider_name: str

    def upsert_graph(
        self,
        entities: list[GraphEntity],
        relations: list[GraphRelation],
    ) -> None:
        ...

    def retrieve_paths(
        self,
        query: str,
        max_hops: int = 2,
        limit: int = 5,
    ) -> list[GraphPath]:
        ...


@dataclass
class InMemoryGraphStore:
    """Local graph store for tests and offline demos."""

    provider_name: str = "memory"
    entities: dict[str, GraphEntity] = field(default_factory=dict)
    relations: dict[str, GraphRelation] = field(default_factory=dict)

    def upsert_graph(
        self,
        entities: list[GraphEntity],
        relations: list[GraphRelation],
    ) -> None:
        for entity in entities:
            self.entities[entity.id] = entity
        for relation in relations:
            self.relations[relation.id] = relation

    def retrieve_paths(
        self,
        query: str,
        max_hops: int = 2,
        limit: int = 5,
    ) -> list[GraphPath]:
        seeds = self._seed_entities(query)
        if not seeds:
            seeds = sorted(
                self.entities.values(),
                key=lambda entity: len(entity.source_chunk_ids),
                reverse=True,
            )[:limit]

        adjacency = self._adjacency()
        paths: list[GraphPath] = []
        for seed in seeds:
            paths.extend(self._walk(seed=seed, adjacency=adjacency, max_hops=max_hops))

        deduped: dict[str, GraphPath] = {}
        for path in paths:
            key = "|".join(sorted(relation.id for relation in path.relations))
            if key not in deduped or path.score > deduped[key].score:
                deduped[key] = path

        return sorted(
            deduped.values(),
            key=lambda path: path.score,
            reverse=True,
        )[:limit]

    def _seed_entities(self, query: str) -> list[GraphEntity]:
        query_lower = query.lower()
        scored: list[tuple[float, GraphEntity]] = []
        for entity in self.entities.values():
            name_lower = entity.name.lower()
            token_match = any(token in name_lower for token in query_lower.split())
            if name_lower in query_lower or token_match:
                score = 2.0 + len(entity.source_chunk_ids)
                scored.append((score, entity))

        return [
            entity
            for _, entity in sorted(scored, key=lambda item: item[0], reverse=True)
        ]

    def _adjacency(self) -> dict[str, list[tuple[GraphRelation, str]]]:
        adjacency: dict[str, list[tuple[GraphRelation, str]]] = defaultdict(list)
        for relation in self.relations.values():
            adjacency[relation.source_id].append((relation, relation.target_id))
            adjacency[relation.target_id].append((relation, relation.source_id))
        return adjacency

    def _walk(
        self,
        seed: GraphEntity,
        adjacency: dict[str, list[tuple[GraphRelation, str]]],
        max_hops: int,
    ) -> list[GraphPath]:
        queue = deque([(seed.id, [seed.id], [])])
        paths: list[GraphPath] = []

        while queue:
            current_id, node_ids, relation_ids = queue.popleft()
            if relation_ids:
                nodes = [self.entities[node_id] for node_id in node_ids]
                relations = [self.relations[relation_id] for relation_id in relation_ids]
                paths.append(
                    GraphPath(
                        nodes=nodes,
                        relations=relations,
                        score=sum(relation.weight for relation in relations) / len(relations),
                    )
                )

            if len(relation_ids) >= max_hops:
                continue

            for relation, next_id in adjacency.get(current_id, []):
                if next_id in node_ids:
                    continue
                queue.append((next_id, node_ids + [next_id], relation_ids + [relation.id]))

        return paths


class Neo4jGraphStore:
    """Neo4j graph store using a stable generic schema."""

    provider_name = "neo4j"

    def __init__(self, uri: str, username: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self._ensure_constraints()

    def close(self) -> None:
        self.driver.close()

    def upsert_graph(
        self,
        entities: list[GraphEntity],
        relations: list[GraphRelation],
    ) -> None:
        with self.driver.session() as session:
            for entity in entities:
                payload = entity.model_dump()
                payload["metadata"] = json.dumps(payload["metadata"], ensure_ascii=False)
                session.run(
                    """
                    MERGE (e:ResearchEntity {id: $id})
                    SET e.name = $name,
                        e.type = $type,
                        e.description = $description,
                        e.source_chunk_ids = $source_chunk_ids,
                        e.metadata = $metadata
                    """,
                    **payload,
                )

            for relation in relations:
                payload = relation.model_dump()
                payload["metadata"] = json.dumps(payload["metadata"], ensure_ascii=False)
                session.run(
                    """
                    MATCH (source:ResearchEntity {id: $source_id})
                    MATCH (target:ResearchEntity {id: $target_id})
                    MERGE (source)-[r:RELATED {id: $id}]->(target)
                    SET r.type = $type,
                        r.description = $description,
                        r.weight = $weight,
                        r.source_chunk_ids = $source_chunk_ids,
                        r.metadata = $metadata
                    """,
                    **payload,
                )

    def retrieve_paths(
        self,
        query: str,
        max_hops: int = 2,
        limit: int = 5,
    ) -> list[GraphPath]:
        max_hops = max(1, min(max_hops, 2))
        one_hop = self._retrieve_one_hop(query=query, limit=limit)
        if max_hops == 1 or len(one_hop) >= limit:
            return one_hop[:limit]

        two_hop = self._retrieve_two_hop(query=query, limit=limit - len(one_hop))
        return (one_hop + two_hop)[:limit]

    def _ensure_constraints(self) -> None:
        with self.driver.session() as session:
            session.run(
                """
                CREATE CONSTRAINT research_entity_id IF NOT EXISTS
                FOR (e:ResearchEntity) REQUIRE e.id IS UNIQUE
                """
            )

    def _retrieve_one_hop(self, query: str, limit: int) -> list[GraphPath]:
        with self.driver.session() as session:
            records = session.run(
                """
                MATCH (seed:ResearchEntity)
                WHERE toLower($query) CONTAINS toLower(seed.name)
                   OR toLower(seed.name) CONTAINS toLower($query)
                MATCH (seed)-[r:RELATED]-(neighbor:ResearchEntity)
                RETURN seed, r, neighbor
                ORDER BY r.weight DESC
                LIMIT $limit
                """,
                query=query,
                limit=limit,
            )
            return [
                GraphPath(
                    nodes=[
                        _entity_from_neo4j(record["seed"]),
                        _entity_from_neo4j(record["neighbor"]),
                    ],
                    relations=[
                        _relation_from_neo4j(
                            record["r"],
                            source_id=record["seed"].get("id", ""),
                            target_id=record["neighbor"].get("id", ""),
                        )
                    ],
                    score=float(record["r"].get("weight", 1.0)),
                )
                for record in records
            ]

    def _retrieve_two_hop(self, query: str, limit: int) -> list[GraphPath]:
        with self.driver.session() as session:
            records = session.run(
                """
                MATCH (seed:ResearchEntity)
                WHERE toLower($query) CONTAINS toLower(seed.name)
                   OR toLower(seed.name) CONTAINS toLower($query)
                MATCH (seed)-[r1:RELATED]-(mid:ResearchEntity)
                      -[r2:RELATED]-(neighbor:ResearchEntity)
                WHERE seed.id <> neighbor.id
                RETURN seed, r1, mid, r2, neighbor
                ORDER BY (r1.weight + r2.weight) DESC
                LIMIT $limit
                """,
                query=query,
                limit=limit,
            )
            return [
                GraphPath(
                    nodes=[
                        _entity_from_neo4j(record["seed"]),
                        _entity_from_neo4j(record["mid"]),
                        _entity_from_neo4j(record["neighbor"]),
                    ],
                    relations=[
                        _relation_from_neo4j(
                            record["r1"],
                            source_id=record["seed"].get("id", ""),
                            target_id=record["mid"].get("id", ""),
                        ),
                        _relation_from_neo4j(
                            record["r2"],
                            source_id=record["mid"].get("id", ""),
                            target_id=record["neighbor"].get("id", ""),
                        ),
                    ],
                    score=float(record["r1"].get("weight", 1.0))
                    + float(record["r2"].get("weight", 1.0)),
                )
                for record in records
            ]


def build_graph_store(
    provider: str | None = None,
    settings: Settings | None = None,
) -> GraphStore:
    settings = settings or get_settings()
    selected_provider = provider or settings.graph_store_provider
    if selected_provider == "neo4j":
        return Neo4jGraphStore(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password,
        )
    return InMemoryGraphStore()


def _entity_from_neo4j(node) -> GraphEntity:
    payload = dict(node)
    return GraphEntity(
        id=payload.get("id", ""),
        name=payload.get("name", ""),
        type=payload.get("type", "Concept"),
        description=payload.get("description", ""),
        source_chunk_ids=payload.get("source_chunk_ids", []),
        metadata=_decode_metadata(payload.get("metadata", {})),
    )


def _relation_from_neo4j(
    relation,
    source_id: str,
    target_id: str,
) -> GraphRelation:
    payload = dict(relation)
    return GraphRelation(
        id=payload.get("id", ""),
        source_id=source_id,
        target_id=target_id,
        type=payload.get("type", "RELATED"),
        description=payload.get("description", ""),
        weight=float(payload.get("weight", 1.0)),
        source_chunk_ids=payload.get("source_chunk_ids", []),
        metadata=_decode_metadata(payload.get("metadata", {})),
    )


def _decode_metadata(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
