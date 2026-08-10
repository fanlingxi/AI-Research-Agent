from __future__ import annotations

from typing import Literal, Protocol

ProjectionTarget = Literal["qdrant", "neo4j", "obsidian"]


class KnowledgeProjectionRebuildPort(Protocol):
    """Future rebuild boundary: projections consume SQLite Core facts only."""

    def rebuild(self, *, target: ProjectionTarget, collection_slug: str | None = None) -> int: ...


class KnowledgeProjectionRebuildService:
    """Declares the rebuild contract without changing current projector implementations.

    Phase 1A intentionally leaves ``projector.py`` and ``obsidian.py`` untouched.
    A later phase can provide ports for each projection while retaining this stable
    call shape and the SQLite-first source-of-truth rule.
    """

    def __init__(self, port: KnowledgeProjectionRebuildPort) -> None:
        self.port = port

    def rebuild(
        self, *, target: ProjectionTarget, collection_slug: str | None = None
    ) -> int:
        return self.port.rebuild(target=target, collection_slug=collection_slug)
