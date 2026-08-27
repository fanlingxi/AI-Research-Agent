from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from app.config.settings import Settings, get_settings
from app.knowledge.extractor import LiveLLMRequiredError, SchemaKnowledgeExtractor
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.projector import (
    KnowledgeProjector,
    Neo4jKnowledgeProjector,
    QdrantKnowledgeIndexer,
)
from app.knowledge.repository import (
    DecisionAlreadyApplied,
    KnowledgeRepository,
    StaleIngestionExecution,
)
from app.knowledge.schemas import (
    BulkApprovalResult,
    BulkCandidateDecision,
    BulkCandidateDecisionIssue,
    BulkCandidateDecisionResult,
    CandidateDecision,
    CandidateEntity,
    CandidateRelation,
    ConfidenceAutoApprovalResult,
    DecisionResult,
    KnowledgeIngestion,
    KnowledgeJob,
    ProjectionEvent,
)
from app.knowledge.source_identity import content_sha256, derive_source_identity, source_document_id
from app.llms.provider import LLMClient, MockLLMClient, get_llm_client
from app.schemas.documents import DocumentChunk, PaperMetadata, ParsedDocument
from app.tools.pdf_tools import parse_pdf_source

logger = logging.getLogger(__name__)


class KnowledgeExtractor(Protocol):
    def extract(self, paper: PaperMetadata, chunks: list[DocumentChunk]): ...


class ChunkIndexer(Protocol):
    def index(self, chunks: list[DocumentChunk]) -> None: ...


@dataclass
class NoopKnowledgeProjector:
    """Test-only projector for deterministic unit tests."""

    def upsert_entities(self, entities) -> None:
        return None

    def upsert_relations(self, relations) -> None:
        return None


@dataclass
class NoopChunkIndexer:
    """Test-only Qdrant substitute; production service always uses QdrantKnowledgeIndexer."""

    def index(self, chunks: list[DocumentChunk]) -> None:
        return None


