"""Deterministic evaluators for curated Phase 6 platform cases."""

from __future__ import annotations

import re
from typing import Any

from app.benchmarking.contracts import (
    AssertionResult,
    EvaluationCase,
    EvaluationCaseResult,
    FailureRecord,
    canonical_sha256,
)

_CITATION_ID = re.compile(r"\[cite:[^\]]+\]")


def evaluate_case(
    case: EvaluationCase, actual: dict[str, Any], *, duration_ms: float
) -> EvaluationCaseResult:
    """Evaluate a SUT outcome without model-assisted judging."""

    assertions: list[AssertionResult] = []
    expectation = case.expectation
    status = str(actual.get("status", "exception"))
    if expectation.expected_status is not None:
        _assert(
            assertions,
            "terminal_status",
            status == expectation.expected_status,
            expectation.expected_status,
            status,
        )
    if expectation.expected_error_contains is not None:
        error = _error_text(actual)
        _assert(
            assertions,
            "expected_error",
            expectation.expected_error_contains.casefold() in error.casefold(),
            expectation.expected_error_contains,
            error,
        )

    if case.target == "retrieval":
        _evaluate_retrieval(assertions, actual, expectation)
    elif case.target == "context":
        _evaluate_context(
            assertions,
            actual,
            expectation,
            repeat=case.scenario == "context_repeatable",
        )
    elif case.target in {"research_runtime", "game_runtime"}:
        _evaluate_runtime(assertions, actual, expectation, game=case.target == "game_runtime")
    elif case.target == "plugin_boundary":
        _evaluate_plugin_boundary(assertions, actual)
    elif case.target == "workspace_projection":
        _evaluate_workspace(assertions, actual, expectation)

    if expectation.expect_no_business_outputs:
        counts = actual.get("business_counts", {})
        zero_business_outputs = all(
            int(counts.get(key, 0)) == 0
            for key in ("agent_run_outputs", "artifacts", "memory_proposals")
        )
        _assert(
            assertions,
            "no_business_outputs",
            zero_business_outputs,
            {"agent_run_outputs": 0, "artifacts": 0, "memory_proposals": 0},
            counts,
        )

    passed = bool(assertions) and all(assertion.passed for assertion in assertions)
    failure = None
    if not passed:
        failure = FailureRecord(
            kind="oracle" if status != "exception" else "sut",
            message="; ".join(
                assertion.name for assertion in assertions if not assertion.passed
            )
            or "Evaluation case produced no assertions.",
            detail={
                assertion.name: {"expected": assertion.expected, "actual": assertion.actual}
                for assertion in assertions
                if not assertion.passed
            },
        )
    return EvaluationCaseResult(
        case_id=case.id,
        suite=case.suite,
        status="passed" if passed else "failed",
        assertions=assertions,
        metrics=_metrics(case, actual),
        semantic_digest=canonical_sha256(_semantic_payload(case, actual)),
        duration_ms=round(duration_ms, 2),
        failure=failure,
    )


def _evaluate_retrieval(assertions, actual: dict[str, Any], expectation) -> None:
    response = actual.get("response", {})
    evidence = list(response.get("evidence", []))
    paper_ids = [str(item.get("paper_id", "")) for item in evidence]
    for paper_id in expectation.expected_retrieved_paper_ids:
        _assert(
            assertions,
            f"retrieves_{paper_id}",
            paper_id in paper_ids,
            paper_id,
            paper_ids,
        )
    for paper_id in expectation.forbidden_retrieved_paper_ids:
        _assert(
            assertions,
            f"excludes_{paper_id}",
            paper_id not in paper_ids,
            f"not {paper_id}",
            paper_ids,
        )
    grounded = all(
        item.get("paper_id")
        and item.get("chunk_id")
        and int(item.get("page_start", 0)) >= 1
        and str(item.get("text", "")).strip()
        for item in evidence
    )
    _assert(assertions, "grounded_evidence", grounded, True, grounded)


