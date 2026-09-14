"""Local report input checks, shared by retrieval and the atomic terminal write."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from app.knowledge.schemas import ChunkSearchHit, ResearchReport

if TYPE_CHECKING:
    from app.knowledge.core_repository import KnowledgeCoreRepository


def report_request(report: ResearchReport) -> dict[str, Any]:
    return {
        "query": report.query, "topic_slugs": report.topic_slugs,
        "top_k": report.top_k, "report_depth": report.report_depth,
    }


def read_fingerprint(allowed, evidence, candidates, graph, selections) -> str:
    identities = {
        match.item_id: [source.model_dump(mode="json") for source in match.sources]
        for candidate in candidates if candidate.channel in {"vector", "sqlite"}
        for match in candidate.matches
    }
    payload = {
        "allowed": sorted(allowed),
        "chunks": [
            {"evidence": item.model_dump(mode="json"), "sources": identities[item.chunk_id]}
            for item in evidence
        ],
        "graph": graph,
        "graph_sources": [item.model_dump(mode="json") for item in selections],
    }
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def make_read_guard(topics, allowed, evidence, candidates, graph, selections) -> dict[str, Any]:
    return {
        "version": "report-input-v1",
        "topic_slugs": list(topics),
        "chunks": [{"chunk_id": item.chunk_id, "score": item.score} for item in evidence],
        "graph": [{"edge_id": item["edge_id"]} for item in graph],
        "fingerprint": read_fingerprint(allowed, evidence, candidates, graph, selections),
    }


def validate_read_guard_tx(
    connection: sqlite3.Connection, core: KnowledgeCoreRepository, guard: dict[str, Any],
) -> bool:
    from app.knowledge.retrieval_audit import rehydrate_graph_candidates_tx

    if guard.get("version") != "report-input-v1":
        return False
    topics = guard["topic_slugs"]
    allowed = core.authorized_report_paper_ids_tx(connection, topics)
    if not allowed:
        return False
    if "corpus_chunk_ids" in guard and (
        core.report_corpus_ids_tx(connection, allowed) != guard["corpus_chunk_ids"]
    ):
        return False
    evidence, candidates = core.rehydrate_report_candidates_tx(
        connection, [ChunkSearchHit.model_validate(item) for item in guard["chunks"]],
        allowed_paper_ids=allowed,
    )
    graph, _, selections = rehydrate_graph_candidates_tx(
        connection, guard["graph"], topic_slugs=topics, allowed_paper_ids=allowed,
    )
    return guard["fingerprint"] == read_fingerprint(
        allowed, evidence, candidates, graph, selections,
    )
