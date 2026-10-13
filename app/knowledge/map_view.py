"""Read-only knowledge map projection built directly from the SQLite Core.

The map is an exploration aid: it never writes, and it only reads formal
(``published``) Core entities, relations, claims and evidence by default.
Draft review candidates are surfaced exclusively when a caller opts in, and
are always marked ``status="pending"`` so the UI can keep them visually
separate from approved knowledge.  Neo4j/Qdrant projections are not consulted.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import PurePath
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.persistence.sqlite import SQLiteDatabase

NodeStatus = Literal["published", "pending"]
NodeKind = Literal["entity", "source"]
EdgeKind = Literal["relation", "evidence"]

SOURCE_NODE_PREFIX = "source:"
EVIDENCE_EDGE_TYPE = "EVIDENCE"

_CURRENT_CANDIDATE_SQL = """NOT EXISTS (
    SELECT 1 FROM documents d JOIN sources s ON s.id = d.source_id
    WHERE d.id = json_extract(c.payload_json, '$.evidence.paper_id')
      AND s.superseded_by_source_id IS NOT NULL
)"""


class MapNode(BaseModel):
    id: str
    label: str
    type: str
    kind: NodeKind = "entity"
    status: NodeStatus = "published"
    degree: int = 0
    evidence_count: int = 0
    paper_count: int = 0
    score: float = 0.0
    matched: bool = False


class MapEdge(BaseModel):
    id: str
    source: str
    target: str
    type: str
    kind: EdgeKind = "relation"
    status: NodeStatus = "published"
    weight: int = 1
    label: str = ""


class MapStats(BaseModel):
    total_nodes: int
    total_edges: int
    shown_nodes: int
    shown_edges: int
    pending_nodes: int
    pending_edges: int
    truncated: bool
    limit: int


class KnowledgeMapGraph(BaseModel):
    nodes: list[MapNode]
    edges: list[MapEdge]
    stats: MapStats
    type_counts: dict[str, int] = Field(default_factory=dict)


class _Node:
    __slots__ = ("node", "aliases", "documents")

    def __init__(self, node: MapNode, aliases: list[str], documents: dict[str, int]) -> None:
        self.node = node
        self.aliases = aliases
        # document_id -> evidence count supporting this node from that document
        self.documents = documents


class KnowledgeMapReader:
    """Assemble bounded, filterable graph views and evidence drill-downs."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    # ------------------------------------------------------------------ graph
    def graph(
        self,
        *,
        collection: str | None = None,
        entity_types: list[str] | None = None,
        query: str | None = None,
        include_pending: bool = False,
        limit: int = 300,
    ) -> KnowledgeMapGraph:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            nodes, edges = self._published(connection, collection)
            if include_pending:
                self._add_pending(connection, collection, nodes, edges)
            paper_titles = self._paper_titles(connection, nodes)

        self._add_evidence_edges(nodes, edges, paper_titles)
        type_counts: dict[str, int] = defaultdict(int)
        for item in nodes.values():
            type_counts[item.node.type] += 1

        allowed_types = {item for item in entity_types or [] if item}
        if allowed_types:
            nodes = {key: item for key, item in nodes.items() if item.node.type in allowed_types}
        edges = [edge for edge in edges if edge.source in nodes and edge.target in nodes]

        needle = _normalize(query or "")
        if needle:
            matched = {
                key
                for key, item in nodes.items()
                if needle in _normalize(item.node.label)
                or any(needle in _normalize(alias) for alias in item.aliases)
            }
            neighbours = set(matched)
            for edge in edges:
                if edge.source in matched:
                    neighbours.add(edge.target)
                if edge.target in matched:
                    neighbours.add(edge.source)
            nodes = {key: item for key, item in nodes.items() if key in neighbours}
            for key in matched:
                nodes[key].node.matched = True
            edges = [edge for edge in edges if edge.source in nodes and edge.target in nodes]

        degree: dict[str, int] = defaultdict(int)
        for edge in edges:
            degree[edge.source] += 1
            degree[edge.target] += 1
        for key, item in nodes.items():
            item.node.degree = degree[key]
            item.node.paper_count = len(item.documents)
            item.node.score = float(item.node.degree + item.node.evidence_count)

        ranked = sorted(
            nodes.values(),
            key=lambda item: (
                not item.node.matched,
                item.node.status != "published",
                -item.node.score,
                item.node.label,
            ),
        )
        kept = [item.node for item in ranked[:limit]]
        kept_ids = {item.id for item in kept}
        kept_edges = [edge for edge in edges if edge.source in kept_ids and edge.target in kept_ids]
        return KnowledgeMapGraph(
            nodes=kept,
            edges=kept_edges,
            stats=MapStats(
                total_nodes=len(nodes),
                total_edges=len(edges),
                shown_nodes=len(kept),
                shown_edges=len(kept_edges),
                pending_nodes=sum(item.status == "pending" for item in kept),
                pending_edges=sum(edge.status == "pending" for edge in kept_edges),
                truncated=len(kept) < len(nodes),
                limit=limit,
            ),
            type_counts=dict(sorted(type_counts.items())),
        )

    def _published(
        self, connection: sqlite3.Connection, collection: str | None
    ) -> tuple[dict[str, _Node], list[MapEdge]]:
        scope_sql = ""
        params: tuple[Any, ...] = ()
        if collection:
            scope_sql = """
              AND EXISTS (
                SELECT 1 FROM collection_memberships m
                WHERE m.aggregate_type = 'entity'
                  AND m.aggregate_id = COALESCE(e.legacy_id, e.id)
                  AND m.collection_slug = ?
              )
            """
            params = (collection,)
        rows = connection.execute(
            f"""
            SELECT e.id, COALESCE(e.legacy_id, e.id) AS node_id, e.name, e.entity_type,
                   e.properties_json
            FROM entities e
            WHERE e.status = 'published' {scope_sql}
            """,
            params,
        ).fetchall()
        core_to_node = {str(row["id"]): str(row["node_id"]) for row in rows}
        documents = self._entity_documents(connection)
        nodes: dict[str, _Node] = {}
        for row in rows:
            node_id = str(row["node_id"])
            properties = _loads(row["properties_json"])
            docs = documents.get(str(row["id"]), {})
            if not docs:
                continue
            nodes[node_id] = _Node(
                MapNode(
                    id=node_id,
                    label=str(row["name"]),
                    type=str(row["entity_type"]),
                    evidence_count=sum(docs.values()),
                ),
                [str(alias) for alias in properties.get("aliases") or []],
                dict(docs),
            )

        relation_scope = ""
        relation_params: tuple[Any, ...] = ()
        if collection:
            relation_scope = """
              AND EXISTS (
                SELECT 1 FROM collection_memberships m
                WHERE m.aggregate_type = 'relation'
                  AND m.aggregate_id = COALESCE(r.legacy_id, r.id)
                  AND m.collection_slug = ?
              )
            """
            relation_params = (collection,)
        relation_rows = connection.execute(
            f"""
            SELECT r.id, COALESCE(r.legacy_id, r.id) AS edge_id, r.source_entity_id,
                   r.target_entity_id, r.relation_type,
                   (SELECT c.statement FROM claims c
                    JOIN claim_evidence_links l ON l.claim_id = c.id
                    JOIN evidences ev ON ev.id = l.evidence_id
                    JOIN sources s ON s.id = ev.source_id
                      AND s.superseded_by_source_id IS NULL
                    WHERE c.relation_id = r.id AND c.status = 'published'
                    ORDER BY c.created_at LIMIT 1) AS statement
            FROM relations r
            WHERE r.status = 'published' {relation_scope}
            """,
            relation_params,
        ).fetchall()
        relation_evidence: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for evidence in connection.execute(
            """SELECT c.relation_id, ev.chunk_id, ev.quote FROM claims c
               JOIN relations r ON r.id = c.relation_id
               JOIN claim_evidence_links l ON l.claim_id = c.id
               JOIN evidences ev ON ev.id = l.evidence_id
               JOIN sources s ON s.id = ev.source_id AND s.superseded_by_source_id IS NULL
               LEFT JOIN chunks ch ON ch.id = ev.chunk_id
               WHERE c.status = 'published' AND r.status = 'published'
                 AND (? IS NULL OR EXISTS (
                   SELECT 1 FROM collection_memberships m
                   JOIN ingestion_documents d ON d.ingestion_id = m.ingestion_id
                   WHERE m.aggregate_type = 'relation'
                     AND m.aggregate_id = COALESCE(r.legacy_id, r.id)
                     AND m.collection_slug = ? AND d.document_id = ch.document_id))""",
            (collection, collection),
        ):
            relation_evidence[str(evidence["relation_id"])].add(
                (evidence["chunk_id"], evidence["quote"]))
        edges: list[MapEdge] = []
        grouped: dict[tuple[str, str, str], MapEdge] = {}
        evidence_keys: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
        for row in relation_rows:
            source = core_to_node.get(str(row["source_entity_id"]))
            target = core_to_node.get(str(row["target_entity_id"]))
            current = relation_evidence[str(row["id"])]
            if source not in nodes or target not in nodes or not current:
                continue
            key = (source, target, str(row["relation_type"]))
            evidence_keys[key].update(current)
            if key not in grouped:
                edge = MapEdge(id=str(row["edge_id"]), source=source, target=target,
                               type=str(row["relation_type"]), label=str(row["statement"] or ""))
                grouped[key] = edge
                edges.append(edge)
            grouped[key].weight = max(1, len(evidence_keys[key]))
        return nodes, edges

    @staticmethod
    def _entity_documents(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
        rows = connection.execute(
            f"""
            SELECT c.entity_id, {_DOCUMENT_ID_SQL} AS document_id,
                   COUNT(DISTINCT ev.id) AS evidence_count
            FROM claims c
            JOIN claim_evidence_links l ON l.claim_id = c.id
            JOIN evidences ev ON ev.id = l.evidence_id
            JOIN sources s ON s.id = ev.source_id AND s.superseded_by_source_id IS NULL
            LEFT JOIN chunks ch ON ch.id = ev.chunk_id
            WHERE c.status = 'published' AND c.entity_id IS NOT NULL
            GROUP BY c.entity_id, document_id
            """
        ).fetchall()
        result: dict[str, dict[str, int]] = defaultdict(dict)
        for row in rows:
            if row["document_id"]:
                result[str(row["entity_id"])][str(row["document_id"])] = int(row["evidence_count"])
        return result

    def _add_pending(
        self,
        connection: sqlite3.Connection,
        collection: str | None,
        nodes: dict[str, _Node],
        edges: list[MapEdge],
    ) -> None:
        canonical = {
            str(row["id"]): str(row["canonical_id"])
            for row in connection.execute(
                "SELECT id, canonical_id FROM candidates "
                "WHERE kind = 'entity' AND canonical_id IS NOT NULL"
            )
        }
        for row in self._draft_candidates(connection, collection, "entity"):
            payload = _loads(row["payload_json"])
            evidence = _candidate_evidence(payload)
            documents: dict[str, int] = defaultdict(int)
            for item in evidence:
                if item.get("paper_id"):
                    documents[str(item["paper_id"])] += 1
            nodes[str(row["id"])] = _Node(
                MapNode(
                    id=str(row["id"]),
                    label=str(payload.get("name") or row["id"]),
                    type=str(payload.get("type") or "Concept"),
                    status="pending",
                    evidence_count=len(evidence),
                ),
                [str(alias) for alias in payload.get("aliases") or []],
                dict(documents),
            )
        for row in self._draft_candidates(connection, collection, "relation"):
            payload = _loads(row["payload_json"])
            source = _resolve_candidate(payload.get("source_candidate_id"), canonical, nodes)
            target = _resolve_candidate(payload.get("target_candidate_id"), canonical, nodes)
            if source is None or target is None or source == target:
                continue
            edges.append(
                MapEdge(
                    id=str(row["id"]),
                    source=source,
                    target=target,
                    type=str(payload.get("type") or "RELATED"),
                    status="pending",
                    weight=max(1, len(_candidate_evidence(payload))),
                    label=str(payload.get("summary") or ""),
                )
            )

    @staticmethod
    def _draft_candidates(
        connection: sqlite3.Connection, collection: str | None, kind: str
    ) -> list[sqlite3.Row]:
        scope_sql = ""
        params: list[Any] = [kind]
        if collection:
            scope_sql = "AND ic.collection_slug = ?"
            params.append(collection)
        return connection.execute(
            f"""
            SELECT c.id, c.payload_json FROM candidates c
            LEFT JOIN ingestion_collections ic ON ic.ingestion_id = c.ingestion_id
            WHERE c.status = 'draft' AND c.kind = ? {scope_sql}
              AND {_CURRENT_CANDIDATE_SQL}
            ORDER BY c.created_at, c.id
            """,
            tuple(params),
        ).fetchall()

    @staticmethod
    def _paper_titles(
        connection: sqlite3.Connection, nodes: dict[str, _Node]
    ) -> dict[str, tuple[str | None, str]]:
        """Map document_id -> (paper node id or None, display title)."""

        documents = {
            str(row["id"]): _document_title(row)
            for row in connection.execute(
                """
                SELECT d.id, d.title, d.local_path, d.source_url, s.title AS source_title
                FROM documents d LEFT JOIN sources s ON s.id = d.source_id
                """
            )
        }
        result: dict[str, tuple[str | None, str]] = {}
        for item in nodes.values():
            if item.node.type != "Paper" or item.node.status != "published":
                continue
            for document_id in item.documents:
                result.setdefault(document_id, (item.node.id, item.node.label))
        for document_id, title in documents.items():
            result.setdefault(document_id, (None, title))
        return result

    @staticmethod
    def _add_evidence_edges(
        nodes: dict[str, _Node],
        edges: list[MapEdge],
        paper_titles: dict[str, tuple[str | None, str]],
    ) -> None:
        """Link concepts to the papers whose quotes support them (derived, read-only)."""

        linked = {frozenset((edge.source, edge.target)) for edge in edges}
        for item in list(nodes.values()):
            if item.node.type == "Paper":
                continue
            for document_id, count in item.documents.items():
                paper_node, title = paper_titles.get(document_id, (None, document_id))
                if paper_node is None:
                    paper_node = f"{SOURCE_NODE_PREFIX}{document_id}"
                    if paper_node not in nodes:
                        nodes[paper_node] = _Node(
                            MapNode(id=paper_node, label=title, type="Paper", kind="source"),
                            [],
                            {document_id: 0},
                        )
                if paper_node not in nodes:
                    continue
                pair = frozenset((paper_node, item.node.id))
                if pair in linked:
                    continue
                linked.add(pair)
                edges.append(
                    MapEdge(
                        id=f"evidence:{paper_node}:{item.node.id}",
                        source=paper_node,
                        target=item.node.id,
                        type=EVIDENCE_EDGE_TYPE,
                        kind="evidence",
                        status=item.node.status,
                        weight=max(1, count),
                        label=f"《{title}》提供 {count} 条证据",
                    )
                )

    # ----------------------------------------------------------------- detail
    def node_detail(self, node_id: str, *, include_pending: bool = False) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            documents = self._documents(connection)
            if node_id.startswith(SOURCE_NODE_PREFIX):
                return self._source_detail(connection, node_id, documents)
            row = connection.execute(
                """
                SELECT id, COALESCE(legacy_id, id) AS node_id, name, entity_type,
                       properties_json
                FROM entities
                WHERE status = 'published' AND (legacy_id = ? OR id = ?)
                """,
                (node_id, node_id),
            ).fetchone()
            if row is not None:
                return self._entity_detail(connection, row, documents, include_pending)
            if include_pending:
                candidate = connection.execute(
                    "SELECT c.* FROM candidates c "
                    "WHERE c.id = ? AND c.kind = 'entity' AND c.status = 'draft' "
                    f"AND {_CURRENT_CANDIDATE_SQL}",
                    (node_id,),
                ).fetchone()
                if candidate is not None:
                    return self._candidate_detail(connection, candidate, documents)
        raise KeyError(node_id)

    def _documents(self, connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        papers: dict[str, dict[str, Any]] = {}
        rows = connection.execute(
            f"""
            SELECT COALESCE(e.legacy_id, e.id) AS node_id, e.name,
                   {_DOCUMENT_ID_SQL} AS document_id
            FROM entities e
            JOIN claims c ON c.entity_id = e.id AND c.status = 'published'
            JOIN claim_evidence_links l ON l.claim_id = c.id
            JOIN evidences ev ON ev.id = l.evidence_id
            JOIN sources s ON s.id = ev.source_id AND s.superseded_by_source_id IS NULL
            LEFT JOIN chunks ch ON ch.id = ev.chunk_id
            WHERE e.status = 'published' AND e.entity_type = 'Paper'
            """
        ).fetchall()
        for row in rows:
            if row["document_id"]:
                papers.setdefault(
                    str(row["document_id"]), {"node_id": row["node_id"], "title": row["name"]}
                )
        result: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            """
            SELECT d.id, d.title, d.local_path, d.source_url, d.pages, s.title AS source_title
            FROM documents d LEFT JOIN sources s ON s.id = d.source_id
            WHERE s.superseded_by_source_id IS NULL
            """
        ):
            document_id = str(row["id"])
            paper = papers.get(document_id)
            url = row["source_url"] if _is_http(row["source_url"]) else None
            result[document_id] = {
                "document_id": document_id,
                "node_id": paper["node_id"] if paper else f"{SOURCE_NODE_PREFIX}{document_id}",
                "title": paper["title"] if paper else _document_title(row),
                "pages": row["pages"],
                "url": url,
            }
        return result

    def _evidence_for_claims(
        self,
        connection: sqlite3.Connection,
        claim_ids: list[str],
        documents: dict[str, dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        if not claim_ids:
            return {}
        rows = connection.execute(
            f"""
            SELECT l.claim_id, ev.id, ev.quote, ev.location_json, {_DOCUMENT_ID_SQL} AS document_id,
                   ch.page_start AS chunk_page_start, ch.page_end AS chunk_page_end
            FROM claim_evidence_links l
            JOIN evidences ev ON ev.id = l.evidence_id
            JOIN sources s ON s.id = ev.source_id AND s.superseded_by_source_id IS NULL
            LEFT JOIN chunks ch ON ch.id = ev.chunk_id
            WHERE l.claim_id IN ({",".join("?" for _ in claim_ids)})
            ORDER BY l.claim_id, ev.id
            """,
            tuple(claim_ids),
        ).fetchall()
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            location = _loads(row["location_json"])
            result[str(row["claim_id"])].append(
                _evidence_view(
                    evidence_id=str(row["id"]),
                    quote=str(row["quote"]),
                    document_id=row["document_id"],
                    page_start=location.get("page_start") or row["chunk_page_start"],
                    page_end=location.get("page_end") or row["chunk_page_end"],
                    documents=documents,
                )
            )
        return result

    def _entity_detail(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        documents: dict[str, dict[str, Any]],
        include_pending: bool,
    ) -> dict[str, Any]:
        core_id = str(row["id"])
        node_id = str(row["node_id"])
        properties = _loads(row["properties_json"])
        claim_rows = connection.execute(
            """
            SELECT id, statement, claim_type, confidence FROM claims
            WHERE entity_id = ? AND status = 'published'
            ORDER BY CASE claim_type WHEN 'entity_definition' THEN 0 ELSE 1 END, created_at
            """,
            (core_id,),
        ).fetchall()
        relation_rows = connection.execute(
            """
            SELECT r.id, COALESCE(r.legacy_id, r.id) AS edge_id, r.relation_type,
                   r.source_entity_id, r.target_entity_id,
                   o.id AS other_core_id, COALESCE(o.legacy_id, o.id) AS other_id,
                   o.name AS other_name, o.entity_type AS other_type
            FROM relations r
            JOIN entities o ON o.id = CASE WHEN r.source_entity_id = ?
                                           THEN r.target_entity_id ELSE r.source_entity_id END
            WHERE r.status = 'published' AND o.status = 'published'
              AND (r.source_entity_id = ? OR r.target_entity_id = ?)
            ORDER BY r.relation_type, o.name
            """,
            (core_id, core_id, core_id),
        ).fetchall()
        relation_claims = (
            connection.execute(
                f"""
                SELECT id, relation_id, statement FROM claims
                WHERE status = 'published'
                  AND relation_id IN ({",".join("?" for _ in relation_rows)})
                ORDER BY created_at
                """,
                tuple(str(item["id"]) for item in relation_rows),
            ).fetchall()
            if relation_rows
            else []
        )
        evidence = self._evidence_for_claims(
            connection,
            [str(item["id"]) for item in claim_rows]
            + [str(item["id"]) for item in relation_claims],
            documents,
        )
        claims = [
            {
                "id": str(item["id"]),
                "statement": str(item["statement"]),
                "claim_type": str(item["claim_type"]),
                "confidence": item["confidence"],
                "status": "published",
                "evidence": evidence.get(str(item["id"]), []),
            }
            for item in claim_rows
            if evidence.get(str(item["id"]))
        ]
        if not claims:
            raise KeyError(node_id)
        statements: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for item in relation_claims:
            if evidence.get(str(item["id"])):
                statements[str(item["relation_id"])].append(item)
        relations = []
        for item in relation_rows:
            relation_claim_rows = statements.get(str(item["id"]), [])
            if not relation_claim_rows:
                continue
            relations.append(
                {
                    "id": str(item["edge_id"]),
                    "type": str(item["relation_type"]),
                    "direction": "out" if item["source_entity_id"] == core_id else "in",
                    "status": "published",
                    "statement": str(relation_claim_rows[0]["statement"])
                    if relation_claim_rows
                    else "",
                    "other": {
                        "id": str(item["other_id"]),
                        "label": str(item["other_name"]),
                        "type": str(item["other_type"]),
                        "status": "published",
                    },
                    "evidence": [
                        quote
                        for claim in relation_claim_rows
                        for quote in evidence.get(str(claim["id"]), [])
                    ],
                }
            )
        grouped_relations: dict[tuple[str, str, str], dict[str, Any]] = {}
        for relation in relations:
            key = (relation["direction"], relation["other"]["id"], relation["type"])
            if key in grouped_relations:
                grouped_relations[key]["evidence"].extend(relation["evidence"])
            else:
                grouped_relations[key] = relation
        relations = list(grouped_relations.values())
        for relation in relations:
            relation["evidence"] = list({e["id"]: e for e in relation["evidence"]}.values())
        if include_pending:
            relations.extend(self._pending_relations_for(connection, node_id, documents))
        summary = next(
            (item["statement"] for item in claims if item["claim_type"] == "entity_definition"),
            "",
        )
        collections = [
            str(item["collection_slug"])
            for item in connection.execute(
                """
                SELECT DISTINCT collection_slug FROM collection_memberships
                WHERE aggregate_type = 'entity' AND aggregate_id = ?
                ORDER BY collection_slug
                """,
                (node_id,),
            )
        ]
        return {
            "node": {
                "id": node_id,
                "label": str(row["name"]),
                "type": str(row["entity_type"]),
                "kind": "entity",
                "status": "published",
                "summary": summary,
                "aliases": [str(alias) for alias in properties.get("aliases") or []],
                "collections": collections,
            },
            "claims": claims,
            "relations": relations,
            "papers": _papers_from(claims, relations, documents, row),
            "review": None,
        }

    def _pending_relations_for(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        documents: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidate_ids = {
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM candidates WHERE kind = 'entity' AND (canonical_id = ? OR id = ?)",
                (node_id, node_id),
            )
        }
        if not candidate_ids:
            return []
        relations = []
        for row in connection.execute(
            "SELECT c.id, c.payload_json FROM candidates c "
            "WHERE c.kind = 'relation' AND c.status = 'draft' "
            f"AND {_CURRENT_CANDIDATE_SQL} ORDER BY c.created_at, c.id"
        ):
            payload = _loads(row["payload_json"])
            source = str(payload.get("source_candidate_id") or "")
            target = str(payload.get("target_candidate_id") or "")
            if source not in candidate_ids and target not in candidate_ids:
                continue
            other_candidate = target if source in candidate_ids else source
            other = self._candidate_endpoint(connection, other_candidate)
            if other is None:
                continue
            relations.append(
                {
                    "id": str(row["id"]),
                    "type": str(payload.get("type") or "RELATED"),
                    "direction": "out" if source in candidate_ids else "in",
                    "status": "pending",
                    "statement": str(payload.get("summary") or ""),
                    "other": other,
                    "evidence": [
                        _candidate_evidence_view(item, documents)
                        for item in _candidate_evidence(payload)
                    ],
                }
            )
        return relations

    @staticmethod
    def _candidate_endpoint(
        connection: sqlite3.Connection, candidate_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT c.id, c.status, c.canonical_id, c.payload_json FROM candidates c "
            f"WHERE c.id = ? AND {_CURRENT_CANDIDATE_SQL}",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        if row["canonical_id"]:
            entity = connection.execute(
                """
                SELECT COALESCE(legacy_id, id) AS node_id, name, entity_type FROM entities
                WHERE status = 'published' AND (legacy_id = ? OR id = ?)
                """,
                (row["canonical_id"], row["canonical_id"]),
            ).fetchone()
            if entity is not None:
                return {
                    "id": str(entity["node_id"]),
                    "label": str(entity["name"]),
                    "type": str(entity["entity_type"]),
                    "status": "published",
                }
        if row["status"] != "draft":
            return None
        payload = _loads(row["payload_json"])
        return {
            "id": str(row["id"]),
            "label": str(payload.get("name") or row["id"]),
            "type": str(payload.get("type") or "Concept"),
            "status": "pending",
        }

    def _candidate_detail(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        documents: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        payload = _loads(row["payload_json"])
        claims = [
            {
                "id": str(row["id"]),
                "statement": str(payload.get("summary") or ""),
                "claim_type": "candidate_summary",
                "confidence": payload.get("confidence"),
                "status": "pending",
                "evidence": [
                    _candidate_evidence_view(item, documents)
                    for item in _candidate_evidence(payload)
                ],
            }
        ]
        relations = self._pending_relations_for(connection, str(row["id"]), documents)
        return {
            "node": {
                "id": str(row["id"]),
                "label": str(payload.get("name") or row["id"]),
                "type": str(payload.get("type") or "Concept"),
                "kind": "entity",
                "status": "pending",
                "summary": str(payload.get("summary") or ""),
                "aliases": [str(alias) for alias in payload.get("aliases") or []],
                "collections": [],
            },
            "claims": claims,
            "relations": relations,
            "papers": _papers_from(claims, relations, documents, None),
            "review": {
                "candidate_id": str(row["id"]),
                "ingestion_id": str(row["ingestion_id"]),
                "merge_suggestions": payload.get("merge_suggestions") or [],
            },
        }

    def _source_detail(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        documents: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        document_id = node_id.removeprefix(SOURCE_NODE_PREFIX)
        document = documents.get(document_id)
        if document is None:
            raise KeyError(node_id)
        supported = connection.execute(
            f"""
            SELECT COALESCE(e.legacy_id, e.id) AS node_id, e.name, e.entity_type,
                   COUNT(DISTINCT ev.id) AS evidence_count
            FROM entities e
            JOIN claims c ON c.entity_id = e.id AND c.status = 'published'
            JOIN claim_evidence_links l ON l.claim_id = c.id
            JOIN evidences ev ON ev.id = l.evidence_id
            JOIN sources s ON s.id = ev.source_id AND s.superseded_by_source_id IS NULL
            LEFT JOIN chunks ch ON ch.id = ev.chunk_id
            WHERE e.status = 'published' AND {_DOCUMENT_ID_SQL} = ?
            GROUP BY e.id ORDER BY evidence_count DESC, e.name
            """,
            (document_id,),
        ).fetchall()
        return {
            "node": {
                "id": node_id,
                "label": document["title"],
                "type": "Paper",
                "kind": "source",
                "status": "published",
                "summary": "",
                "aliases": [],
                "collections": [],
            },
            "claims": [],
            "relations": [
                {
                    "id": f"evidence:{node_id}:{item['node_id']}",
                    "type": EVIDENCE_EDGE_TYPE,
                    "direction": "out",
                    "status": "published",
                    "statement": f"提供 {item['evidence_count']} 条证据",
                    "other": {
                        "id": str(item["node_id"]),
                        "label": str(item["name"]),
                        "type": str(item["entity_type"]),
                        "status": "published",
                    },
                    "evidence": [],
                }
                for item in supported
            ],
            "papers": [{**document, "evidence_count": sum(r["evidence_count"] for r in supported)}],
            "review": None,
        }


_DOCUMENT_ID_SQL = "COALESCE(ch.document_id, json_extract(ev.location_json, '$.paper_id'))"


def _papers_from(
    claims: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    row: sqlite3.Row | None,
) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for claim in claims:
        for item in claim["evidence"]:
            if item.get("document_id"):
                counts[item["document_id"]] += 1
    for relation in relations:
        for item in relation["evidence"]:
            if item.get("document_id"):
                counts[item["document_id"]] += 1
    papers = []
    for document_id, count in counts.items():
        document = documents.get(document_id) or {
            "document_id": document_id,
            "node_id": f"{SOURCE_NODE_PREFIX}{document_id}",
            "title": document_id,
            "pages": None,
            "url": None,
        }
        papers.append({**document, "evidence_count": count})
    return sorted(papers, key=lambda item: (-item["evidence_count"], item["title"]))


def _evidence_view(
    *,
    evidence_id: str,
    quote: str,
    document_id: Any,
    page_start: Any,
    page_end: Any,
    documents: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    document = documents.get(str(document_id)) if document_id else None
    return {
        "id": evidence_id,
        "quote": quote,
        "document_id": str(document_id) if document_id else None,
        "paper_title": document["title"] if document else None,
        "paper_node_id": document["node_id"] if document else None,
        "page_start": page_start,
        "page_end": page_end or page_start,
    }


def _candidate_evidence(payload: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        return [evidence]
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    return []


def _candidate_evidence_view(
    item: dict[str, Any], documents: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return _evidence_view(
        evidence_id=str(item.get("chunk_id") or ""),
        quote=str(item.get("quote") or ""),
        document_id=item.get("paper_id"),
        page_start=item.get("page_start"),
        page_end=item.get("page_end"),
        documents=documents,
    )


def _resolve_candidate(
    candidate_id: Any, canonical: dict[str, str], nodes: dict[str, _Node]
) -> str | None:
    if not candidate_id:
        return None
    key = str(candidate_id)
    if key in nodes:
        return key
    resolved = canonical.get(key)
    return resolved if resolved in nodes else None


def _document_title(row: sqlite3.Row) -> str:
    for value in (row["title"], row["source_title"]):
        if value and str(value).strip():
            return str(value)
    if row["local_path"]:
        return PurePath(str(row["local_path"]).replace("\\", "/")).name
    if row["source_url"]:
        return str(row["source_url"])
    return str(row["id"])


def _is_http(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()
