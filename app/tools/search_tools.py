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
            content="检索主题不能为空。",
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
            "综述基础",
            "该离线示例用于模拟真实检索结果，概括主题的核心概念、分类框架和评估问题。",
        ),
        (
            "检索与索引",
            "该离线示例聚焦文档获取、切片、向量表示、向量检索和证据排序。",
        ),
        (
            "知识图谱构建",
            "该离线示例聚焦实体、声明、方法、数据集和关系抽取，并写入科研知识图谱。",
        ),
        (
            "多跳推理",
            "该离线示例聚焦将向量检索、图遍历和引用感知推理结合起来。",
        ),
        (
            "评估与反思",
            "该离线示例聚焦答案忠实性、引用覆盖、检索质量和 Critic 驱动的报告修订。",
        ),
    ]

    papers = []
    for index, (title_suffix, abstract) in enumerate(templates[:limit], start=1):
        title = f"{query}: {title_suffix}"
        papers.append(
            PaperMetadata(
                id=stable_paper_id("offline-demo", title=title, url=str(index)),
                title=title,
                authors=["AI-Research-Agent 示例"],
                abstract=abstract,
                year=2026,
                source="offline-demo",
                url=None,
                pdf_url=None,
                metadata={"note": "离线占位数据。使用 --live-search 可获取 arXiv 结果。"},
            )
        )
    return papers


def format_paper_results(papers: list[PaperMetadata]) -> str:
    lines = []
    for paper in papers:
        year = paper.year or "n.d."
        authors = ", ".join(paper.authors[:3]) if paper.authors else "未知作者"
        url = f" <{paper.url}>" if paper.url else ""
        lines.append(f"- {paper.title} ({year}) — {authors} [{paper.source}]{url}")
    return "\n".join(lines)


def _node_text(node: ET.Element, path: str, namespace: dict[str, str]) -> str:
    child = node.find(path, namespace)
    return child.text if child is not None and child.text else ""


def _clean_text(value: str) -> str:
    return textwrap.dedent(value).replace("\n", " ").strip()
