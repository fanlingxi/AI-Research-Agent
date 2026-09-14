"""Explicit new evaluation importer; the sealed A02 importer remains unchanged."""

from __future__ import annotations

import hashlib

from app.knowledge.evidence_passages import PASSAGE_VERSION, passage_ranges
from app.knowledge.schemas import CandidateEntity, EvidenceSpan
from app.schemas.documents import DocumentChunk


def import_passages(repository, corpus, root, *, version=PASSAGE_VERSION, ranges_by_source=None):
    """Review literal excerpts uniformly over all pages, without consulting gold."""
    mapping = {"version": version, "sources": {}, "chunks": {}}
    for paper in corpus.sources:
        collection = repository.create_collection(f"A08 {version} {paper.id}")
        ingestion = repository.create_ingestion(
            collection=collection.slug,
            sources=[paper.pdf_url],
            pdf_max_pages=paper.pdf_pages,
            enqueue=False,
        )
        repository.update_ingestion(ingestion.id, status="needs_review")
        document_id = f"a08:{version}:{paper.id}"
        metadata = {
            "source_version": paper.version,
            "original_source": paper.pdf_url,
            "pdf_sha256": paper.pdf_sha256,
            "license": paper.license,
            "evaluation_only": True,
            "passage_version": version,
        }
        repository.add_document(
            ingestion_id=ingestion.id,
            document_id=document_id,
            title=paper.title,
            source="pdf",
            source_url=paper.pdf_url,
            local_path=str(root / paper.pdf_path),
            pages=paper.pdf_pages,
            metadata=metadata,
        )
        repository.core_repository.record_source_document(
            document_id=document_id,
            title=paper.title,
            uri=paper.pdf_url,
            content=paper.text,
            parser_version=version,
            metadata=metadata,
        )
        mapping["sources"][paper.id] = {"document_id": document_id, "scope": collection.slug}
        index = 0
        for start, end, hard_cut, page_start, page_end, page_limited in _ranges(
            paper, ranges_by_source
        ):
            quote = paper.text[start:end]
            if not quote.strip():
                continue
            chunk_id = f"{document_id}:page:{page_start}:part:{index}"
            location = {
                "page_start": page_start,
                "page_end": page_end,
                "source_start": start,
                "source_end": end,
                "passage_version": version,
                "hard_cut": hard_cut,
                "page_limited": page_limited,
            }
            repository.core_repository.upsert_chunks(
                [
                    DocumentChunk(
                        id=chunk_id,
                        paper_id=document_id,
                        title=paper.title,
                        text=quote,
                        chunk_index=index,
                        token_count=max(1, (len(quote) + 3) // 4),
                        source_tier="primary_fulltext",
                        metadata=location,
                    )
                ]
            )
            candidate = CandidateEntity(
                id=f"candidate:{chunk_id}",
                ingestion_id=ingestion.id,
                topic_slug=collection.slug,
                type="Paper",
                name=f"{paper.id} page {page_start} passage {index}",
                summary=quote,
                confidence=1.0,
                evidence=EvidenceSpan(
                    paper_id=document_id,
                    chunk_id=chunk_id,
                    quote=quote,
                    page_start=page_start,
                    page_end=page_end,
                ),
                metadata={**metadata, "verbatim_transcription": True},
            )
            repository.add_candidate_entity(candidate)
            published = repository.publish_entity(
                candidate.id,
                review_note="A08 controlled literal transcription; not model or semantic gold",
            )
            claim = repository.core_repository.get_claim_by_legacy_id(
                f"published_entity_definition:{published.id}"
            )
            mapping["chunks"][chunk_id] = {
                "source_id": paper.id,
                "source_version": paper.version,
                "pdf_sha256": paper.pdf_sha256,
                "start": start,
                "end": end,
                "pdf_page": page_start,
                "pdf_page_end": page_end,
                "claim_id": claim.id,
                "text_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                "hard_cut": hard_cut,
                "page_limited": page_limited,
            }
            index += 1
    return mapping


def _ranges(paper, overrides):
    if overrides is None:
        for page in paper.pages:
            for left, right, hard in passage_ranges(paper.text[page.start : page.end]):
                yield (
                    page.start + left,
                    page.start + right,
                    hard,
                    page.pdf_page,
                    page.pdf_page,
                    page.start + right == page.end,
                )
        return
    previous = 0
    for start, end, hard in overrides[paper.id]:
        if start != previous or not start < end <= len(paper.text) or end - start > 6000:
            raise ValueError("Passage ranges must preserve contiguous original text within bounds")
        pages = [p.pdf_page for p in paper.pages if max(p.start, start) < min(p.end, end)]
        if not pages:
            raise ValueError("Passage has no source page")
        yield start, end, hard, min(pages), max(pages), False
        previous = end
    if previous != len(paper.text):
        raise ValueError("Passage ranges omitted source text")
