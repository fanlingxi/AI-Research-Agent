"""Hermetic fixtures that exercise production services without production state.

Nothing in this module reads the configured application defaults.  Every
service receives explicit paths underneath an Evaluation sandbox, and the only
LLM implementation is a local scripted test double.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.config.settings import Settings
from app.context.service import ContextBuilderService
from app.domain_plugins.game_modeling.knowledge import GameKnowledgeAuthoringService
from app.domain_plugins.game_modeling.models import (
    GameFormulaCandidateCreateRequest,
    GamePatchCandidateCreateRequest,
)
from app.domain_plugins.models import (
    ProjectDomainPluginUpdateRequest,
    WorkspaceTaskPluginBindRequest,
)
from app.domain_plugins.registry import create_builtin_plugin_registry
from app.domain_plugins.service import DomainPluginService
from app.knowledge.query import KnowledgeQueryService
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan, ReportEvidence
from app.knowledge.service import KnowledgeIngestionService
from app.llms.provider import LLMClient, MockLLMClient
from app.schemas.documents import DocumentChunk
from app.worker import KnowledgeWorker
from app.workspace.service import WorkspaceProjectionService


class ScriptedResearchLLM(LLMClient):
    """A local transcript that drives the actual bounded Research workflow.

    ``provider_name`` intentionally is not ``mock``: Research correctly rejects
    the MockLLM sentinel.  The Evaluation report separately labels this client
    as ``scripted_fixture`` and never represents it as a provider benchmark.
    """

    provider_name = "phase6_fixture"

    def __init__(self, mode: Literal["valid", "repair", "invalid", "interrupt"] = "valid"):
        self.mode = mode
        self.last_usage: dict[str, int] = {}
        self.calls: list[str] = []
        self._draft_calls = 0
        self._interrupted = False

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        del system_prompt
        self.calls.append(prompt)
        self.last_usage = {"input_tokens": 17, "output_tokens": 11}
        if "Return {research_question, intended_output, constraints}." in prompt:
            return json.dumps(
                {
                    "research_question": "What does the scoped fixture evidence establish?",
                    "intended_output": "A concise cited evaluation report.",
                    "constraints": ["Use only the immutable ContextSnapshot."],
                },
                ensure_ascii=False,
            )
        if "Return {steps, tool_sequence}." in prompt:
            return json.dumps(
                {
                    "steps": ["Read scoped input.", "Write cited findings."],
                    "tool_sequence": ["context.research_input"],
                },
                ensure_ascii=False,
            )
        if "Return {title, executive_summary, findings, limitations, markdown," in prompt:
            self._draft_calls += 1
            if self.mode == "interrupt" and not self._interrupted:
                self._interrupted = True
                raise RuntimeError("phase6 scripted interruption")
            invalid = self.mode == "invalid" or (self.mode == "repair" and self._draft_calls == 1)
            return self._draft_from_prompt(prompt, invalid=invalid)
        if "Return the complete ResearchDraft JSON object." in prompt:
            return self._draft_from_prompt(prompt, invalid=self.mode == "invalid")
        raise AssertionError("Unexpected Phase 6 research prompt")

    @staticmethod
    def _draft_from_prompt(prompt: str, *, invalid: bool) -> str:
        raw_input = prompt.split("Research input: ", 1)[1].rsplit("\nReturn", 1)[0]
        payload = json.loads(raw_input)
        bundle = payload["knowledge"]["claim_bundles"][0]
        evidence_id = "invented-evidence" if invalid else bundle["evidence"][0]["evidence_id"]
        return json.dumps(
            {
                "title": "Phase 6 Scoped Research Report",
                "executive_summary": "The fixture exercises the immutable evidence boundary.",
                "findings": [
                    {
                        "claim_bundle_id": bundle["claim"]["claim_id"],
                        "assertion": "The selected claim retains locatable evidence.",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "limitations": ["This is a local scripted-fixture evaluation."],
                "markdown": (
                    "# Phase 6 Scoped Research Report\n\n"
                    f"The selected claim retains locatable evidence. [cite:{evidence_id}]"
                ),
                "memory_proposal": {
                    "summary": "Keep ContextSnapshot scope explicit.",
                    "rationale": "The result is bounded by reviewed fixture evidence.",
                    "impact": "Preserve the same provenance boundary in future work.",
                },
            },
            ensure_ascii=False,
        )


class FixtureChunkSearch:
    """Local-only ChunkSearch port used to exercise KnowledgeQueryService filtering."""

    def __init__(self, evidence: list[ReportEvidence]) -> None:
        self.evidence = evidence
        self.calls: list[dict[str, Any]] = []

    def search(
        self, query: str, *, allowed_paper_ids: set[str], top_k: int
    ) -> list[ReportEvidence]:
        self.calls.append({"query": query, "allowed_paper_ids": sorted(allowed_paper_ids)})
        return [
            item
            for item in self.evidence
            if item.paper_id in allowed_paper_ids and query.casefold() in item.text.casefold()
        ][:top_k]


class EmptyGraphSearch:
    def search(self, query: str, *, topic_slugs: list[str], limit: int = 20) -> list[dict]:
        del query, topic_slugs, limit
        return []


@dataclass
class EvaluationFixture:
    name: str
    root: Path
    settings: Settings
    repository: KnowledgeRepository
    context_builder: ContextBuilderService
    agent_service: AgentRunService
    runtime: AgentRuntime
    worker: KnowledgeWorker
    workspace: WorkspaceProjectionService
    aliases: dict[str, Any] = field(default_factory=dict)
    scripted_llm: ScriptedResearchLLM | None = None
    query_service: KnowledgeQueryService | None = None


def build_fixture(name: str, root: Path) -> EvaluationFixture:
    """Build one self-contained fixture under ``root``.

    These profiles intentionally construct prerequisite reviewed facts through
    the existing repositories and plugin authoring service before the SUT is
    invoked.  The scenario itself only uses production Context/Runtime paths.
    """

    if name in {"research", "research_repair", "research_invalid", "research_interrupt"}:
        mode: Literal["valid", "repair", "invalid", "interrupt"] = {
            "research": "valid",
            "research_repair": "repair",
            "research_invalid": "invalid",
            "research_interrupt": "interrupt",
        }[name]
        return _build_research_fixture(root, name=name, mode=mode)
    if name in {"context", "retrieval"}:
        return _build_research_fixture(root, name=name, mode="valid")
    if name in {"game", "game_patch_mismatch"}:
        return _build_game_fixture(root, mismatch=name == "game_patch_mismatch")
    if name == "no_scope":
        return _build_no_scope_fixture(root)
    raise ValueError(f"Unknown Evaluation fixture: {name}")


def _settings(root: Path) -> Settings:
    return Settings(
        llm_provider="mock",
        knowledge_db_path=str(root / "knowledge.db"),
        agent_checkpoint_path=str(root / "agent_checkpoints.db"),
        knowledge_vault_path=str(root / "vault"),
        qdrant_url="http://127.0.0.1:9",
        neo4j_uri="bolt://127.0.0.1:9",
        embedding_provider="hash",
    )


def _stack(
    root: Path, *, name: str, llm: LLMClient | None = None
) -> EvaluationFixture:
    settings = _settings(root)
    repository = KnowledgeRepository(settings.knowledge_db_path)
    context_builder = ContextBuilderService(repository)
    agent_service = AgentRunService(
        repository,
        context_builder=context_builder,
        settings=settings,
        llm=llm or MockLLMClient(),
    )
    runtime = AgentRuntime(
        agent_service,
        checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path),
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=KnowledgeIngestionService(
            repository, settings=settings, require_live_llm=False
        ),
        report_service=KnowledgeReportService(
            repository, settings=settings, require_live_llm=False
        ),
        agent_runtime=runtime,
        lease_seconds=10,
    )
    workspace = WorkspaceProjectionService(
        repository, context_builder=context_builder, agent_run_service=agent_service
    )
    return EvaluationFixture(
        name=name,
        root=root,
        settings=settings,
        repository=repository,
        context_builder=context_builder,
        agent_service=agent_service,
        runtime=runtime,
        worker=worker,
        workspace=workspace,
    )


def _build_research_fixture(
    root: Path,
    *,
    name: str,
    mode: Literal["valid", "repair", "invalid", "interrupt"],
) -> EvaluationFixture:
    llm = ScriptedResearchLLM(mode)
    fixture = _stack(root, name=name, llm=llm)
    allowed = fixture.repository.create_collection("Phase 6 Allowed")
    denied = fixture.repository.create_collection("Phase 6 Denied")
    allowed_fact = _publish_research_fact(
        fixture.repository,
        collection_slug=allowed.slug,
        logical_id="allowed",
        name="Scoped Evaluation Method",
        quote="Scoped fixture evidence establishes the approved context boundary.",
    )
    denied_fact = _publish_research_fact(
        fixture.repository,
        collection_slug=denied.slug,
        logical_id="denied",
        name="Out of Scope Method",
        quote="Denied fixture evidence must never enter the allowed context boundary.",
    )
    memory = fixture.repository.memory_repository
    project = memory.create_project(
        name="Phase 6 Research", goal="Evaluate governed research.", domain="research", metadata={}
    )
    memory.replace_project_knowledge_scopes(
        project.id, [allowed.slug], expected_project_revision=project.revision
    )
    project = memory.get_project(project.id)
    task = memory.create_workspace_task(
        project_id=project.id,
        title="Evaluate scoped evidence",
        goal="Produce a cited report from the allowed evidence only.",
        priority="high",
        metadata={"expected_output": "A cited research report."},
    )
    fixture.aliases.update(
        {
            "project_id": project.id,
            "task_id": task.id,
            "allowed_collection": allowed.slug,
            "denied_collection": denied.slug,
            "allowed": allowed_fact,
            "denied": denied_fact,
        }
    )
    fixture.scripted_llm = llm
    fixture.query_service = KnowledgeQueryService(
        fixture.repository,
        chunk_search=FixtureChunkSearch(
            [allowed_fact["report_evidence"], denied_fact["report_evidence"]]
        ),
        graph_search=EmptyGraphSearch(),
        settings=fixture.settings,
    )
    return fixture


def _build_no_scope_fixture(root: Path) -> EvaluationFixture:
    fixture = _stack(root, name="no_scope")
    memory = fixture.repository.memory_repository
    project = memory.create_project(
        name="Phase 6 No Scope", goal="Validate closed scope.", domain="research", metadata={}
    )
    task = memory.create_workspace_task(
        project_id=project.id,
        title="No scope task",
        goal="This Context build must fail closed.",
        priority="normal",
        metadata={},
    )
    fixture.aliases.update({"project_id": project.id, "task_id": task.id})
    return fixture


def _build_game_fixture(root: Path, *, mismatch: bool) -> EvaluationFixture:
    fixture = _stack(root, name="game_patch_mismatch" if mismatch else "game")
    repository = fixture.repository
    collection = repository.create_collection("Phase 6 Game")
    patch_version = "1.2.0"
    patch = _publish_game_patch(repository, collection.slug, patch_version)
    formula = _publish_game_formula(repository, collection.slug, patch, patch_version)
    memory = repository.memory_repository
    project = memory.create_project(
        name="Phase 6 Game", goal="Evaluate deterministic calculation.", domain="game", metadata={}
    )
    memory.replace_project_knowledge_scopes(
        project.id, [collection.slug], expected_project_revision=project.revision
    )
    project = memory.get_project(project.id)
    plugin_service = DomainPluginService(
        repository.memory_repository, create_builtin_plugin_registry()
    )
    enabled = plugin_service.set_project_binding(
        project.id,
        "game_modeling",
        ProjectDomainPluginUpdateRequest(
            expected_project_revision=project.revision, status="enabled"
        ),
    )
    requested_version = "9.9.9" if mismatch else patch_version
    task = memory.create_workspace_task(
        project_id=project.id,
        title="Evaluate fixture formula",
        goal="Calculate the reviewed formula under the reviewed patch.",
        priority="high",
        metadata={
            "game_model": {
                "formula_claim_id": formula["claim_id"],
                "patch_claim_id": patch["claim_id"],
                "patch_version": requested_version,
                "parameters": {"base_damage": 10.0, "attack": 20.0, "ratio": 0.5},
            }
        },
    )
    bound = plugin_service.bind_workspace_task(
        task.id,
        WorkspaceTaskPluginBindRequest(
            expected_revision=task.revision, plugin_key="game_modeling"
        ),
    )
    fixture.aliases.update(
        {
            "project_id": enabled.project.id,
            "task_id": bound.task.id,
            "patch": patch,
            "formula": formula,
            "patch_version": requested_version,
        }
    )
    return fixture


def _publish_research_fact(
    repository: KnowledgeRepository,
    *,
    collection_slug: str,
    logical_id: str,
    name: str,
    quote: str,
) -> dict[str, Any]:
    evidence = _persist_fixture_evidence(
        repository,
        collection_slug=collection_slug,
        logical_id=logical_id,
        title=name,
        quote=quote,
    )
    ingestion_id = _ingestion_id_for_document(repository, evidence.paper_id)
    candidate = CandidateEntity(
        id=f"candidate-{logical_id}",
        ingestion_id=ingestion_id,
        topic_slug=collection_slug,
        name=name,
        type="Paper",
        summary=f"Reviewed Phase 6 fixture fact for {name}.",
        confidence=1.0,
        evidence=evidence,
    )
    repository.add_candidate_entity(candidate)
    published = repository.publish_entity(candidate.id)
    claim = repository.core_repository.get_claim_by_legacy_id(
        f"published_entity_definition:{published.id}"
    )
    return {
        "paper_id": evidence.paper_id,
        "chunk_id": evidence.chunk_id,
        "evidence_id": _evidence_id(repository, claim.id),
        "claim_id": claim.id,
        "entity_name": name,
        "report_evidence": ReportEvidence(
            id=f"E-{logical_id}",
            paper_id=evidence.paper_id,
            chunk_id=evidence.chunk_id,
            title=name,
            text=quote,
            page_start=1,
            page_end=1,
            score=1.0,
        ),
    }


def _publish_game_patch(
    repository: KnowledgeRepository, collection_slug: str, patch_version: str
) -> dict[str, Any]:
    evidence = _persist_fixture_evidence(
        repository,
        collection_slug=collection_slug,
        logical_id="game-patch",
        title="Phase 6 Patch",
        quote=f"Patch {patch_version} supplies the reviewed deterministic game version.",
        source_version=patch_version,
    )
    authoring = GameKnowledgeAuthoringService(repository)
    candidate = authoring.create_patch_candidate(
        GamePatchCandidateCreateRequest(
            ingestion_id=_ingestion_id_for_document(repository, evidence.paper_id),
            name=f"Patch {patch_version}",
            summary=f"Reviewed patch version {patch_version} for Phase 6 game modeling.",
            evidence=evidence,
            patch={"patch_version": patch_version},
        )
    )
    publication = authoring.approve_candidate(candidate.id)
    return publication.model_dump(mode="json")


def _publish_game_formula(
    repository: KnowledgeRepository,
    collection_slug: str,
    patch: dict[str, Any],
    patch_version: str,
) -> dict[str, Any]:
    evidence = _persist_fixture_evidence(
        repository,
        collection_slug=collection_slug,
        logical_id="game-formula",
        title="Phase 6 Formula",
        quote=f"Patch {patch_version} defines the reviewed formula for deterministic damage.",
        source_version=patch_version,
    )
    authoring = GameKnowledgeAuthoringService(repository)
    candidate = authoring.create_formula_candidate(
        GameFormulaCandidateCreateRequest(
            ingestion_id=_ingestion_id_for_document(repository, evidence.paper_id),
            name="Phase 6 Damage Formula",
            summary="Reviewed deterministic damage formula for the Phase 6 fixture.",
            evidence=evidence,
            formula={
                "expression": "base_damage + attack * ratio",
                "patch_claim_id": patch["claim_id"],
                "patch_version": patch_version,
                "unit": "damage",
                "variables": ["base_damage", "attack", "ratio"],
            },
        )
    )
    return authoring.approve_candidate(candidate.id).model_dump(mode="json")


def _persist_fixture_evidence(
    repository: KnowledgeRepository,
    *,
    collection_slug: str,
    logical_id: str,
    title: str,
    quote: str,
    source_version: str | None = None,
) -> EvidenceSpan:
    ingestion = repository.create_ingestion(
        collection=collection_slug,
        sources=[f"{logical_id}.pdf"],
        pdf_max_pages=1,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    paper_id = f"paper:phase6:{logical_id}"
    chunk_id = f"{paper_id}:page:1:chunk:0"
    evidence = EvidenceSpan(
        paper_id=paper_id, chunk_id=chunk_id, page_start=1, page_end=1, quote=quote
    )
    repository.add_document(
        ingestion_id=ingestion.id,
        document_id=paper_id,
        title=title,
        source="pdf",
        source_url=None,
        local_path=f"{logical_id}.pdf",
        pages=1,
        metadata={
            "original_source": f"{logical_id}.pdf",
            **({"source_version": source_version} if source_version else {}),
        },
    )
    repository.core_repository.record_source_document(
        document_id=paper_id,
        title=title,
        uri=f"fixture://{logical_id}.pdf",
        content=quote,
        parser_version="phase6-fixture-v1",
        metadata={
            "original_source": f"{logical_id}.pdf",
            **({"source_version": source_version} if source_version else {}),
        },
    )
    repository.core_repository.upsert_chunks(
        [
            DocumentChunk(
                id=chunk_id,
                paper_id=paper_id,
                title=title,
                text=quote,
                chunk_index=0,
                token_count=max(1, len(quote.split())),
                source_tier="primary_fulltext",
                metadata={"page_start": 1, "page_end": 1},
            )
        ]
    )
    return evidence


def _evidence_id(repository: KnowledgeRepository, claim_id: str) -> str:
    with repository._connect() as connection:
        row = connection.execute(
            """
            SELECT evidence_id FROM claim_evidence_links
            WHERE claim_id = ? ORDER BY evidence_id LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"Fixture Claim {claim_id} is missing evidence")
    return str(row["evidence_id"])


def _ingestion_id_for_document(repository: KnowledgeRepository, document_id: str) -> str:
    with repository._connect() as connection:
        row = connection.execute(
            "SELECT ingestion_id FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    if row is None:
        raise AssertionError(f"Fixture Document {document_id} is missing")
    return str(row["ingestion_id"])
