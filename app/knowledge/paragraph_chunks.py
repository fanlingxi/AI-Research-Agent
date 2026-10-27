"""Version-three chunks, with exact page spans for every included fragment."""

from __future__ import annotations

import re
from collections import defaultdict

from app.retrieval.anchors import locate_quote
from app.schemas.documents import (
    DocumentBlock,
    DocumentChunk,
    PaperMetadata,
    ParsedDocument,
    TextSpan,
)
from app.tools.pdf_layout import derive_document_blocks

PARSER_VERSION = "pypdf-v3"
_INCLUDED = {"paragraph", "abstract", "heading", "caption"}


def _sentences(text: str, limit: int):
    # A single overlong sentence is kept whole; never fall back to a mid-word cut.
    parts = re.split(r"(?<=[。！？])|(?<=[.!?])\s+(?=[A-Z\u3400-\u9fff])", text)
    pending = ""
    for part in parts:
        if not part.strip():
            continue
        joined = (pending + " " + part).strip()
        if pending and len(joined) > limit:
            yield pending
            pending = part.strip()
        else:
            pending = joined
    if pending:
        yield pending


def _piece_spans(
    piece: str, block: DocumentBlock, pages: dict[int, str], after: int,
) -> tuple[list[TextSpan], int]:
    raw = "\n".join(pages[s.page][s.start:s.end] for s in block.spans)
    found = locate_quote(piece, raw[after:])
    if found is None:
        raise ValueError("Derived paragraph no longer anchors to its raw page spans")
    start, end = (position + after for position in found)
    offset = 0
    spans = []
    for span in block.spans:
        length = span.end - span.start
        left, right = max(start, offset), min(end, offset + length)
        if left < right:
            spans.append(TextSpan(page=span.page, start=span.start + left - offset,
                                  end=span.start + right - offset))
        offset += length + 1
    return spans, end


def paragraph_chunks(
    paper: PaperMetadata, parsed: ParsedDocument, *, limit: int = 1200,
) -> list[DocumentChunk]:
    texts = parsed.page_texts or [parsed.text]
    numbers = parsed.page_numbers or list(range(1, len(texts) + 1))
    pages = dict(zip(numbers, texts, strict=True))
    blocks = parsed.blocks
    if blocks is None:
        blocks = derive_document_blocks(texts, numbers)
    chunks = []
    per_page = defaultdict(int)
    pending: list[str] = []
    spans: list[TextSpan] = []

    def flush():
        if not pending:
            return
        start, end = spans[0].page, spans[-1].page
        text = "\n\n".join(pending)
        chunks.append(DocumentChunk(
            id=f"{paper.id}:page:{start}:chunk:{per_page[start]}",
            paper_id=paper.id, title=paper.title, text=text, chunk_index=len(chunks),
            token_count=len(text.split()), source_tier="primary_fulltext",
            metadata={
                "source": "pdf", "source_tier": "primary_fulltext", "url": paper.url,
                "pdf_url": paper.pdf_url, "local_path": paper.metadata.get("local_path"),
                "content_kind": "pdf_full_text", "page_start": start, "page_end": end,
                "parser_version": PARSER_VERSION,
                "spans": [span.model_dump() for span in spans],
            },
        ))
        per_page[start] += 1
        pending.clear()
        spans.clear()

    for block in blocks:
        if block.kind not in _INCLUDED:
            # Excluded material is a hard boundary: no quote can span a deletion.
            flush()
            continue
        pieces = list(_sentences(block.text, limit)) if len(block.text) > limit else [block.text]
        after = 0
        for piece in pieces:
            piece_spans, after = _piece_spans(piece, block, pages, after)
            if pending:
                previous, current = spans[-1], piece_spans[0]
                contiguous = (previous.page == current.page
                              and not pages[current.page][previous.end:current.start].strip())
                if not contiguous or len("\n\n".join([*pending, piece])) > limit:
                    flush()
            pending.append(piece)
            spans.extend(piece_spans)
    flush()
    return chunks
