"""Bounded, evidence-constrained Research Agent workflow for Phase 3B."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.agent.errors import ResearchValidationError
from app.agent.state import ResearchAgentState
from app.agent.tools import (
    build_research_tool_registry,
    research_input_payload,
    snapshot_tool_audit_summary,
)
from app.context.models import ContextPackage
from app.domain_plugins.contracts import DomainFinalizationCommand
from app.domain_plugins.research.ports import ResearchRuntimePort

_CITATION_PATTERN = re.compile(r"\[cite:([A-Za-z0-9._:-]+)\]")
_RESEARCH_SYSTEM_PROMPT = (
    "You are a rigorous research assistant. Use only the supplied immutable ContextSnapshot. "
    "Return JSON only, never reveal chain-of-thought, and never invent evidence, citations, "
    "sources, tool results, file contents, or project state."
)


class TaskAnalysis(BaseModel):
    research_question: str = Field(min_length=2, max_length=2000)
    intended_output: str = Field(min_length=2, max_length=1000)
    constraints: list[str] = Field(default_factory=list, max_length=20)


class ResearchPlan(BaseModel):
    steps: list[str] = Field(min_length=1, max_length=4)
    tool_sequence: list[
        Literal[
            "context.research_input",
            "context.task_constraints",
            "context.knowledge_bundles",
        ]
    ] = Field(min_length=1, max_length=3)

    @field_validator("steps", mode="before")
    @classmethod
    def normalize_descriptive_step_objects(cls, value: Any) -> Any:
        """Accept the common lossless {step, action} JSON shape from compatible proxies."""

        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, str):
                normalized.append(item)
                continue
            if isinstance(item, dict):
                description = item.get("action", item.get("description"))
                if isinstance(description, str):
                    normalized.append(description)
                    continue
            normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def requires_one_complete_snapshot_input(self):
        if "context.research_input" not in self.tool_sequence:
            raise ValueError("research plan must include context.research_input")
        if len(self.tool_sequence) != len(set(self.tool_sequence)):
            raise ValueError("research plan cannot repeat a snapshot tool")
        return self


class ResearchFinding(BaseModel):
    claim_bundle_id: str = Field(min_length=1)
    assertion: str = Field(min_length=2, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class MemoryProposalSuggestion(BaseModel):
    summary: str = Field(min_length=2, max_length=2000)
    rationale: str = Field(default="", max_length=5000)
    impact: str = Field(default="", max_length=3000)


class ResearchDraft(BaseModel):
    title: str = Field(min_length=2, max_length=500)
    executive_summary: str = Field(default="", max_length=6000)
    findings: list[ResearchFinding] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    markdown: str = Field(min_length=2, max_length=30000)
    memory_proposal: MemoryProposalSuggestion | None = None


class CitationValidation(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    invalid_citation_ids: list[str] = Field(default_factory=list)
    repair_applied: bool = False


class ResearchWorkflow:
    """Finite Research workflow: no unbounded planning or out-of-snapshot reads."""

    def __init__(self, service: ResearchRuntimePort) -> None:
        self.service = service
        self.llm = service.llm

    def compile(self, checkpointer: Any):
        graph = StateGraph(ResearchAgentState)
        graph.add_node("load_context", self._load_context)
        graph.add_node("task_analysis", self._task_analysis)
        graph.add_node("constrained_plan", self._constrained_plan)
        graph.add_node("snapshot_tools", self._snapshot_tools)
        graph.add_node("research_draft", self._research_draft_validate_and_finalize)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "task_analysis")
        graph.add_edge("task_analysis", "constrained_plan")
        graph.add_edge("constrained_plan", "snapshot_tools")
        graph.add_edge("snapshot_tools", "research_draft")
        graph.add_edge("research_draft", END)
        return graph.compile(checkpointer=checkpointer)

    def _load_context(self, state: ResearchAgentState) -> dict[str, Any]:
        self.service.mark_node(
            state["run_id"],
            status="running",
            node_name="load_context",
            input_summary={"context_snapshot_id": state["context_snapshot_id"]},
        )
        package = self.service.load_context_snapshot(state["run_id"])
        self.service.record_node_completed(
            state["run_id"],
            node_name="load_context",
            output_summary={
                "context_snapshot_id": package.snapshot_id,
                "claim_bundle_count": len(package.knowledge.claim_bundles),
            },
        )
        return self._progress(
            state,
            phase="task_analysis",
            current_node="task_analysis",
            steps_used=1,
            selected_claim_ids=[
                bundle.claim.claim_id for bundle in package.knowledge.claim_bundles
            ],
            selected_evidence_ids=[
                evidence.evidence_id
                for bundle in package.knowledge.claim_bundles
                for evidence in bundle.evidence
            ],
        )

    def _task_analysis(self, state: ResearchAgentState) -> dict[str, Any]:
        self._mark_running(state, "task_analysis")
        package = self.service.load_context_snapshot(state["run_id"])
        task_input = {
            "task": package.task.model_dump(mode="json"),
            # Keep the source constraint object distinct from the required
            # output field.  Otherwise models can copy this object verbatim
            # into TaskAnalysis.constraints, whose contract is a list.
            "task_constraints": package.constraints.model_dump(mode="json"),
        }
        analysis = self._invoke_model(
            TaskAnalysis,
            prompt=(
                "Analyze the task using only this snapshot task and constraint data.\n"
                f"{_canonical_json(task_input)}\n"
                "In the output, constraints MUST be a JSON array of concise strings, "
                "never an object.\n"
                "Return {research_question, intended_output, constraints}."
            ),
        )
        self._record_llm_node(
            state,
            "task_analysis",
            {"constraint_count": len(analysis.constraints)},
        )
        return self._progress(
            state,
            phase="constrained_plan",
            current_node="constrained_plan",
            steps_used=int(state.get("steps_used", 1)) + 1,
            llm_calls_used=int(state.get("llm_calls_used", 0)) + 1,
            task_analysis=analysis.model_dump(mode="json"),
        )

    def _constrained_plan(self, state: ResearchAgentState) -> dict[str, Any]:
        self._mark_running(state, "constrained_plan")
        run = self.service.get_run(state["run_id"])
        analysis = TaskAnalysis.model_validate(state["task_analysis"])
        plan = self._invoke_model(
            ResearchPlan,
            prompt=(
                "Create a bounded research plan. The only permitted tools are "
                "context.research_input, context.task_constraints, context.knowledge_bundles. "
                "The plan must include context.research_input and use no more than "
                f"{run.max_tool_calls} tool(s).\n"
                f"Task analysis: {_canonical_json(analysis.model_dump(mode='json'))}\n"
                "steps MUST be a JSON array of concise strings, not step objects.\n"
                "Return {steps, tool_sequence}."
            ),
        )
        if len(plan.tool_sequence) > run.max_tool_calls:
            raise ResearchValidationError("Research plan exceeds AgentRun max_tool_calls.")
        self._record_llm_node(
            state,
            "constrained_plan",
            {
                "step_count": len(plan.steps),
                "tool_sequence": plan.tool_sequence,
                # This is a bounded execution plan, not a prompt or private
                # reasoning trace. It is needed by the Workspace audit view
                # after the ephemeral LangGraph state has been discarded.
                "plan_steps": [
                    {"position": index, "description": _safe_plan_step(step)}
                    for index, step in enumerate(plan.steps, start=1)
                ],
            },
        )
        return self._progress(
            state,
            phase="snapshot_tools",
            current_node="snapshot_tools",
            steps_used=int(state.get("steps_used", 2)) + 1,
            llm_calls_used=int(state.get("llm_calls_used", 0)) + 1,
            research_plan=plan.model_dump(mode="json"),
        )

    def _snapshot_tools(self, state: ResearchAgentState) -> dict[str, Any]:
        self._mark_running(state, "snapshot_tools")
        package = self.service.load_context_snapshot(state["run_id"])
        plan = ResearchPlan.model_validate(state["research_plan"])
        registry = build_research_tool_registry(package)
        arguments = {
            "context_snapshot_id": package.snapshot_id,
            "context_sha256": package.package_sha256,
        }
        tool_call_ids: list[str] = []
        for offset, tool_name in enumerate(plan.tool_sequence, start=1):
            result = registry.execute(
                name=tool_name,
                permission="context_read",
                arguments=arguments,
            )
            # The position is stable across checkpoint recovery.  It is not
            # derived from the mutable persisted call count, which would turn
            # an interrupted replay into a duplicate external observation.
            sequence = offset
            tool_call = self.service.record_tool_call(
                state["run_id"],
                sequence=sequence,
                tool_name=tool_name,
                permission="context_read",
                arguments=arguments,
                result_summary=snapshot_tool_audit_summary(tool_name, result),
                idempotency_key=f"{state['run_id']}:{tool_name}:{sequence}",
                node_name="snapshot_tools",
            )
            tool_call_ids.append(tool_call.id)
        self.service.record_node_completed(
            state["run_id"],
            node_name="snapshot_tools",
            output_summary={"tool_names": sorted(plan.tool_sequence)},
        )
        return self._progress(
            state,
            phase="research_draft",
            current_node="research_draft",
            steps_used=int(state.get("steps_used", 3)) + 1,
            tool_calls_used=self.service.get_run(state["run_id"]).tool_call_count,
            tool_call_ids=tool_call_ids,
        )

    def _research_draft_validate_and_finalize(self, state: ResearchAgentState) -> dict[str, Any]:
        """Keep source content and draft text local to this execution step.

        A draft is intentionally not a LangGraph channel: it can contain large
        generated prose and would otherwise be copied into the SQLite
        checkpointer.  The source is reloaded from the immutable,
        hash-validated ContextSnapshot only for this bounded node.
        """

        self._mark_running(state, "research_draft")
        package = self.service.load_context_snapshot(state["run_id"])
        input_payload = self._research_input(package)
        draft = self._invoke_model(
            ResearchDraft,
            prompt=(
                "Write a structured research draft from this immutable snapshot-only input. "
                "Every finding must cite evidence IDs from its own claim bundle, and markdown must "
                "include every cited evidence ID as [cite:<evidence_id>]. "
                "Do not add uncited facts.\n"
                f"Task analysis: {_canonical_json(state['task_analysis'])}\n"
                f"Plan: {_canonical_json(state['research_plan'])}\n"
                f"Research input: {_canonical_json(input_payload)}\n"
                "Return {title, executive_summary, findings, limitations, markdown, "
                "memory_proposal}."
            ),
        )
        self._record_llm_node(
            state,
            "research_draft",
            {
                "finding_count": len(draft.findings),
                "has_memory_proposal": draft.memory_proposal is not None,
            },
        )
        validation = self._validate_draft(state, draft, package)
        llm_calls_used = int(state.get("llm_calls_used", 0)) + 1
        if not validation.passed:
            draft, validation = self._repair_once(state, draft, package, validation)
            llm_calls_used += 1
        if not validation.passed:
            self.service.mark_needs_review(
                state["run_id"],
                error_message="; ".join(validation.errors) or "Citation validation failed.",
            )
            return self._progress(
                state,
                phase="needs_review",
                current_node="citation_validation",
                steps_used=int(state.get("steps_used", 4)) + 1,
                llm_calls_used=llm_calls_used,
                repair_attempts=self.service.get_run(state["run_id"]).repair_count,
                validation_summary=_validation_summary(validation),
                needs_review=True,
                error_code="citation_validation_failed",
                safe_error_detail="Citation validation failed after the permitted repair attempt.",
            )
        return self._finalize_validated_draft(
            state,
            draft,
            validation,
            llm_calls_used=llm_calls_used,
        )

    def _validate_draft(
        self, state: ResearchAgentState, draft: ResearchDraft, package: ContextPackage
    ) -> CitationValidation:
        self.service.mark_node(
            state["run_id"],
            status="validating",
            node_name="citation_validation",
            input_summary={"repair_count": self.service.get_run(state["run_id"]).repair_count},
        )
        validation = validate_research_draft(draft, package)
        validation.repair_applied = self.service.get_run(state["run_id"]).repair_count == 1
        self.service.record_node_completed(
            state["run_id"],
            node_name="citation_validation",
            output_summary={
                "passed": validation.passed,
                "error_count": len(validation.errors),
                "cited_evidence_count": len(validation.cited_evidence_ids),
            },
        )
        return validation

    def _repair_once(
        self,
        state: ResearchAgentState,
        prior_draft: ResearchDraft,
        package: ContextPackage,
        validation: CitationValidation,
    ) -> tuple[ResearchDraft, CitationValidation]:
        self.service.begin_repair(state["run_id"])
        input_payload = self._research_input(package)
        draft = self._invoke_model(
            ResearchDraft,
            prompt=(
                "Repair this research draft exactly once. Remove unsupported claims and use only "
                "evidence IDs and claim bundles in the immutable research input. "
                "Markdown citations "
                "must use [cite:<evidence_id>].\n"
                f"Validation errors: {_canonical_json(validation.model_dump(mode='json'))}\n"
                f"Prior draft: {_canonical_json(prior_draft.model_dump(mode='json'))}\n"
                f"Research input: {_canonical_json(input_payload)}\n"
                "Return the complete ResearchDraft JSON object."
            ),
        )
        self._record_llm_node(
            state,
            "repair",
            {"finding_count": len(draft.findings), "repair_count": 1},
        )
        return draft, self._validate_draft(state, draft, package)

    def _finalize_validated_draft(
        self,
        state: ResearchAgentState,
        draft: ResearchDraft,
        validation: CitationValidation,
        *,
        llm_calls_used: int,
    ) -> dict[str, Any]:
        run = self.service.get_run(state["run_id"])
        proposal_payload = None
        if run.options.create_memory_proposal and draft.memory_proposal is not None:
            proposal_payload = {
                "proposal_type": "decision_create",
                **draft.memory_proposal.model_dump(mode="json"),
                "metadata": {
                    "agent_run_id": run.id,
                    "context_snapshot_id": run.context_snapshot_id,
                },
            }
        finalization = self.service.finalize_run(
            state["run_id"],
            command=DomainFinalizationCommand(
                plugin=run.plugin,
                output_type="research_report",
                structured_output={
                    "kind": "research_report",
                    "draft": draft.model_dump(mode="json"),
                },
                rendered_text=draft.markdown,
                validation=validation.model_dump(mode="json"),
                artifact_type="research_report",
                memory_proposal_payload=proposal_payload,
            ),
        )
        return self._progress(
            state,
            phase="completed",
            current_node="complete",
            steps_used=int(state.get("steps_used", 4)) + 1,
            llm_calls_used=llm_calls_used,
            repair_attempts=run.repair_count,
            validation_summary=_validation_summary(validation),
            output_id=finalization.output.id,
            artifact_id=finalization.artifact.id,
            memory_proposal_id=(
                finalization.memory_proposal.id if finalization.memory_proposal else None
            ),
        )

    def _research_input(self, package: ContextPackage) -> dict[str, Any]:
        """Load a snapshot-only payload locally, never into graph state."""

        return research_input_payload(package)

    @staticmethod
    def _progress(state: ResearchAgentState, **updates: Any) -> dict[str, Any]:
        """Return only explicitly permitted compact checkpoint fields."""

        return updates

    def _mark_running(self, state: ResearchAgentState, node_name: str) -> None:
        self.service.mark_node(
            state["run_id"],
            status="running",
            node_name=node_name,
            input_summary={},
        )

    def _invoke_model(self, schema: type[BaseModel], *, prompt: str) -> BaseModel:
        started = time.perf_counter()
        # The field-name shorthand above does not describe nested findings or
        # bounded arrays. Supply the same schema we actually validate; this
        # clarifies the existing contract without relaxing it or repairing JSON.
        raw = self.llm.invoke(
            prompt + "\nRequired JSON schema: " + _canonical_json(schema.model_json_schema()),
            system_prompt=_RESEARCH_SYSTEM_PROMPT,
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            parsed = json.loads(_strip_json_fence(raw))
            result = schema.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ResearchValidationError(
                f"Research LLM returned invalid {schema.__name__} JSON: {exc}"
            ) from exc
        self._last_latency_ms = latency_ms
        return result

    def _record_llm_node(
        self, state: ResearchAgentState, node_name: str, output_summary: dict[str, Any]
    ) -> None:
        usage = getattr(self.llm, "last_usage", {}) or {}
        self.service.record_node_completed(
            state["run_id"],
            node_name=node_name,
            output_summary=output_summary,
            token_usage={
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            },
            latency_ms=getattr(self, "_last_latency_ms", None),
        )


def validate_research_draft(draft: ResearchDraft, package: ContextPackage) -> CitationValidation:
    """Validate every draft assertion against the exact snapshot evidence graph."""

    evidence_by_bundle = {
        bundle.claim.claim_id: {evidence.evidence_id for evidence in bundle.evidence}
        for bundle in package.knowledge.claim_bundles
    }
    valid_evidence = set().union(*evidence_by_bundle.values()) if evidence_by_bundle else set()
    cited_from_findings = {
        evidence_id for finding in draft.findings for evidence_id in finding.evidence_ids
    }
    cited_from_markdown = set(_CITATION_PATTERN.findall(draft.markdown))
    errors: list[str] = []
    if not draft.findings:
        errors.append("Research draft has no evidence-backed findings.")
    for finding in draft.findings:
        bundle_evidence = evidence_by_bundle.get(finding.claim_bundle_id)
        if bundle_evidence is None:
            errors.append(f"Unknown claim bundle: {finding.claim_bundle_id}")
            continue
        invalid = sorted(set(finding.evidence_ids) - bundle_evidence)
        if invalid:
            errors.append(
                f"Finding for {finding.claim_bundle_id} cites evidence outside its bundle: "
                f"{', '.join(invalid)}"
            )
    invalid_citations = sorted((cited_from_findings | cited_from_markdown) - valid_evidence)
    if invalid_citations:
        errors.append(f"Unknown evidence citation IDs: {', '.join(invalid_citations)}")
    missing_markdown = sorted(cited_from_findings - cited_from_markdown)
    if missing_markdown:
        errors.append(f"Markdown is missing citations: {', '.join(missing_markdown)}")
    if not cited_from_markdown:
        errors.append("Markdown contains no evidence citations.")
    return CitationValidation(
        passed=not errors,
        errors=errors,
        cited_evidence_ids=sorted(cited_from_findings & valid_evidence),
        invalid_citation_ids=invalid_citations,
    )


def _validation_summary(validation: CitationValidation) -> dict[str, Any]:
    """Return checkpoint-safe citation validation metadata.

    Error text can incorporate future validator details, so only stable counts,
    identifiers, and the bounded repair flag are retained in graph state.
    """

    return {
        "passed": validation.passed,
        "error_count": len(validation.errors),
        "cited_evidence_ids": validation.cited_evidence_ids,
        "invalid_citation_ids": validation.invalid_citation_ids,
        "repair_applied": validation.repair_applied,
    }


def _safe_plan_step(value: str) -> str:
    """Bound a user-visible plan description before it enters audit storage."""

    return " ".join(value.split())[:500]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strip_json_fence(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
