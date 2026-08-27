# Offline Demo Guide

## Goal

Demonstrate the system’s evidence and governance boundaries without accessing a real provider, operational SQLite database, checkpoint database, Vault, Qdrant, Neo4j, or the internet.

The demo produces a local **candidate evidence pack**. It is not a product UI demo, a human-approved benchmark baseline, or a claim about real-LLM quality.

## Prerequisites

- Python environment with project dependencies available;
- write access to a local evidence output directory;
- no provider key is required.

Do not use an operational data directory as the output location. The normal Phase 6 runner creates independent fixtures and blocks network sockets.

## Run the demo

~~~bash
./.venv/bin/python scripts/run_phase6_demo.py --output data/reports/phase6
~~~

The command prints a new immutable directory such as:

~~~text
data/reports/phase6/phase6-<timestamp>-<id>/
├── run.json
├── report.md
└── demo-summary.md
~~~

Open report.md after the command completes. A successful current demo has three passing cases and a passed isolation status.

## Suggested five-minute walkthrough

### 1. Establish the governance model

Start with the README architecture diagram. Explain that Knowledge and Memory are different trust domains, and that Context Builder creates a scoped, immutable ContextSnapshot before the runtime begins.

### 2. Run cited Research finalization

The research scenario sends a fixture task through the existing Context Builder, Research workflow, citation validator, and Platform Finalizer. The report shows that the terminal status is completed, cited evidence is present, and the resulting Artifact/MemoryProposal carries governed provenance.

Key message: a citation is accepted only when it belongs to the selected context evidence bundle.

### 3. Run deterministic Game Modeling

The Game Modeling scenario executes a pure, evidence-linked formula calculation through the shared Runtime and Finalizer. Its companion negative contract verifies that a patch-version mismatch fails closed and produces no business output.

Key message: a domain plugin reuses platform governance; it does not own a separate database or bypass validation.

### 4. Show Workspace Artifact provenance

The Workspace scenario projects an Artifact from its AgentRun and ContextSnapshot references. The evaluator checks that output, artifact, validation, and provenance agree.

Key message: the UI projection is derived from governed records, not a second source of truth.

### 5. Close with isolation and scope

Show the report’s isolation result and state the boundary precisely: historical fixture schema v15, current operational schema v18 not opened, network blocked, and sandbox removed. The evidence is a deterministic candidate with no human-approved baseline.

## Optional React Workspace walkthrough

The React Workspace is separate from this offline demo. It requires a declared Node/pnpm environment and its own test/build verification:

~~~bash
cd frontend
pnpm test
pnpm build
~~~

The current workbench verification ran 38 Vitest checks and a production build. These browser-component results remain separate from the offline evaluation candidate and do not imply real-provider semantic quality.

## Presenter safety checklist

- Never display .env, local operational data, or generated frontend build assets.
- Do not claim an accepted Phase 6 baseline or regression result.
- Do not claim live model quality, external retrieval quality, or production database validation.
- Do not run production v0009 or any unreviewed migration during the demo.
- Keep the generated report as evidence; it contains no need for external-provider credentials.
