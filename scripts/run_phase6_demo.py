"""Produce an offline Phase 6 demo evidence pack from curated cases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.benchmarking.report import render_markdown  # noqa: E402
from app.benchmarking.runner import load_manifest, run_manifest, write_run  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an offline Phase 6 demo evidence pack.")
    parser.add_argument("--output", default="data/reports/phase6", help="Evidence output root")
    args = parser.parse_args()
    demo = json.loads((ROOT / "benchmarks/phase6/demo/manifest.json").read_text(encoding="utf-8"))
    manifest = load_manifest(ROOT / "benchmarks/phase6/cases/v1/manifest.json")
    run = run_manifest(
        manifest, profile="demo-evidence", case_ids=set(demo["case_ids"])
    )
    target = write_run(run, ROOT / args.output)
    (target / "report.md").write_text(render_markdown(run), encoding="utf-8")
    summary = "\n".join(
        [
            "# Phase 6 Demo Evidence",
            "",
            "This local, offline fixture demonstrates Context scope, cited Research finalization,",
            "deterministic Game Modeling, and Workspace Artifact provenance through existing",
            "services.",
            "It does not claim real-provider quality or production-database validation.",
            "",
            f"Run: `{run.run_id}`",
        ]
    )
    (target / "demo-summary.md").write_text(summary + "\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
