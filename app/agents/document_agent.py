from __future__ import annotations

from app.config.settings import get_settings
from app.retrieval.chunking import TextChunker, paper_to_retrieval_text
from app.schemas.documents import DocumentChunk, PaperMetadata


class DocumentAgent:
    """Convert paper metadata or parsed documents into retrieval chunks."""

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
