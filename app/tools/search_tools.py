from __future__ import annotations

import hashlib
import textwrap
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from app.schemas.documents import PaperMetadata
from app.schemas.research import ToolResult

ARXIV_API_URL = "https://export.arxiv.org/api/query"


def stable_paper_id(source: str, title: str, url: str | None = None) -> str:
    raw = "|".join([source, title, url or ""])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def paper_search(query: str, limit: int = 5, live_search: bool = False) -> ToolResult:
    """Search papers through arXiv when enabled, otherwise use an offline fallback."""

    normalized_query = query.strip()
    if not normalized_query:
        return ToolResult(
            tool_name="paper_search",
            status="error",
            content="Query cannot be empty.",
            metadata={"papers": []},
        )

    papers: list[PaperMetadata] = []
    provider = "offline-demo"

    if live_search:
        try:
            papers = search_arxiv(normalized_query, limit=limit)
            provider = "arxiv"
        except Exception as exc:  # pragma: no cover - network boundary
            papers = []
            provider = f"offline-demo; arxiv_error={exc}"

    if not papers:
        papers = build_offline_demo_papers(normalized_query, limit=limit)

    content = format_paper_results(papers)
    return ToolResult(
        tool_name="paper_search",
        status="success",
        content=content,
        metadata={
            "query": normalized_query,
            "provider": provider,
            "papers": [paper.model_dump() for paper in papers],
        },
    )


def search_arxiv(query: str, limit: int = 5) -> list[PaperMetadata]:
    import httpx

    params = urlencode(
        {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": limit,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
    )
    response = httpx.get(f"{ARXIV_API_URL}?{params}", timeout=20.0)
    response.raise_for_status()

    return parse_arxiv_response(response.text)[:limit]


def parse_arxiv_response(xml_text: str) -> list[PaperMetadata]:
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_text)
    papers: list[PaperMetadata] = []

    for entry in root.findall("atom:entry", namespace):
        title = _node_text(entry, "atom:title", namespace)
        abstract = _node_text(entry, "atom:summary", namespace)
        url = _node_text(entry, "atom:id", namespace)
        published_at = _node_text(entry, "atom:published", namespace)
        year = int(published_at[:4]) if published_at[:4].isdigit() else None
        authors = [
            _node_text(author, "atom:name", namespace)
            for author in entry.findall("atom:author", namespace)
        ]
        pdf_url = None
        for link in entry.findall("atom:link", namespace):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href")
                break

        papers.append(
            PaperMetadata(
                id=stable_paper_id("arxiv", title=title, url=url),
                title=_clean_text(title),
                authors=[_clean_text(author) for author in authors if author],
                abstract=_clean_text(abstract),
                year=year,
                source="arxiv",
                url=url,
                pdf_url=pdf_url,
                published_at=published_at,
            )
        )

    return papers


def build_offline_demo_papers(query: str, limit: int = 5) -> list[PaperMetadata]:
    templates = [
        (
            "Survey Foundations",
            "A survey-style placeholder describing the core concepts, taxonomy, and evaluation "
            "questions that a real search provider should collect for the topic.",
        ),
        (
            "Retrieval and Indexing",
            "A placeholder focused on document acquisition, chunking, embeddings, vector search, "
            "and evidence ranking for research-oriented RAG systems.",
        ),
        (
            "Knowledge Graph Construction",
            "A placeholder focused on extracting entities, claims, methods, datasets, and "
            "relationships before storing them in a scientific knowledge graph.",
        ),
        (
            "Multi-hop Reasoning",
            "A placeholder focused on combining vector retrieval with graph traversal and "
            "citation-aware reasoning.",
        ),
        (
            "Evaluation and Reflection",
            "A placeholder focused on answer faithfulness, citation coverage, retrieval quality, "
            "and critic-driven report revision.",
        ),
    ]

    papers = []
    for index, (title_suffix, abstract) in enumerate(templates[:limit], start=1):
        title = f"{query}: {title_suffix}"
        papers.append(
            PaperMetadata(
                id=stable_paper_id("offline-demo", title=title, url=str(index)),
                title=title,
                authors=["AI-Research-Agent Demo"],
                abstract=abstract,
                year=2026,
                source="offline-demo",
                url=None,
                pdf_url=None,
                metadata={"note": "Offline placeholder. Enable --live-search for arXiv results."},
            )
        )
    return papers


def format_paper_results(papers: list[PaperMetadata]) -> str:
    lines = []
    for paper in papers:
        year = paper.year or "n.d."
        authors = ", ".join(paper.authors[:3]) if paper.authors else "Unknown authors"
        url = f" <{paper.url}>" if paper.url else ""
        lines.append(f"- {paper.title} ({year}) — {authors} [{paper.source}]{url}")
    return "\n".join(lines)


def _node_text(node: ET.Element, path: str, namespace: dict[str, str]) -> str:
    child = node.find(path, namespace)
    return child.text if child is not None and child.text else ""


def _clean_text(value: str) -> str:
    return textwrap.dedent(value).replace("\n", " ").strip()
