"""Research-only extensions to the generic Domain Runtime port."""

from __future__ import annotations

from typing import Any

from app.domain_plugins.ports import DomainRuntimePort


class ResearchRuntimePort(DomainRuntimePort):
    """Historical Phase 3 controls unavailable to other Domain Plugins.

    Research is the only current plugin with an LLM workflow, a one-time
    citation repair, and the Phase 3A placeholder completion path. Keeping
    these methods out of :class:`DomainRuntimePort` prevents a future domain
    plugin from bypassing generic Platform Finalization.
    """

    def __init__(self, service: Any) -> None:
        super().__init__(service)
        self.__research_service = service

    @property
    def llm(self) -> Any:
        return self.__research_service.llm

    def complete_foundation_output(self, run_id: str, **kwargs: Any):
        return self.__research_service.complete_foundation_output(run_id, **kwargs)

    def begin_repair(self, run_id: str):
        return self.__research_service.begin_repair(run_id)

    def begin_generation_attempt(self, run_id, slot, digest):
        return self.__research_service.begin_generation_attempt(run_id, slot, digest)

    def finish_generation_attempt(self, run_id, slot, **kwargs):
        return self.__research_service.finish_generation_attempt(run_id, slot, **kwargs)

    def mark_needs_review(self, run_id: str, **kwargs: Any):
        return self.__research_service.mark_needs_review(run_id, **kwargs)
