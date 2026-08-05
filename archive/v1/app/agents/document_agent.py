from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.config.settings import get_settings
from app.retrieval.chunking import TextChunker, paper_to_retrieval_text
from app.schemas.documents import DocumentChunk, PaperMetadata
from app.tools.pdf_tools import parse_pdf_source


@dataclass
class DocumentIngestionResult:
    """PDF ingestion outcome kept separate from search metadata."""

    papers: list[PaperMetadata]
    errors: list[str]


class DocumentAgent:
    """Convert paper metadata or parsed documents into retrieval chunks."""

    def ingest_pdf_sources(
        self,
        sources: list[str],
        max_pages: int | None = None,
    ) -> DocumentIngestionResult:
        """Parse user-selected PDFs and expose their full text as research papers.

        Search results are intentionally not downloaded automatically.  A user must
        opt in through ``--pdf`` so the workflow remains fast, predictable, and
        respectful of network and storage costs.
        """

        papers: list[PaperMetadata] = []
        errors: list[str] = []
        for source in sources:
            try:
                parsed = parse_pdf_source(source=source, max_pages=max_pages)
            except Exception as exc:  # pragma: no cover - network and malformed PDF boundary
                errors.append(f"PDF 解析失败：{source}（{exc}）")
                continue

            original_source = str(parsed.metadata.get("original_source", source))
            source_id = hashlib.sha1(original_source.encode("utf-8")).hexdigest()[:16]
            is_url = original_source.startswith(("http://", "https://"))
            papers.append(
                PaperMetadata(
                    id=f"pdf:{source_id}",
                    title=parsed.title or "未命名 PDF 文档",
                    abstract=parsed.text,
                    source="pdf",
                    url=original_source if is_url else None,
                    pdf_url=original_source,
                    source_tier="primary_fulltext",
                    metadata={
                        "content_kind": "pdf_full_text",
                        "local_path": parsed.source,
                        "page_count": parsed.pages,
                        "text_characters": len(parsed.text),
                    },
                )
            )

        return DocumentIngestionResult(papers=papers, errors=errors)

    def build_chunks(
        self,
        papers: list[PaperMetadata],
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> list[DocumentChunk]:
        settings = get_settings()
        chunker = TextChunker(
            chunk_size=chunk_size or settings.chunk_size,
            chunk_overlap=chunk_overlap or settings.chunk_overlap,
        )

        chunks: list[DocumentChunk] = []
        for paper in papers:
            text = paper_to_retrieval_text(paper)
            chunks.extend(chunker.chunk_paper(paper=paper, text=text))

        return chunks
