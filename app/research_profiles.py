"""Default project research policy; explicit API choices bypass this policy."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.context.models import ContextBuildRequest

ResearchProfileName = Literal["standard", "classic"]
RETRIEVAL_FIELDS = {
    "enable_vector_candidates", "enable_graph_candidates", "retrieval_strategy",
    "query_planning", "context_neighbors", "evidence_reranking", "reading_format",
}


@dataclass(frozen=True)
class ResearchProfile:
    name: ResearchProfileName
    workflow: str
    token_budget: int
    max_steps: int = 10
    max_tool_calls: int = 3

    def context_request(self, request: ContextBuildRequest) -> ContextBuildRequest:
        values = request.model_dump(include={
            "task_id", "project_id", "snapshot_id", "pinned_claim_ids",
        })
        if self.name == "standard":
            values.update(
                enable_vector_candidates=True, retrieval_strategy="dense-v1",
                evidence_reranking="coverage-v2", reading_format="inline-v1", max_tokens=16000,
            )
        if "max_tokens" in request.model_fields_set:
            values["max_tokens"] = request.max_tokens
        return ContextBuildRequest(**values)

    def run_defaults(self) -> dict[str, int | str]:
        return {
            "workflow": self.workflow, "token_budget": self.token_budget,
            "max_steps": self.max_steps, "max_tool_calls": self.max_tool_calls,
        }


CLASSIC = ResearchProfile("classic", "research", 6000)
STANDARD = ResearchProfile("standard", "research_v11", 1048576)


def resolve_research_profile(
    default: ResearchProfileName, *, has_scope: bool,
    reading_format: str | None = None, notices: Sequence[str] = (),
) -> tuple[ResearchProfile, str | None]:
    if default == "classic":
        return CLASSIC, None
    if not has_scope:
        return CLASSIC, "no_explicit_knowledge_scope"
    if any(n in {"vector_unavailable", "vector_not_configured"} for n in notices):
        return CLASSIC, "vector_unavailable"
    if reading_format == "legacy":
        return CLASSIC, "legacy_snapshot"
    return STANDARD, None
