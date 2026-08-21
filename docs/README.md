# Documentation

This directory distinguishes the current implementation from historical planning material.

## Current public documentation

- [Project specification and architecture](architecture/01_PROJECT_SPEC.md)
- [Data and governance model](architecture/03_DATA_MODEL_SPEC.md)
- [ContextSnapshot contract](architecture/04_CONTEXT_ENGINEERING_SPEC.md)
- [Agent Runtime](architecture/05_AGENT_RUNTIME_SPEC.md)
- [Domain Plugins](architecture/06_DOMAIN_PLUGIN_SPEC.md)
- [React Project Workspace](architecture/07_UI_PRODUCT_SPEC.md)
- [Phase 6 benchmark summary](demo/BENCHMARK_SUMMARY.md)
- [Offline demo guide](demo/DEMO_GUIDE.md)
- [Resume and interview brief](demo/RESUME_PROJECT_BRIEF.md)

The product title used in public material is **Evidence-Grounded Personal Knowledge Agent Workspace** (证据约束的本地优先个人知识 Agent 工作台). The repository package name remains research-knowledge-core for compatibility.

## Current status

Phases 1–6 are complete. The documented engineering verification is:

- backend pytest: 112 passed, 3 skipped;
- Ruff, pip check, git diff --check, and docker compose config --quiet: passed;
- Phase 6 full deterministic evaluation candidate: 13 passed, 0 failed/error/skipped, 4/4 isolation attestations true;
- Phase 6 demo candidate: 3 passed, 0 failed/error/skipped, isolation passed.

There is no human-approved Phase 6 baseline. Public material must say **candidate** and must not imply a baseline comparison or regression comparison. The current application schema contract is v16; historical candidate outputs retain their original fixture version and the evaluation runner never opens the operational database.

## Engineering and migration background

- [Engineering guidelines](engineering/11_ENGINEERING_GUIDELINES.md)
- [Migration background](migration/02_MIGRATION_PLAN.md)
- [Historical design and development records](legacy/)

The migration and Codex prompt directories preserve the project’s evolution. They are not the source of truth for the current public architecture; use the architecture and demo documents above.
