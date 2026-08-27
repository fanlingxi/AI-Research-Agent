# Phase Status — Historical Execution Record

## Current release state

Phases 1–6 are implemented. The current public packaging title is **Evidence-Grounded Personal Knowledge Agent Workspace**.

Verified engineering status:

- backend pytest: 181 passed, 3 skipped;
- React Vitest: 38 passed; production build passed;
- Ruff, pip check, git diff --check, and Docker Compose configuration: passed;
- React browser acceptance passed at 1440×900 and 1920×1080 with SPA deep-link refresh and no horizontal overflow;
- the isolated 15-PDF corpus retained 15 Sources, 15 Documents, 707 Chunks, and 687 pages after migration; a bounded real-provider report used 8 evidence spans from 5 papers and passed the report quality gate;
- Phase 6 deterministic evaluation candidate: 13 passed, 0 failed/error/skipped, 4/4 isolation attestations true;
- Phase 6 demo candidate: 3 passed, 0 failed/error/skipped, isolation passed.

The current Phase 6 result is not an accepted baseline. Human approval is required before any baseline file is created. The application now targets operational schema v18; historical evaluation candidates retain their original schema metadata and did not open the operational database.

## Document status

The other files in docs/codex/ are historical task prompts and planning context. They are preserved for provenance and should not be interpreted as current implementation status or public product documentation.

Use the following documents as the current source of truth:

- [Current architecture](../architecture/01_PROJECT_SPEC.md)
- [Phase 6 benchmark summary](../demo/BENCHMARK_SUMMARY.md)
- [Demo guide](../demo/DEMO_GUIDE.md)
- [Release-facing README](../../README.md)