def _evaluate_context(assertions, actual: dict[str, Any], expectation, *, repeat: bool) -> None:
    package = actual.get("package")
    if package is None:
        _assert(assertions, "context_package_present", False, "ContextPackage", None)
        return
    names = [
        bundle.entity.name
        for bundle in package.knowledge.claim_bundles
        if bundle.entity is not None
    ]
    for name in expectation.expected_claim_aliases:
        _assert(assertions, f"contains_{name}", name in names, name, names)
    if expectation.expected_knowledge_coverage is not None:
        _assert(
            assertions,
            "knowledge_coverage",
            package.diagnostics.knowledge_coverage == expectation.expected_knowledge_coverage,
            expectation.expected_knowledge_coverage,
            package.diagnostics.knowledge_coverage,
        )
    if expectation.expected_selected_bundle_count is not None:
        _assert(
            assertions,
            "selected_bundle_count",
            len(package.knowledge.claim_bundles) == expectation.expected_selected_bundle_count,
            expectation.expected_selected_bundle_count,
            len(package.knowledge.claim_bundles),
        )
    provenance_complete = all(
        bundle.evidence and bundle.chunks and bundle.documents and bundle.sources
        for bundle in package.knowledge.claim_bundles
    )
    _assert(
        assertions,
        "complete_evidence_provenance",
        provenance_complete,
        True,
        provenance_complete,
    )
    _assert(
        assertions,
        "token_budget_respected",
        package.token_usage.used <= package.token_usage.budget,
        f"<= {package.token_usage.budget}",
        package.token_usage.used,
    )
    if repeat:
        payloads = actual.get("payloads", [])
        _assert(
            assertions,
            "repeatable_runtime_payload",
            len(payloads) == 2 and payloads[0] == payloads[1],
            "identical normalized runtime payloads",
            "identical" if len(payloads) == 2 and payloads[0] == payloads[1] else "different",
        )


def _evaluate_runtime(assertions, actual: dict[str, Any], expectation, *, game: bool) -> None:
    run = actual.get("run")
    output = actual.get("output")
    if run is None:
        _assert(assertions, "agent_run_present", False, "AgentRun", None)
        return
    if expectation.expected_plugin_key:
        _assert(
            assertions,
            "plugin_pin",
            run.plugin.key == expectation.expected_plugin_key,
            expectation.expected_plugin_key,
            run.plugin.key,
        )
    if expectation.expected_repair_count is not None:
        _assert(
            assertions,
            "repair_count",
            run.repair_count == expectation.expected_repair_count,
            expectation.expected_repair_count,
            run.repair_count,
        )
    if expectation.expected_tool_call_count is not None:
        count = len(actual.get("tool_calls", []))
        _assert(
            assertions,
            "tool_call_count",
            count == expectation.expected_tool_call_count,
            expectation.expected_tool_call_count,
            count,
        )
    if expectation.expect_artifact is not None:
        _assert(
            assertions,
            "artifact_presence",
            (actual.get("artifact_id") is not None) == expectation.expect_artifact,
            expectation.expect_artifact,
            actual.get("artifact_id") is not None,
        )
    if expectation.expect_memory_proposal is not None:
        _assert(
            assertions,
            "proposal_presence",
            (actual.get("proposal_id") is not None) == expectation.expect_memory_proposal,
            expectation.expect_memory_proposal,
            actual.get("proposal_id") is not None,
        )
    if output is not None:
        validation = output.validation
        for field in expectation.required_validation_fields:
            _assert(
                assertions,
                f"validation_{field}",
                field in validation,
                f"validation.{field}",
                validation,
            )
        _assert(
            assertions,
            "validation_passed",
            validation.get("passed") is True,
            True,
            validation.get("passed"),
        )
    if game and output is not None and expectation.expected_value is not None:
        value = float(output.structured["game_model"]["value"])
        _assert(
            assertions,
            "formula_value",
            abs(value - expectation.expected_value) <= 1e-9,
            expectation.expected_value,
            value,
        )
    if "interrupted_status" in actual:
        _assert(
            assertions,
            "recovery_starts_failed",
            actual["interrupted_status"] == "failed",
            "failed",
            actual["interrupted_status"],
        )
        _assert(
            assertions,
            "recovery_does_not_repeat_snapshot_tool",
            actual.get("tool_calls_before_resume") == len(actual.get("tool_calls", [])),
            actual.get("tool_calls_before_resume"),
            len(actual.get("tool_calls", [])),
        )


def _evaluate_plugin_boundary(assertions, actual: dict[str, Any]) -> None:
    _assert(
        assertions,
        "generic_port_excludes_research_privileges",
        actual.get("forbidden_port_members") == [],
        [],
        actual.get("forbidden_port_members"),
    )
    _assert(
        assertions,
        "builtins_are_explicit",
        actual.get("plugin_keys") == ["game_modeling", "research"],
        ["game_modeling", "research"],
        actual.get("plugin_keys"),
    )


