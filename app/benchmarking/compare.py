"""Strict comparison of candidate Evaluation runs against approved baselines."""

from __future__ import annotations

from app.benchmarking.contracts import BaselineComparison, EvaluationBaseline, EvaluationRun


def compare_to_baseline(
    baseline: EvaluationBaseline, candidate: EvaluationRun
) -> BaselineComparison:
    reasons: list[str] = []
    if baseline.contract_version != candidate.contract_version:
        reasons.append("evaluation contract version differs")
    if baseline.manifest_sha256 != candidate.manifest_sha256:
        reasons.append("case manifest digest differs")
    if baseline.suite_version != candidate.suite_version:
        reasons.append("suite version differs")
    if baseline.profile != candidate.profile:
        reasons.append("evaluation profile differs")
    if reasons:
        return BaselineComparison(comparable=False, passed=False, reasons=reasons)

    baseline_cases = {item.case_id: item for item in baseline.accepted_run.cases}
    candidate_cases = {item.case_id: item for item in candidate.cases}
    regressions: list[str] = []
    for case_id, reference in baseline_cases.items():
        current = candidate_cases.get(case_id)
        if current is None:
            regressions.append(f"missing baseline case: {case_id}")
        elif reference.status == "passed" and current.status != "passed":
            regressions.append(
                f"case regressed: {case_id} ({reference.status} -> {current.status})"
            )
    if not candidate.isolation.passed:
        regressions.append("candidate isolation attestation failed")
    return BaselineComparison(
        comparable=True,
        passed=not regressions,
        regressions=regressions,
        metric_deltas=_metric_deltas(baseline.accepted_run, candidate),
    )


def _metric_deltas(baseline: EvaluationRun, candidate: EvaluationRun) -> dict[str, float]:
    reference = {item.case_id: item for item in baseline.cases}
    deltas: dict[str, float] = {}
    for current in candidate.cases:
        prior = reference.get(current.case_id)
        if prior is None:
            continue
        for key, value in current.metrics.items():
            before = prior.metrics.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(
                before, (int, float)
            ) and not isinstance(before, bool):
                deltas[f"{current.case_id}.{key}"] = round(float(value) - float(before), 6)
    return deltas
