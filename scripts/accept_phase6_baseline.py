"""Create an explicitly human-approved Phase 6 baseline from one result run."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.benchmarking.contracts import EvaluationBaseline, EvaluationRun  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Accept a reviewed Phase 6 Evaluation baseline.")
    parser.add_argument("run_json", help="Path to a reviewed Phase 6 run.json")
    parser.add_argument("--approved-by", required=True, help="Human approver name")
    parser.add_argument("--baseline-id", required=True, help="Immutable baseline identity")
    parser.add_argument("--output-dir", default="benchmarks/phase6/baselines")
    args = parser.parse_args()

    run = EvaluationRun.model_validate_json(Path(args.run_json).read_text(encoding="utf-8"))
    if not run.isolation.passed or any(case.status != "passed" for case in run.cases):
        raise SystemExit(
            "Only a fully passing, isolated Evaluation run can be accepted as a baseline."
        )
    baseline = EvaluationBaseline(
        baseline_id=args.baseline_id,
        manifest_sha256=run.manifest_sha256,
        suite_version=run.suite_version,
        profile=run.profile,
        accepted_run=run,
        approved_by=args.approved_by,
        approved_at=datetime.now(tz=UTC).isoformat(),
    )
    output = Path(args.output_dir) / f"{args.baseline_id}.json"
    if output.exists():
        raise SystemExit(f"Baseline already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
