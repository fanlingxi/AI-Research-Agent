"""Run the local, offline Phase 6 Evaluation Plane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.benchmarking.compare import compare_to_baseline
from app.benchmarking.contracts import EvaluationBaseline
from app.benchmarking.report import render_markdown
from app.benchmarking.runner import load_manifest, run_manifest, write_run


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated Phase 6 Evaluation cases.")
    parser.add_argument(
        "--manifest", default="benchmarks/phase6/cases/v1/manifest.json", help="Case manifest"
    )
    parser.add_argument("--output", default="data/reports/phase6", help="Result output root")
    parser.add_argument(
        "--profile",
        choices=("deterministic-contract", "repeatability", "demo-evidence"),
        default="deterministic-contract",
    )
    parser.add_argument("--case", action="append", default=[], help="Case ID; may repeat")
    parser.add_argument("--baseline", default="", help="Approved baseline JSON to compare")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    run = run_manifest(manifest, profile=args.profile, case_ids=set(args.case) or None)
    comparison = None
    if args.baseline:
        baseline_raw = Path(args.baseline).read_text(encoding="utf-8")
        comparison = compare_to_baseline(
            EvaluationBaseline.model_validate_json(baseline_raw), run
        )
    target = write_run(run, args.output)
    (target / "report.md").write_text(render_markdown(run, comparison), encoding="utf-8")
    if comparison is not None:
        (target / "baseline-comparison.json").write_text(
            json.dumps(comparison.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(target)


if __name__ == "__main__":
    main()
