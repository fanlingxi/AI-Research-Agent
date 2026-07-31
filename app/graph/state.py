from typing import Annotated, Any, TypedDict

from app.schemas.documents import DocumentChunk, PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphPath, GraphRelation
from app.schemas.memory import MemoryRecord
from app.schemas.quality import CritiqueResult, EvaluationResult, ReflectionResult
from app.schemas.research import AgentTrace, ToolResult


def append_list(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    return (left or []) + (right or [])


class ResearchState(TypedDict, total=False):
    """State passed between LangGraph nodes."""

    query: str
    live_search: bool
    paper_limit: int
    top_k: int
    vector_store_provider: str
    graph_store_provider: str
    memory_enabled: bool
    memory_context: str
    memory_records: list[MemoryRecord]
    plan: dict[str, Any]
    tool_results: Annotated[list[ToolResult], append_list]
    papers: list[PaperMetadata]
    chunks: list[DocumentChunk]
    retrieval_results: list[RetrievalHit]
    rag_answer: str
    graph_entities: list[GraphEntity]
    graph_relations: list[GraphRelation]
    graph_paths: list[GraphPath]
    graphrag_answer: str
    evaluation_result: EvaluationResult
    critique_result: CritiqueResult
    reflection_result: ReflectionResult
    memory_record: MemoryRecord
    traces: Annotated[list[AgentTrace], append_list]
    final_report: str
    errors: Annotated[list[str], append_list]
