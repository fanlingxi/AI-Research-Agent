# Phase 6 历史确定性评测记录

> 以下记录对应 2026-08 的实验，保留原始次数、标识和结论；其中 v18 是当时应用版本。当前实现与实测结果见[项目状态](../PROJECT_STATUS.md)。

## Status

This document records **candidate benchmark results**. No human-approved baseline exists, and these results must not be used to claim a regression comparison.

| Field | Verified value |
| --- | --- |
| Full experiment | phase6-20260807T101302Z-7b90a436e6 |
| Suite | phase6-curated-v1 |
| Manifest SHA-256 | 5c1a4878d037c7df0187b8f50daf2a8b96e6fcadc5f3834a307ee0061f1705c2 |
| Result | 13 passed, 0 failed, 0 error, 0 skipped |
| Isolation | 4/4 attestations true |
| Demo experiment | phase6-20260807T101505Z-534b3819bd |
| Demo result | 3 passed, 0 failed, 0 error, 0 skipped; isolation passed |
| Baseline status | No human-approved Phase 6 baseline exists |

The full candidate includes retrieval, scope filtering, ContextSnapshot repeatability, citation finalization and repair, citation fail-closed behavior, checkpoint recovery/idempotency, Game Modeling calculation, patch-version failure, plugin isolation, and Workspace Artifact provenance.

## What the result proves

Each versioned case creates a new temporary fixture and invokes existing production service paths:

~~~text
Curated case
  → isolated SQLite/checkpoint/vault fixture
  → Context Builder / Agent Runtime / Plugin / Validator / Finalizer / Workspace
  → deterministic evaluator
  → immutable candidate report
~~~

The full candidate passed these isolation attestations:

- real operational database fingerprint unchanged;
- evaluation settings and paths contained within the fixture;
- network connection attempts blocked;
- temporary sandbox removed after execution.

The historical candidate fixture schema is v15. The operational SQLite contract at the time was v18, is not opened by the evaluation, and has no migration applied by this workflow. Historical fixture metadata remains unchanged and does not describe the current application schema.

## What the result does not prove

- real-provider or open-domain LLM quality;
- internet, Qdrant, Neo4j, or production retrieval availability;
- production AgentRun execution;
- browser/UI validation;
- a human-approved benchmark baseline or a regression comparison.

Research scenarios use scripted local transcripts to make governance deterministic. This validates platform behavior such as evidence validation, repair limits, and finalization boundaries; it is not an accuracy score for a provider model.

## Reproduce locally

Use a Python environment with the project dependencies installed:

~~~bash
./.venv/bin/python -m app.benchmarking   --manifest benchmarks/phase6/cases/v1/manifest.json   --output data/reports/phase6
~~~

Each run receives a new ID and writes run.json and report.md under the selected output root. Results are immutable; do not overwrite an existing run directory.

The selected demo subset can be reproduced with:

~~~bash
./.venv/bin/python scripts/run_phase6_demo.py --output data/reports/phase6
~~~

See [演示指南](../demo/DEMO_GUIDE.md) for a presentation flow and [../../benchmarks/phase6/README.md](../../benchmarks/phase6/README.md) for the evaluation runner boundary.

## Baseline governance

A passing candidate is intentionally not a baseline. Baseline promotion requires an explicit human reviewer and the repository’s manual acceptance command. Until that review occurs, describe results only as deterministic evaluation candidates.
