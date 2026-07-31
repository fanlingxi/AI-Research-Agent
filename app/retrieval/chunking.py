from __future__ import annotations

import re

from app.schemas.documents import DocumentChunk, PaperMetadata


class TextChunker:
    """Simple word-window chunker used before Phase 3 graph extraction."""

    def __init__(self, chunk_size: int = 900, chunk_overlap: int = 150) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive.")
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap cannot be negative.")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size.")

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk_paper(self, paper: PaperMetadata, text: str) -> list[DocumentChunk]:
        words = self._tokenize_words(text)
        if not words:
            return []

        chunks: list[DocumentChunk] = []
        step = self.chunk_size - self.chunk_overlap

        for chunk_index, start in enumerate(range(0, len(words), step)):
            window = words[start : start + self.chunk_size]
            if not window:
                break

            chunk_text = " ".join(window)
            chunks.append(
                DocumentChunk(
                    id=f"{paper.id}:chunk:{chunk_index}",
                    paper_id=paper.id,
                    title=paper.title,
                    text=chunk_text,
                    chunk_index=chunk_index,
                    token_count=len(window),
                    metadata={
                        "source": paper.source,
                        "url": paper.url,
                        "pdf_url": paper.pdf_url,
                        "year": paper.year,
                    },
                )
            )

            if start + self.chunk_size >= len(words):
                break

        return chunks

    def _tokenize_words(self, text: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", text).strip()
        return normalized.split(" ") if normalized else []


def paper_to_retrieval_text(paper: PaperMetadata) -> str:
    """Build text from metadata when the PDF has not been downloaded yet."""

    authors = ", ".join(paper.authors[:8]) if paper.authors else "Unknown authors"
    year = str(paper.year) if paper.year else "Unknown year"
    abstract = paper.abstract or "No abstract was available from the search provider."

    return "\n".join(
        [
            f"Title: {paper.title}",
            f"Authors: {authors}",
            f"Year: {year}",
            f"Source: {paper.source}",
            f"URL: {paper.url or 'N/A'}",
            "",
            "Abstract:",
            abstract,
        ]
    )
