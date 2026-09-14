"""SQLite graph hydration and report audit adapters; no projection text is trusted."""

from __future__ import annotations

import sqlite3
from typing import Any

from app.context.knowledge_reader import KnowledgeContextReader
from app.context.retrieval_audit import evidence_identity
from app.knowledge.repository import KnowledgeRepository
from app.retrieval.contracts import (
    CandidateAudit,
    CandidateMatch,
    CandidateReference,
    SelectionAudit,
    SourceIdentity,
    validate_candidates,
)


def rehydrate_graph_candidates(
    repository: KnowledgeRepository,
    records: list[dict[str, Any]],
    *,
    topic_slugs: list[str],
    allowed_paper_ids: set[str],
) -> tuple[list[dict[str, str]], list[CandidateAudit], list[SelectionAudit]]:
    with repository.database.connect() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        return rehydrate_graph_candidates_tx(
            connection, records, topic_slugs=topic_slugs, allowed_paper_ids=allowed_paper_ids,
        )


def rehydrate_graph_candidates_tx(
    connection: sqlite3.Connection, records: list[dict[str, Any]], *,
    topic_slugs: list[str], allowed_paper_ids: set[str],
) -> tuple[list[dict[str, str]], list[CandidateAudit], list[SelectionAudit]]:
    """Read graph facts in the same caller-owned snapshot as report chunks."""
    references = [
        CandidateReference(
            channel="graph",
            target_type="legacy_relation",
            target_id=str(row.get("edge_id") or ""),
            source_identity=(
                SourceIdentity.model_validate(row["source_identity"])
                if row.get("source_identity") is not None
                else None
            ),
        )
        for row in records
    ]
    if not references or not allowed_paper_ids:
        return [], validate_candidates(references, {}), []
    scopes = topic_slugs or [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT collection_slug FROM collection_memberships "
            "ORDER BY collection_slug"
        )
    ]
    bundles = KnowledgeContextReader().read_formal_bundles_tx(connection, scopes)
    bundles = [
        b for b in bundles if all(item.document["id"] in allowed_paper_ids for item in b.evidence)
    ]
    entities = {b.entity["id"]: b for b in bundles if b.entity}
    bindings: dict[tuple[str, str], list[CandidateMatch]] = {}
    facts: dict[str, dict[str, str]] = {}
    for bundle in bundles:
        relation = bundle.relation
        if not relation or not relation["legacy_id"]:
            continue
        source = entities.get(relation["source_entity_id"])
        target = entities.get(relation["target_entity_id"])
        if source is None or target is None:
            continue
        relation_id = relation["legacy_id"]
        # The projection supplies only edge_id. Direction, type and names all
        # come from formally published, scoped and grounded SQLite objects.
        facts[relation_id] = {
            "edge_id": relation_id,
            "source_id": source.entity["legacy_id"] or source.entity["id"],
            "source_name": source.entity["name"],
            "relation_type": relation["relation_type"],
            "target_id": target.entity["legacy_id"] or target.entity["id"],
            "target_name": target.entity["name"],
        }
        matches = bindings.setdefault(("legacy_relation", relation_id), [])
        matches.append(
            CandidateMatch(
                item_type="relation",
                item_id=relation["id"],
                sources=list(
                    dict.fromkeys(
                        evidence_identity(e) for b in (bundle, source, target) for e in b.evidence
                    )
                ),
            )
        )
    observations = validate_candidates(references, bindings)
    graph = []
    selections = []
    seen: set[str] = set()
    for observation in observations:
        if observation.status != "verified":
            continue
        if observation.target_id in seen:
            observation.reason = "duplicate_ignored_first_valid_relation"
            continue
        seen.add(observation.target_id)
        graph.append(facts[observation.target_id])
        selections.append(
            SelectionAudit(
                item_type="relation",
                item_id=observation.matches[0].item_id,
                selected=True,
                reason="sqlite_verified_graph_relation",
                rank=len(graph),
                sources=list(
                    dict.fromkeys(s for match in observation.matches for s in match.sources)
                ),
            )
        )
    return graph, observations, selections
