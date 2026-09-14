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

The research-workspace engineering closeout is implemented; report semantics and
interaction acceptance remain open. See [current progress and limitations](PROJECT_STATUS.md)
and [installation, verification, and demo](GETTING_STARTED.md).

As of 2026-09-14: backend 581 passed / 3 skipped; frontend 53 tests passed and
production build passed. The application schema is v20. These checks establish
engineering behavior, not model accuracy. New retrieval/generation strategies
remain opt-in; the latest fixed comparison did not justify changing defaults.

Phase 6 documents and candidate outputs below are historical. Their original
schema-v15 fixture metadata and counts are preserved; there is no human-approved
Phase 6 baseline or independent semantic quality claim.

## Engineering and migration background

- [Engineering guidelines](engineering/11_ENGINEERING_GUIDELINES.md)
- [Migration background](migration/02_MIGRATION_PLAN.md)
- [Historical design and development records](legacy/)

The migration and Codex prompt directories preserve the project’s evolution. They are not the source of truth for the current public architecture; use the architecture and demo documents above.
