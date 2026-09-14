"""Approval overlay and gold-free, verbatim paper prerequisites for A02."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan
from app.schemas.documents import DocumentChunk
from scripts.validate_paper_benchmark import PaperCorpus, PaperTasks, file_digest, validate_dataset


def approved_dataset(root: Path, approval_path: Path):
    validate_dataset(root)
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    tasks = [
        t for split in ("dev", "holdout")
        for t in PaperTasks.model_validate_json((root / f"tasks.{split}.json").read_bytes()).tasks
    ]
    if (
        approval["status"] != "approved"
        or approval["dataset_version"] != "a01-papers-v1"
        or approval["freeze_sha256"] != file_digest(root / "freeze.json")
        or set(approval["task_ids"]) != {t.id for t in tasks}
        or len(approval["task_ids"]) != len(tasks)
        or not approval["user_statement"]
    ):
        raise ValueError("Approval does not bind this exact frozen dataset")
    corpus = PaperCorpus.model_validate_json((root / "corpus.json").read_bytes())
    return corpus, tasks, approval


def task_input(task) -> dict:
    """Allowlist: no expectations, tags, gold spans or annotation rationale."""
    return {
        "id": task.id, "question": task.question,
        "report_requirements": task.report_requirements,
        "allowed_source_ids": task.allowed_source_ids,
    }


def import_papers(repository: KnowledgeRepository, corpus: PaperCorpus, root: Path) -> dict:
    """All pages equally, without reading gold spans or selecting answer pages.

    Publish literal source excerpts as controlled evaluation prerequisites via
    the existing review repository. No model-derived assertions are approved.
    600-char evidence limit is an existing platform contract, not retrieval tuning.
    """
    mapping = {"sources": {}, "chunks": {}}
    for paper in corpus.sources:
        collection = repository.create_collection(f"A02 {paper.id}")
        ingestion = repository.create_ingestion(
            collection=collection.slug, sources=[paper.pdf_url],
            pdf_max_pages=paper.pdf_pages, enqueue=False,
        )
        repository.update_ingestion(ingestion.id, status="needs_review")
        document_id = f"a02:{paper.id}"
        metadata = {
            "source_version": paper.version, "original_source": paper.pdf_url,
            "pdf_sha256": paper.pdf_sha256, "license": paper.license,
            "evaluation_only": True,
        }
        repository.add_document(
            ingestion_id=ingestion.id, document_id=document_id, title=paper.title,
            source="pdf", source_url=paper.pdf_url, local_path=str(root / paper.pdf_path),
            pages=paper.pdf_pages, metadata=metadata,
        )
        repository.core_repository.record_source_document(
            document_id=document_id, title=paper.title, uri=paper.pdf_url, content=paper.text,
            parser_version="a02-verbatim-600-v1", metadata=metadata,
        )
        mapping["sources"][paper.id] = {"document_id": document_id, "scope": collection.slug}
        index = 0
        for page in paper.pages:
            for start in range(page.start, page.end, 600):
                end = min(start + 600, page.end)
                quote = paper.text[start:end]
                chunk_id = f"{document_id}:page:{page.pdf_page}:part:{index}"
                location = {"page_start": page.pdf_page, "page_end": page.pdf_page,
                            "source_start": start, "source_end": end}
                repository.core_repository.upsert_chunks([DocumentChunk(
                    id=chunk_id, paper_id=document_id, title=paper.title, text=quote,
                    chunk_index=index, token_count=max(1, (len(quote) + 3) // 4),
                    source_tier="primary_fulltext", metadata=location,
                )])
                candidate = CandidateEntity(
                    id=f"candidate:{chunk_id}", ingestion_id=ingestion.id,
                    topic_slug=collection.slug,
                    name=f"{paper.id} page {page.pdf_page} excerpt {index}", type="Paper",
                    summary=quote, confidence=1.0,
                    evidence=EvidenceSpan(
                        paper_id=document_id, chunk_id=chunk_id, quote=quote,
                        page_start=page.pdf_page, page_end=page.pdf_page,
                    ),
                    metadata={"evaluation_only": True, "verbatim_transcription": True},
                )
                repository.add_candidate_entity(candidate)
                published = repository.publish_entity(
                    candidate.id, review_note="A02 controlled literal transcription; not model gold"
                )
                claim = repository.core_repository.get_claim_by_legacy_id(
                    f"published_entity_definition:{published.id}"
                )
                mapping["chunks"][chunk_id] = {
                    "source_id": paper.id, "source_version": paper.version,
                    "pdf_sha256": paper.pdf_sha256,
                    "start": start, "end": end, "pdf_page": page.pdf_page,
                    "claim_id": claim.id, "text_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                }
                index += 1
    return mapping
