"""Human-readable evidence reports derived from structured Evaluation results."""

from __future__ import annotations

from app.benchmarking.contracts import BaselineComparison, EvaluationRun


def render_markdown(run: EvaluationRun, comparison: BaselineComparison | None = None) -> str:
    summary = run.summary
    lines = [
        "# Phase 6 Evaluation Report",
        "",
        f"- Run: `{run.run_id}`",
        f"- Profile: `{run.profile}`",
        f"- Suite: `{run.suite_version}`",
        f"- Manifest SHA-256: `{run.manifest_sha256}`",
        f"- Fixture schema version: `{run.environment.schema_version}`",
        "- Operational schema baseline: "
        f"`v{run.environment.operational_schema_baseline}` (not opened)",
        f"- LLM mode: `{run.environment.llm_mode}`",
        "",
        "## Result",
        "",
        f"- Passed: {summary['passed']}",
        f"- Failed: {summary['failed']}",
        f"- Errors: {summary['error']}",
        f"- Skipped: {summary['skipped']}",
        f"- Isolation: {'passed' if run.isolation.passed else 'FAILED'}",
        "",
        "## Cases",
        "",
        "| Case | Status | Duration (ms) | Semantic digest |",
        "|---|---:|---:|---|",
    ]
    lines.extend(
        "| "
        f"`{case.case_id}` | {case.status} | {case.duration_ms:.2f} "
        f"| `{case.semantic_digest or '-'}` |"
        for case in run.cases
    )
    if comparison is not None:
        lines.extend(["", "## Baseline comparison", ""])
        if not comparison.comparable:
            lines.append("Not comparable: " + "; ".join(comparison.reasons))
        else:
            lines.append("Passed" if comparison.passed else "FAILED")
            lines.extend(f"- {item}" for item in comparison.regressions)
    failures = [case for case in run.cases if case.failure]
    if failures:
        lines.extend(["", "## Failures", ""])
        lines.extend(
            f"- `{case.case_id}` ({case.failure.kind}): {case.failure.message}" for case in failures
        )
    lines.extend(
        [
            "",
            "## Scope",
            "",
            "This offline result proves only the versioned local fixtures and "
            "production service paths exercised above. Scripted fixture LLMs validate "
            "platform governance, not open-domain model quality.",
            "",
        ]
    )
    return "\n".join(lines)