class KnowledgeIngestionService:
    """Coordinates PDF-only candidate extraction, human review, and published projections."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        extractor: KnowledgeExtractor | None = None,
        parser: Callable[..., ParsedDocument] = parse_pdf_source,
        indexer: ChunkIndexer | None = None,
        projector: KnowledgeProjector | None = None,
        vault_exporter: KnowledgeVaultExporter | None = None,
        require_live_llm: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository
        self.core_repository = repository.core_repository
        self.llm = llm or get_llm_client(self.settings)
        self.extractor = extractor or SchemaKnowledgeExtractor(self.llm)
        self.parser = parser
        self.indexer = indexer or QdrantKnowledgeIndexer(self.settings)
        self.projector = projector or Neo4jKnowledgeProjector(self.settings)
        self.vault_exporter = vault_exporter or KnowledgeVaultExporter(
            self.settings.knowledge_vault_path
        )
        self.require_live_llm = require_live_llm

    def submit(
        self,
        *,
        topic: str | None = None,
        collection: str | None = None,
        sources: list[str],
        pdf_max_pages: int,
        auto_execute: bool = False,
    ) -> KnowledgeIngestion:
        self.ensure_execution_available()
        cleaned_sources = list(
            dict.fromkeys(source.strip() for source in sources if source.strip())
        )
        if not cleaned_sources:
            raise ValueError("至少需要提供一个本地 PDF 路径或 PDF URL。")
        ingestion, deduplicated = self.repository.create_or_reuse_active_ingestion(
            topic=topic,
            collection=collection,
            sources=cleaned_sources,
            pdf_max_pages=pdf_max_pages,
            auto_execute=auto_execute,
        )
        return ingestion.model_copy(update={"deduplicated": deduplicated})

    def run(
        self,
        ingestion_id: str,
        *,
        expected_job_id: str | None = None,
        expected_job_attempt: int | None = None,
        expected_job_owner: str | None = None,
    ) -> KnowledgeIngestion:
        if (expected_job_id is None) != (expected_job_attempt is None):
            raise ValueError("A claimed ingestion requires both its job id and attempt.")
        ingestion = self.repository.get_ingestion(ingestion_id)
        try:
            self.ensure_execution_available()
        except LiveLLMRequiredError as exc:
            if expected_job_id is not None and expected_job_attempt is not None:
                return self.repository.finalize_ingestion_execution(
                    ingestion_id,
                    expected_job_id,
                    expected_attempt=expected_job_attempt,
                    status="failed",
                    error=str(exc),
                    expected_owner=expected_job_owner,
                )
            raise
        if expected_job_id is not None and expected_job_attempt is not None:
            ingestion = self.repository.begin_ingestion_execution(
                ingestion_id,
                expected_job_id,
                expected_attempt=expected_job_attempt,
                expected_owner=expected_job_owner,
            )
        else:
            ingestion = self.repository.update_ingestion(ingestion_id, status="running")
        errors: list[str] = []
        indexed_chunks: list[DocumentChunk] = []
        documents: list[tuple[PaperMetadata, list[DocumentChunk]]] = []
        try:
            for source in ingestion.sources:
                try:
                    self._assert_claim(
                        ingestion_id,
                        expected_job_id=expected_job_id,
                        expected_job_attempt=expected_job_attempt,
                        expected_job_owner=expected_job_owner,
                    )
                    parsed = self.parser(source=source, max_pages=ingestion.pdf_max_pages)
                    self._assert_claim(
                        ingestion_id,
                        expected_job_id=expected_job_id,
                        expected_job_attempt=expected_job_attempt,
                        expected_job_owner=expected_job_owner,
                    )
                    paper = self._paper_from_document(parsed, source)
                    chunks = [
                        chunk.model_copy(
                            update={
                                "metadata": {
                                    **chunk.metadata,
                                    "ingestion_id": ingestion.id,
                                    "topic_slug": ingestion.topic_slug,
                                }
                            }
                        )
                        for chunk in self._page_chunks(paper, parsed)
                    ]
                    if not chunks:
                        raise ValueError("PDF 未提取到可用正文。")
                    self.repository.add_document(
                        ingestion_id=ingestion.id,
                        document_id=paper.id,
                        title=paper.title,
                        source="pdf",
                        source_url=paper.url,
                        local_path=str(paper.metadata.get("local_path") or ""),
                        pages=parsed.pages,
                        metadata=paper.metadata,
                    )
                    self.core_repository.record_source_document(
                        document_id=paper.id,
                        title=paper.title,
                        uri=source,
                        content=parsed.text,
                        parser_version="pypdf-v1",
                        metadata=paper.metadata,
                    )
                    self.core_repository.upsert_chunks(chunks)
                    documents.append((paper, chunks))
                    indexed_chunks.extend(chunks)
                except StaleIngestionExecution:
                    raise
                except Exception as exc:
                    errors.append(f"{source}：{exc}")

            if not documents:
                raise RuntimeError(
                    "没有任何 PDF 能够完成解析。" + (" " + "；".join(errors) if errors else "")
                )

            self._assert_claim(
                ingestion_id,
                expected_job_id=expected_job_id,
                expected_job_attempt=expected_job_attempt,
                expected_job_owner=expected_job_owner,
            )
            self.indexer.index(indexed_chunks)
            for paper, chunks in documents:
                candidates = self._candidate_batch(ingestion, paper, chunks)
                self.repository.replace_draft_candidates_for_paper(
                    ingestion.id,
                    paper.id,
                    candidates,
                    expected_job_id=expected_job_id,
                    expected_job_attempt=expected_job_attempt,
                    expected_job_owner=expected_job_owner,
                )
            warning = "；".join(errors) if errors else None
            if expected_job_id is not None and expected_job_attempt is not None:
                return self.repository.finalize_ingestion_execution(
                    ingestion_id,
                    expected_job_id,
                    expected_attempt=expected_job_attempt,
                    status="needs_review",
                    error=warning,
                    expected_owner=expected_job_owner,
                )
            result = self.repository.update_ingestion(
                ingestion_id, status="needs_review", error=warning
            )
            self.repository.complete_resource_job("ingestion", ingestion_id)
            return result
        except StaleIngestionExecution:
            raise
        except Exception as exc:
            if expected_job_id is not None and expected_job_attempt is not None:
                return self.repository.finalize_ingestion_execution(
                    ingestion_id,
                    expected_job_id,
                    expected_attempt=expected_job_attempt,
                    status="failed",
                    error=str(exc),
                    expected_owner=expected_job_owner,
                )
            return self.repository.update_ingestion(ingestion_id, status="failed", error=str(exc))

    def retry(self, ingestion_id: str, *, auto_execute: bool = False) -> KnowledgeIngestion:
        ingestion = self.repository.get_ingestion(ingestion_id)
        if ingestion.status not in {"failed", "interrupted"}:
            raise ValueError("只有失败或中断的入库任务可以重试。")
        self.ensure_execution_available()
        return self.repository.reset_ingestion(ingestion_id, auto_execute=auto_execute)

    def ensure_execution_available(self) -> None:
        """Fail before any ingestion side effect when formal extraction lacks a live LLM."""
        if self.require_live_llm and self._is_mock_llm():
            raise LiveLLMRequiredError(
                "知识入库需要配置 OPENAI、QWEN 或 DEEPSEEK 的真实 API Key。"
            )

    def execute_claimed(self, job: KnowledgeJob) -> KnowledgeIngestion:
        """Execute one claimed ingestion while fencing every terminal write."""
        try:
            return self.run(
                job.resource_id,
                expected_job_id=job.id,
                expected_job_attempt=job.attempts,
                expected_job_owner=job.lease_owner,
            )
        except StaleIngestionExecution:
            return self.repository.get_ingestion(job.resource_id)

    def fail_claimed(self, job: KnowledgeJob, error: str) -> KnowledgeIngestion:
        """Atomically fail a claimed ingestion if this attempt still owns it."""
        return self.repository.finalize_ingestion_execution(
            job.resource_id,
            job.id,
            expected_attempt=job.attempts,
            status="failed",
            error=error,
            expected_owner=job.lease_owner,
        )

    def execute_claimed_with_heartbeat(
        self,
        job: KnowledgeJob,
        *,
        lease_seconds: int,
    ) -> KnowledgeIngestion:
        """Run one claimed ingestion while renewing the same fenced attempt."""
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_claimed_lease,
            args=(job, lease_seconds, heartbeat_stop),
            daemon=True,
            name=f"ingestion-lease-{job.id}",
        )
        heartbeat.start()
        try:
            return self.execute_claimed(job)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)

    def _renew_claimed_lease(
        self,
        job: KnowledgeJob,
        lease_seconds: int,
        stopped: threading.Event,
    ) -> None:
        interval = max(0.05, min(30.0, max(1, lease_seconds) / 3))
        while not stopped.wait(interval):
            try:
                renewed = self.repository.renew_job_lease(
                    job.id,
                    expected_attempt=job.attempts,
                    lease_seconds=lease_seconds,
                    expected_owner=job.lease_owner,
                )
            except Exception:
                logger.exception("Failed to renew ingestion lease for %s", job.resource_id)
                continue
            if not renewed:
                return

    def _assert_claim(
        self,
        ingestion_id: str,
        *,
        expected_job_id: str | None,
        expected_job_attempt: int | None,
        expected_job_owner: str | None,
    ) -> None:
        if expected_job_id is None or expected_job_attempt is None:
            return
        self.repository.assert_ingestion_execution(
            ingestion_id,
            expected_job_id,
            expected_attempt=expected_job_attempt,
            expected_owner=expected_job_owner,
        )

    def decide(self, candidate_id: str, decision: CandidateDecision) -> DecisionResult:
        stored = self.repository.get_candidate(candidate_id)
        if stored["candidate"]["status"] != "draft":
            return self._replayed_decision(stored, decision)
        ingestion = self.repository.get_ingestion(stored["candidate"]["ingestion_id"])
        if ingestion.status != "needs_review":
            raise ValueError("入库任务完成抽取后才能审核候选。")
        try:
            if decision.decision == "reject":
                self.repository.reject_candidate(candidate_id, decision.review_note)
            elif decision.decision == "defer":
                self.repository.defer_candidate(candidate_id, decision.review_note)
            elif stored["kind"] == "entity":
                self.repository.publish_entity(
                    candidate_id,
                    canonical_id=(
                        decision.canonical_id if decision.decision in {"merge", "link"} else None
                    ),
                    review_note=decision.review_note,
                    decision_name=("link" if decision.decision == "link" else None),
                )
            else:
                if decision.decision in {"merge", "link"}:
                    raise ValueError("关系候选不支持链接或合并，请编辑后批准、待定或驳回。")
                self.repository.publish_relation(candidate_id)
        except DecisionAlreadyApplied:
            return self._replayed_decision(self.repository.get_candidate(candidate_id), decision)

        result = self.repository.get_candidate(candidate_id)
        self.repository.refresh_ingestion_status(result["candidate"]["ingestion_id"])
        return DecisionResult(
            candidate=result,
            applied=True,
            replayed=False,
            projection_status=self.repository.projection_status_for_candidate(candidate_id),
        )

    def _replayed_decision(self, stored: dict, requested: CandidateDecision) -> DecisionResult:
        existing = self.repository.get_review_decision(stored["candidate"]["id"])
        if existing is None:
            raise ValueError("候选已不在草稿状态，但没有可验证的审核记录。")
        same_decision = existing["decision"] == requested.decision
        if requested.decision in {"merge", "link"}:
            same_decision = same_decision and existing["canonical_id"] == requested.canonical_id
        if not same_decision:
            raise ValueError("候选已经存在不同的终态审核决定。")
        return DecisionResult(
            candidate=stored,
            applied=False,
            replayed=True,
            projection_status=self.repository.projection_status_for_candidate(
                stored["candidate"]["id"]
            ),
        )

    def approve_ready(self, ingestion_id: str) -> BulkApprovalResult:
        """Approve only unambiguous entities; semantic relations always need individual review."""
        published_entities = 0
        published_relations = 0
        skipped_conflicts = 0
        blocked_relations = 0
        for item in self.repository.list_candidates(ingestion_id, status="draft"):
            if item["kind"] != "entity":
                continue
            candidate = CandidateEntity.model_validate(item["candidate"])
            suggestions = candidate.merge_suggestions or self.repository.find_merge_suggestions(
                name=candidate.name,
                entity_type=candidate.type,
                aliases=candidate.aliases,
            )
            if any(item.match_kind == "exact" for item in suggestions):
                self.repository.set_merge_suggestions(candidate.id, suggestions)
                skipped_conflicts += 1
                continue
            try:
                result = self.decide(item["candidate"]["id"], CandidateDecision(decision="approve"))
                published_entities += int(result.applied)
            except ValueError:
                skipped_conflicts += 1
        blocked_relations = sum(
            item["kind"] == "relation"
            for item in self.repository.list_candidates(ingestion_id, status="draft")
        )
        return BulkApprovalResult(
            ingestion=self.repository.refresh_ingestion_status(ingestion_id),
            published_entities=published_entities,
            published_relations=published_relations,
            skipped_conflicts=skipped_conflicts,
            blocked_relations=blocked_relations,
        )

    def decide_bulk(
        self, ingestion_id: str, decision: BulkCandidateDecision
    ) -> BulkCandidateDecisionResult:
        """Apply an explicit, bounded selection of compatible review decisions.

        The selection is scoped to one ingestion before any write occurs.  Each
        candidate retains its normal transactional review event, so a stale row
        can be replayed safely and one unresolved relation never rolls back
        decisions that were already durably reviewed.
        """

        self.repository.get_ingestion(ingestion_id)
        candidates = {
            candidate_id: self.repository.get_candidate(candidate_id)
            for candidate_id in decision.candidate_ids
        }
        if any(
            item["candidate"]["ingestion_id"] != ingestion_id
            for item in candidates.values()
        ):
            raise ValueError("批量审核的候选必须属于当前 ingestion。")

        applied = 0
        replayed = 0
        skipped: list[BulkCandidateDecisionIssue] = []
        for candidate_id in decision.candidate_ids:
            stored = candidates[candidate_id]
            # Preserve the single-candidate endpoint's idempotency semantics
            # before looking for merge suggestions. A previously approved
            # candidate naturally matches the entity it just created.
            if stored["candidate"]["status"] != "draft":
                try:
                    result = self.decide(
                        candidate_id,
                        CandidateDecision(
                            decision=decision.decision,
                            review_note=decision.review_note,
                        ),
                    )
                    applied += int(result.applied)
                    replayed += int(result.replayed)
                except ValueError as exc:
                    skipped.append(
                        BulkCandidateDecisionIssue(candidate_id=candidate_id, reason=str(exc))
                    )
                continue
            if decision.decision == "approve" and stored["kind"] == "entity":
                candidate = CandidateEntity.model_validate(stored["candidate"])
                suggestions = (
                    candidate.merge_suggestions
                    or self.repository.find_merge_suggestions(
                        name=candidate.name,
                        entity_type=candidate.type,
                        aliases=candidate.aliases,
                    )
                )
                if any(item.match_kind == "exact" for item in suggestions):
                    self.repository.set_merge_suggestions(candidate.id, suggestions)
                    skipped.append(
                        BulkCandidateDecisionIssue(
                            candidate_id=candidate_id,
                            reason="发现同名规范实体，请逐条选择合并目标。",
                        )
                    )
                    continue
            try:
                result = self.decide(
                    candidate_id,
                    CandidateDecision(
                        decision=decision.decision,
                        review_note=decision.review_note,
                    ),
                )
                applied += int(result.applied)
                replayed += int(result.replayed)
            except ValueError as exc:
                skipped.append(
                    BulkCandidateDecisionIssue(candidate_id=candidate_id, reason=str(exc))
                )

        return BulkCandidateDecisionResult(
            ingestion=self.repository.refresh_ingestion_status(ingestion_id),
            decision=decision.decision,
            requested=len(decision.candidate_ids),
            applied=applied,
            replayed=replayed,
            skipped=skipped,
        )

    def auto_approve_confident(
        self, ingestion_id: str, *, min_confidence_exclusive: float = 0.9
    ) -> ConfidenceAutoApprovalResult:
        """Approve only candidates strictly above a configured confidence cutoff.

        This remains a governed operation: exact entity-name conflicts and
        relations whose endpoints are not both published are returned as
        skipped items rather than being guessed or force-published.
        """

        if not 0 < min_confidence_exclusive < 1:
            raise ValueError("自动审核阈值必须位于 0 与 1 之间。")
        candidate_ids = [
            item["candidate"]["id"]
            for item in self.repository.list_candidates(ingestion_id, status="draft")
            if float(item["candidate"]["confidence"]) > min_confidence_exclusive
        ]
        if not candidate_ids:
            ingestion = self.repository.get_ingestion(ingestion_id)
            return ConfidenceAutoApprovalResult(
                ingestion=ingestion,
                decision="approve",
                requested=0,
                min_confidence_exclusive=min_confidence_exclusive,
            )
        applied = 0
        replayed = 0
        skipped: list[BulkCandidateDecisionIssue] = []
        for offset in range(0, len(candidate_ids), 100):
            result = self.decide_bulk(
                ingestion_id,
                BulkCandidateDecision(
                    candidate_ids=candidate_ids[offset : offset + 100],
                    decision="approve",
                ),
            )
            applied += result.applied
            replayed += result.replayed
            skipped.extend(result.skipped)
        return ConfidenceAutoApprovalResult(
            ingestion=self.repository.refresh_ingestion_status(ingestion_id),
            decision="approve",
            requested=len(candidate_ids),
            applied=applied,
            replayed=replayed,
            skipped=skipped,
            min_confidence_exclusive=min_confidence_exclusive,
        )

    def process_projection(self, event: ProjectionEvent) -> KnowledgeIngestion:
        """Project one durable outbox event into Neo4j and the readable Vault."""
        try:
            if event.aggregate_type == "entity":
                self.projector.upsert_entities(
                    [self.repository.get_published_entity(event.aggregate_id)]
                )
            else:
                self.projector.upsert_relations(
                    [self.repository.get_published_relation(event.aggregate_id)]
                )
            self._render_topics([event.topic_slug])
        except Exception as exc:
            self.repository.fail_projection(
                event.id,
                str(exc),
                expected_attempt=event.attempts,
                expected_owner=event.lease_owner,
            )
            raise
        return self.repository.complete_projection(
            event.id,
            expected_attempt=event.attempts,
            expected_owner=event.lease_owner,
        )

    def drain_projections(self, limit: int = 1000) -> int:
        processed = 0
        while processed < limit:
            event = self.repository.claim_projection()
            if event is None:
                break
            self.process_projection(event)
            processed += 1
        return processed

    def topic(self, topic_slug: str) -> dict:
        return {
            "topic": next(
                (
                    item
                    for item in self.repository.list_topics()
                    if item["topic_slug"] == topic_slug
                ),
                None,
            ),
            "entities": [
                item.model_dump() for item in self.repository.list_published_entities(topic_slug)
            ],
            "relations": [
                item.model_dump() for item in self.repository.list_published_relations(topic_slug)
            ],
        }

    def collection(self, collection_slug: str) -> dict:
        return {
            "collection": self.repository.get_collection(collection_slug).model_dump(),
            "entities": [
                item.model_dump()
                for item in self.repository.list_published_entities(collection_slug)
            ],
            "relations": [
                item.model_dump()
                for item in self.repository.list_published_relations(collection_slug)
            ],
        }

    def entity_detail(self, entity_id: str) -> dict:
        return self.repository.entity_detail(entity_id)

    def sync_collections(self, collection_slugs: list[str]) -> None:
        """Rebuild affected collection projections from SQLite after a durable move job."""
        entities = self.repository.list_published_entities()
        if entities:
            self.projector.upsert_entities(entities)
        self._render_topics(list(dict.fromkeys(collection_slugs)))

    def graph(self, topic_slug: str | None = None) -> dict:
        return {
            "nodes": [
                item.model_dump() for item in self.repository.list_published_entities(topic_slug)
            ],
            "edges": [
                item.model_dump() for item in self.repository.list_published_relations(topic_slug)
            ],
        }

    def _candidate_batch(
        self,
        ingestion: KnowledgeIngestion,
        paper: PaperMetadata,
        chunks: list[DocumentChunk],
    ) -> list[CandidateEntity | CandidateRelation]:
        result = self.extractor.extract(paper, chunks)
        reading = result.payload.reading
        candidates: list[CandidateEntity | CandidateRelation] = []
        paper_candidate = CandidateEntity(
            id=_candidate_id(),
            ingestion_id=ingestion.id,
            topic_slug=ingestion.topic_slug,
            name=paper.title,
            type="Paper",
            summary=reading.research_problem,
            aliases=[],
            confidence=0.95,
            evidence=reading.evidence,
            metadata={
                "source_url": paper.url,
                "pdf_url": paper.pdf_url,
                "local_path": paper.metadata.get("local_path"),
                "reading": reading.model_dump(),
                "used_json_repair": result.used_repair,
            },
        )
        candidates.append(paper_candidate)
        entities_by_name: dict[str, CandidateEntity] = {_normal_name(paper.title): paper_candidate}

        for extracted in result.payload.entities:
            candidate = CandidateEntity(
                id=_candidate_id(),
                ingestion_id=ingestion.id,
                topic_slug=ingestion.topic_slug,
                name=extracted.name,
                type=extracted.type,
                summary=extracted.summary,
                aliases=extracted.aliases,
                sense_qualifier=extracted.sense_qualifier,
                paper_context=extracted.paper_context,
                role=extracted.role,
                conditions=extracted.conditions,
                confidence=extracted.confidence,
                evidence=extracted.evidence,
                merge_suggestions=self.repository.find_merge_suggestions(
                    name=extracted.name,
                    entity_type=extracted.type,
                    aliases=extracted.aliases,
                ),
            )
            candidates.append(candidate)
            entities_by_name.setdefault(_normal_name(candidate.name), candidate)

            relation_type = {
                "Method": "PRESENTS",
                "Task": "ADDRESSES",
                "Dataset": "EVALUATES",
                "Metric": "EVALUATES",
                "Finding": "SUPPORTS",
                "Concept": "SUPPORTS",
                "Topic": "SUPPORTS",
                "Formula": "SUPPORTS",
                "Patch": "SUPPORTS",
            }[candidate.type]
            candidates.append(
                CandidateRelation(
                    id=_candidate_id(),
                    ingestion_id=ingestion.id,
                    topic_slug=ingestion.topic_slug,
                    source_candidate_id=paper_candidate.id,
                    target_candidate_id=candidate.id,
                    type=relation_type,  # type: ignore[arg-type]
                    summary=f"《{paper.title}》{_relation_phrase(relation_type)}“{candidate.name}”。",
                    confidence=extracted.confidence,
                    evidence=extracted.evidence,
                    metadata={"generated_from": "paper_entity_link"},
                )
            )

        for extracted in result.payload.relations:
            source = entities_by_name.get(_normal_name(extracted.source_name))
            target = entities_by_name.get(_normal_name(extracted.target_name))
            if source is None or target is None or source.id == target.id:
                continue
            candidates.append(
                CandidateRelation(
                    id=_candidate_id(),
                    ingestion_id=ingestion.id,
                    topic_slug=ingestion.topic_slug,
                    source_candidate_id=source.id,
                    target_candidate_id=target.id,
                    type=extracted.type,
                    summary=extracted.summary,
                    confidence=extracted.confidence,
                    evidence=extracted.evidence,
                )
            )
        return candidates

    def _paper_from_document(self, parsed: ParsedDocument, source: str) -> PaperMetadata:
        checksum = content_sha256(parsed.text)
        source_metadata = dict(parsed.metadata)
        identity = derive_source_identity(
            uri=source,
            content_sha256=checksum,
            metadata=source_metadata,
        )
        paper_id = source_document_id(
            uri=source,
            content_sha256=checksum,
            metadata=source_metadata,
        )
        is_url = source.startswith(("http://", "https://"))
        return PaperMetadata(
            id=paper_id,
            title=(parsed.title or "未命名论文").strip(),
            abstract=parsed.text,
            source="pdf",
            url=source if is_url else None,
            pdf_url=source,
            source_tier="primary_fulltext",
            metadata={
                "content_kind": "pdf_full_text",
                "local_path": parsed.source,
                "page_count": parsed.pages,
                "original_source": source,
                "canonical_uri": identity.canonical_uri,
                "source_version": identity.version,
                "content_sha256": checksum,
            },
        )

    def _page_chunks(self, paper: PaperMetadata, parsed: ParsedDocument) -> list[DocumentChunk]:
        page_texts = parsed.page_texts or [parsed.text]
        page_numbers = parsed.page_numbers or list(range(1, len(page_texts) + 1))
        chunks: list[DocumentChunk] = []
        for page_number, text in zip(page_numbers, page_texts, strict=False):
            words = re.sub(r"\s+", " ", text).strip().split(" ")
            step = max(1, self.settings.chunk_size - self.settings.chunk_overlap)
            for page_chunk_index, start in enumerate(range(0, len(words), step)):
                window = words[start : start + self.settings.chunk_size]
                if not window:
                    break
                chunks.append(
                    DocumentChunk(
                        id=f"{paper.id}:page:{page_number}:chunk:{page_chunk_index}",
                        paper_id=paper.id,
                        title=paper.title,
                        text=" ".join(window),
                        chunk_index=len(chunks),
                        token_count=len(window),
                        source_tier="primary_fulltext",
                        metadata={
                            "source": "pdf",
                            "source_tier": "primary_fulltext",
                            "url": paper.url,
                            "pdf_url": paper.pdf_url,
                            "local_path": paper.metadata.get("local_path"),
                            "content_kind": "pdf_full_text",
                            "page_start": page_number,
                            "page_end": page_number,
                        },
                    )
                )
                if start + self.settings.chunk_size >= len(words):
                    break
        return chunks

    def _render_topics(self, topic_slugs: list[str]) -> None:
        topics = {item["topic_slug"]: item["topic"] for item in self.repository.list_topics()}
        for topic_slug in topic_slugs:
            topic = topics.get(topic_slug, topic_slug)
            result = self.vault_exporter.render_topic(
                topic=topic,
                topic_slug=topic_slug,
                entities=self.repository.list_published_entities(topic_slug),
                relations=self.repository.list_published_relations(topic_slug),
            )
            for ingestion in self.repository.list_ingestions():
                if ingestion.topic_slug == topic_slug:
                    self.repository.update_ingestion(
                        ingestion.id,
                        status=ingestion.status,
                        error=ingestion.error,
                        vault_path=result.vault_path,
                    )

    def _is_mock_llm(self) -> bool:
        return (
            isinstance(self.llm, MockLLMClient)
            or getattr(self.llm, "provider_name", "mock") == "mock"
        )


class KnowledgeIngestionDispatcher:
    """Durably execute browser-started ingestions and queued knowledge projections."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        service: KnowledgeIngestionService,
        *,
        lease_seconds: int = 180,
        poll_seconds: float = 0.5,
    ) -> None:
        self.repository = repository
        self.service = service
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="knowledge-ingestion-dispatcher",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.repository.claim_dispatched_ingestion_job(self.lease_seconds)
            except Exception:
                logger.exception("Failed to claim a dispatched ingestion job")
                job = None
            if job is not None:
                self._execute_ingestion(job)
                continue
            try:
                projection = self.repository.claim_projection(self.lease_seconds)
            except Exception:
                logger.exception("Failed to claim a knowledge projection")
                projection = None
            if projection is not None:
                try:
                    self.service.process_projection(projection)
                except Exception:
                    logger.exception(
                        "Knowledge projection failed for %s", projection.aggregate_id
                    )
                continue
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _execute_ingestion(self, job: KnowledgeJob) -> None:
        try:
            self.service.execute_claimed_with_heartbeat(
                job,
                lease_seconds=self.lease_seconds,
            )
        except Exception as exc:
            logger.exception("Ingestion dispatcher failed while executing %s", job.resource_id)
            try:
                self.service.fail_claimed(job, str(exc))
            except StaleIngestionExecution:
                pass
            except Exception:
                logger.exception("Failed to persist ingestion dispatcher failure")


def _candidate_id() -> str:
    return f"candidate-{uuid4().hex}"


def _normal_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _relation_phrase(relation_type: str) -> str:
    return {
        "PRESENTS": "提出了",
        "ADDRESSES": "关注",
        "EVALUATES": "评估了",
        "SUPPORTS": "讨论了",
    }.get(relation_type, "关联到")
