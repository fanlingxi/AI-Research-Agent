from app.schemas.research import ToolResult
from app.tools.base import ResearchTool, ToolRegistry


def topic_keyword_expander(topic: str) -> ToolResult:
    """Create deterministic starter keywords for Phase 1."""

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
    """Return placeholder paper candidates until Phase 2 adds real search."""

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
            description="Return mock paper search results for Phase 1 workflow testing.",
            handler=mock_paper_search,
        )
    )
    return registry