def _evaluate_workspace(assertions, actual: dict[str, Any], expectation) -> None:
    _evaluate_runtime(assertions, actual, expectation, game=False)
    projection = actual.get("projection")
    if projection is None:
        _assert(assertions, "artifact_projection_present", False, "ArtifactContentProjection", None)
        return
    _assert(
        assertions,
        "artifact_projection_matches_run",
        projection.run_id == actual["run"].id and projection.artifact_id == actual["artifact_id"],
        {"run_id": actual["run"].id, "artifact_id": actual["artifact_id"]},
        {"run_id": projection.run_id, "artifact_id": projection.artifact_id},
    )
    _assert(
        assertions,
        "artifact_projection_has_validation",
        projection.validation.get("passed") is True,
        True,
        projection.validation,
    )


def _assert(
    assertions: list[AssertionResult], name: str, passed: bool, expected: Any, actual: Any
) -> None:
    assertions.append(
        AssertionResult(name=name, passed=passed, expected=expected, actual=actual)
    )


def _error_text(actual: dict[str, Any]) -> str:
    if actual.get("exception"):
        return str(actual["exception"])
    run = actual.get("run")
    return str(getattr(run, "error_message", "") or "")


def _metrics(
    case: EvaluationCase, actual: dict[str, Any]
) -> dict[str, float | int | str | bool | None]:
    metrics: dict[str, float | int | str | bool | None] = {}
    if case.target == "retrieval":
        evidence = list(actual.get("response", {}).get("evidence", []))
        expected = set(case.expectation.expected_retrieved_paper_ids)
        retrieved = {str(item.get("paper_id", "")) for item in evidence}
        metrics["recall_at_5"] = (
            round(len(expected.intersection(retrieved)) / len(expected), 4) if expected else None
        )
        metrics["evidence_grounding"] = all(
            item.get("paper_id") and item.get("chunk_id") and item.get("text") for item in evidence
        )
    run = actual.get("run")
    package = actual.get("package")
    if package is not None:
        metrics["selected_claim_bundles"] = len(package.knowledge.claim_bundles)
        metrics["knowledge_coverage"] = package.diagnostics.knowledge_coverage
        metrics["provenance_complete"] = all(
            bundle.evidence and bundle.chunks and bundle.documents and bundle.sources
            for bundle in package.knowledge.claim_bundles
        )
    if run is not None:
        metrics["tool_call_count"] = len(actual.get("tool_calls", []))
        metrics["repair_count"] = run.repair_count
        metrics["terminal_status"] = run.status
        metrics["artifact_created"] = actual.get("artifact_id") is not None
        metrics["proposal_created"] = actual.get("proposal_id") is not None
        output = actual.get("output")
        if output is not None:
            metrics["validation_passed"] = output.validation.get("passed") is True
    return metrics


def _semantic_payload(case: EvaluationCase, actual: dict[str, Any]) -> dict[str, Any]:
    if case.target == "retrieval":
        return {
            "status": actual.get("status"),
            "evidence": [
                {
                    "paper_id": item.get("paper_id"),
                    "chunk_id": item.get("chunk_id"),
                    "text": item.get("text"),
                    "page_start": item.get("page_start"),
                }
                for item in actual.get("response", {}).get("evidence", [])
            ],
        }
    if case.target == "context":
        package = actual.get("package")
        return {
            "status": actual.get("status"),
            "entities": [
                bundle.entity.name if bundle.entity else None
                for bundle in getattr(package, "knowledge", {}).claim_bundles
            ]
            if package is not None
            else [],
            "quotes": [
                evidence.quote
                for bundle in getattr(package, "knowledge", {}).claim_bundles
                for evidence in bundle.evidence
            ]
            if package is not None
            else [],
        }
    run = actual.get("run")
    output = actual.get("output")
    if case.target == "game_runtime" and output is not None:
        model = output.structured["game_model"]
        return {
            "status": actual.get("status"),
            "value": model["value"],
            "unit": model["unit"],
            "patch_version": model["patch_version"],
            "parameters": model["parameters"],
        }
    if case.target in {"research_runtime", "workspace_projection"}:
        text = output.rendered_text if output is not None else ""
        validation = output.validation if output is not None else {}
        return {
            "status": actual.get("status"),
            "repair_count": getattr(run, "repair_count", None),
            "tool_names": [item.tool_name for item in actual.get("tool_calls", [])],
            "rendered_text": _CITATION_ID.sub("[cite:<fixture>]", text),
            "validation": {
                "passed": validation.get("passed"),
                "repair_applied": validation.get("repair_applied"),
                "cited_evidence_count": len(validation.get("cited_evidence_ids", [])),
            }
            if output is not None
            else None,
        }
    return {"status": actual.get("status"), "scenario": case.scenario}
