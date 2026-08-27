from __future__ import annotations

from pathlib import Path

from app.benchmarking.compare import compare_to_baseline
from app.benchmarking.contracts import EvaluationBaseline
from app.benchmarking.report import render_markdown
from app.benchmarking.runner import load_manifest, run_manifest, write_run

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "phase6" / "cases" / "v1" / "manifest.json"
)


def test_phase6_curated_contract_suite_is_offline_and_passing() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    run = run_manifest(manifest)

    assert len(manifest.cases) == 13
    assert run.summary == {"passed": 13, "failed": 0, "skipped": 0, "error": 0}
    assert run.isolation.passed
    assert run.environment.schema_version == 18
    assert run.environment.operational_schema_baseline == 18
    assert run.environment.llm_mode == "scripted_fixture"


def test_phase6_repeatability_rebuilds_fresh_sandboxes() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    run = run_manifest(
        manifest,
        profile="repeatability",
        case_ids={"context-repeatable-payload", "research-cited-finalization"},
    )

    assert run.summary == {"passed": 2, "failed": 0, "skipped": 0, "error": 0}
    assert run.isolation.passed
    assert all(case.metrics["repeatability_stable"] is True for case in run.cases)


def test_phase6_baselines_are_manual_and_detect_regression(tmp_path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    run = run_manifest(manifest, case_ids={"game-deterministic-formula"})
    baseline = EvaluationBaseline(
        baseline_id="phase6-test-baseline",
        manifest_sha256=run.manifest_sha256,
        suite_version=run.suite_version,
        profile=run.profile,
        accepted_run=run,
        approved_by="Test Reviewer",
        approved_at="2026-08-07T00:00:00+00:00",
    )

    accepted = compare_to_baseline(baseline, run)
    regressed_run = run.model_copy(deep=True)
    regressed_run.cases[0].status = "failed"
    regressed = compare_to_baseline(baseline, regressed_run)
    result_path = write_run(run, tmp_path)

    assert accepted.comparable is True
    assert accepted.passed is True
    assert regressed.comparable is True
    assert regressed.passed is False
    assert "case regressed" in regressed.regressions[0]
    assert (result_path / "run.json").exists()
    assert "Phase 6 Evaluation Report" in render_markdown(run, accepted)


def test_phase6_demo_profile_is_a_reproducible_subset() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    run = run_manifest(
        manifest,
        profile="demo-evidence",
        case_ids={
            "research-cited-finalization",
            "game-deterministic-formula",
            "workspace-artifact-provenance",
        },
    )

    assert run.summary == {"passed": 3, "failed": 0, "skipped": 0, "error": 0}
    assert run.isolation.passed
