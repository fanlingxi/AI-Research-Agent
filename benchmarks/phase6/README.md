# Phase 6 Evaluation Plane

Phase 6 is a local-first, offline-first deterministic evaluation package. It is not an
Agent Runtime, a Plugin implementation, a business-data store, or a product feature.

Every run creates a fresh temporary SQLite database, checkpoint database, Vault path and
cache-equivalent fixture root. It blocks network sockets, does not open the operational
database, and drives the existing Context Builder, Agent Runtime, validators, finalizer,
Plugin registry and Workspace projections.

Run the full contract suite:

```bash
.venv/bin/python -m app.benchmarking \
  --manifest benchmarks/phase6/cases/v1/manifest.json \
  --output data/reports/phase6
```

Run the selected demo evidence scenarios:

```bash
.venv/bin/python scripts/run_phase6_demo.py --output data/reports/phase6
```

Run fresh-sandbox repeatability checks:

```bash
.venv/bin/python -m app.benchmarking --profile repeatability \
  --case context-repeatable-payload \
  --case research-cited-finalization
```

Each result directory contains `run.json` and `report.md`. Results are immutable: a run ID
may not be overwritten. The report explicitly records fixture schema v15 and operational
schema baseline v14; it never applies a migration to the operational database.

## Baselines

Only a human may promote a fully passing, isolated run into a baseline:

```bash
.venv/bin/python scripts/accept_phase6_baseline.py \
  data/reports/phase6/<run-id>/run.json \
  --approved-by "Reviewer name" \
  --baseline-id phase6-v1-approved
```

The command rejects failed or non-isolated runs and will not overwrite an existing baseline.
Candidate comparison requires the same manifest digest, suite version, contract version and
profile; otherwise it is reported as not comparable.

## Evidence boundary

Research uses a local scripted transcript so the real workflow can prove citation validation,
one-time repair, checkpoint recovery, artifact/proposal finalization and fail-closed behavior.
It is not a claim about real-provider or open-domain model quality. The demo is similarly an
offline reproducibility package. A future browser demonstration must be run in an explicitly
declared Node environment and cannot be inferred from these results.
