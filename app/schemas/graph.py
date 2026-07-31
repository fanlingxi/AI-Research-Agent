from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EntityType = Literal["Paper", "Concept", "Method", "Dataset", "Metric", "Task", "Finding"]


class GraphEntity(BaseModel):
    """A normalized entity stored in the research knowledge graph."""

    id: str
    name: str
    type: EntityType
    description: str = ""
    source_chunk_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphRelation(BaseModel):
    """A directed relation between two graph entities."""

    id: str
    source_id: str
    target_id: str
    type: str
    description: str = ""
    weight: float = 1.0
    source_chunk_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphPath(BaseModel):
    """A compact multi-hop path returned by graph retrieval."""

    nodes: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)
    score: float = 0.0


class GraphBuildResult(BaseModel):
    """Result of extracting and storing a graph for one research run."""

    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)
    graph_store_provider: str = "memory"
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphRAGResult(BaseModel):
    """GraphRAG reasoning result combining vector evidence and graph paths."""

    graph_paths: list[GraphPath] = Field(default_factory=list)
    answer: str
    metadata: dict[str, Any] = Field(default_factory=dict)
