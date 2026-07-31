from app.schemas.research import ToolResult
from app.tools.base import ResearchTool, ToolRegistry
from app.tools.graph_tools import build_knowledge_graph, graph_rag_reasoning
from app.tools.pdf_tools import parse_pdf
from app.tools.search_tools import paper_search
from app.tools.vector_tools import semantic_retrieval


def topic_keyword_expander(topic: str) -> ToolResult:
    """Create deterministic starter keywords."""

    normalized_topic = topic.strip() or "AI research"
    keywords = [
        normalized_topic,
        f"{normalized_topic} survey",
        f"{normalized_topic} benchmark",
        f"{normalized_topic} knowledge graph",
        f"{normalized_topic} limitations",
    ]
    return ToolResult(
        tool_name="topic_keyword_expander",
        status="success",
        content="\n".join(f"- {keyword}" for keyword in keywords),
        metadata={"keywords": keywords},
    )


def mock_paper_search(query: str, limit: int = 5) -> ToolResult:
    """Legacy placeholder search kept for backward compatibility."""

    candidates = [
        {
            "title": f"Survey on {query}",
            "year": "2024",
            "source": "mock-arxiv",
            "url": "https://arxiv.org/",
        },
        {
            "title": f"Graph-based Retrieval for {query}",
            "year": "2023",
            "source": "mock-semantic-scholar",
            "url": "https://www.semanticscholar.org/",
        },
        {
            "title": f"Evaluation Methods for {query}",
            "year": "2025",
            "source": "mock-openreview",
            "url": "https://openreview.net/",
        },
    ][:limit]

    content = "\n".join(
        f"- {paper['title']} ({paper['year']}) [{paper['source']}]" for paper in candidates
    )
    return ToolResult(
        tool_name="mock_paper_search",
        status="success",
        content=content,
        metadata={"query": query, "candidates": candidates},
    )


def build_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ResearchTool(
            name="topic_keyword_expander",
            description="Expand a research topic into starter search keywords.",
            handler=topic_keyword_expander,
        )
    )
    registry.register(
        ResearchTool(
            name="mock_paper_search",
            description="Return legacy mock paper search results.",
            handler=mock_paper_search,
        )
    )
    registry.register(
        ResearchTool(
            name="paper_search",
            description="Search candidate papers through arXiv or offline fallback.",
            handler=paper_search,
        )
    )
    registry.register(
        ResearchTool(
            name="parse_pdf",
            description="Parse a local or remote PDF into extracted text.",
            handler=parse_pdf,
        )
    )
    registry.register(
        ResearchTool(
            name="semantic_retrieval",
            description="Index chunks and return top-k semantically relevant contexts.",
            handler=semantic_retrieval,
        )
    )
    registry.register(
        ResearchTool(
            name="build_knowledge_graph",
            description="Extract entities and relations, store them, and retrieve graph paths.",
            handler=build_knowledge_graph,
        )
    )
    registry.register(
        ResearchTool(
            name="graph_rag_reasoning",
            description="Combine vector hits and graph paths into a GraphRAG answer.",
            handler=graph_rag_reasoning,
        )
    )
    return registry
