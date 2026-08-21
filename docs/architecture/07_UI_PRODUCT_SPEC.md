# Project Workspace UI

## Purpose

The React Project Workspace presents read-oriented projections over governed platform records. It helps a user inspect work and act on explicit review flows; it is not a second source of truth and it does not expose checkpoint internals.

## Implemented routes

| Route | Projection |
| --- | --- |
| / | Dashboard |
| /projects/:projectId/:section | Project workspace sections |
| /projects/:projectId/tasks/:taskId | Task detail |
| /agent-runs/:runId | AgentRun trace |
| /artifacts/:artifactId | Artifact content |
| /review | Memory-proposal review |
| /knowledge | Knowledge ingestion, search, and candidate review |
| /reports | Evidence-report creation, execution, progress, and retry |
| /runtime | Service health and cross-workflow task history |

The UI uses the existing Workspace, Memory, Context, and Agent API projections. Workspace services validate provenance for ContextSnapshot evidence and Artifact content before returning a projection.

## Workspace surfaces

- Dashboard: a direct research-instruction launcher, active projects, recent tasks, runs, artifacts, and pending proposals.
- Project workspace: project status, tasks, scoped knowledge, memory, artifacts, and enabled plugins.
- Task detail: ContextSnapshot preview/creation, selected plugin, AgentRun status, and trace.
- AgentRun: lifecycle, tool-call audit, validation, and output provenance.
- Artifact: content plus run/context relation.
- Review: explicit MemoryProposal approval or rejection flow.
- Reports: submit-and-execute by default, targeted start for existing queued reports, adaptive polling, visible generation stage, evidence pack, download, and in-place failed retry.
- Runtime: health and queue summaries plus deep links back to actionable report and ingestion views.

## Browser dispatch boundary

The ordinary ingestion and report workflows do not require a separately started generic Worker. A browser action persists resource creation and its `auto_execute` intent in one SQLite transaction. Each built-in dispatcher claims only its marked resource jobs, continuously recovers an expired lease, and keeps the lease alive during long parsing or model calls. Repeated clicks cannot create another active attempt, and an explicitly selected resource cannot consume an older ingestion, report, or AgentRun. Failed retries retain the resource identity while resetting stale execution state in the same transaction as the new dispatch intent.

The general Worker remains the batch-execution path and shares the same heartbeat wrappers. Attempt-number fencing protects ingestion candidate replacement and every report stage write. Each resource result and its durable job terminal state commit atomically, so an expired executor cannot overwrite a newer attempt. The ingestion dispatcher also drains the durable projection outbox, allowing reviewed facts to reach Qdrant, Neo4j, and the Vault without making those projections the business source of truth. The Runtime page exposes both built-in executors.

## Next optimization priorities

1. Add drag-and-drop PDF selection, URL preflight, deduplication, and per-document parsing/extraction progress to the now-complete ingestion create/start/retry loop.
2. Add a project-research command mode that can create or reuse a WorkspaceTask and ContextSnapshot automatically, while keeping the existing Agent panel as the governed advanced path.
3. Add an idempotency key to command submission, queue position/attempt details, task filters, and executor last-heartbeat time for stronger recovery guidance.
4. Replace large Collection chip groups with searchable multi-select, remember the last evidence scope, and add server-side pagination or virtualization beyond the current bounded, internally scrolling histories.
5. Finish Chinese terminology normalization and dense-layout acceptance across Project, Task, AgentRun, Artifact, Review, and Runtime at the supported 1440×900 and 1920×1080 PC viewports. Mobile navigation and responsive adaptation are outside the current product scope.

## UI safety model

The browser receives bounded projections rather than checkpoint databases, arbitrary internal files, or unbounded raw runtime state. It cannot use a UI action to bypass knowledge review, proposal review, plugin pinning, or runtime validation.

## Development and production entry points

The React application is in frontend/ and uses React, TypeScript, Vite, Vitest, and TanStack Query. The supported local product stack builds it into an Nginx service and starts every dependency with one command:

~~~bash
make up
~~~

The PC workspace is available on port 5173, proxies `/api` to FastAPI on port 8000, and uses an SPA fallback for deep links. Streamlit remains a separately configured operations console on port 8501. Direct Vite development remains available with a Node/pnpm toolchain:

~~~bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
~~~

Run pnpm test and pnpm build in a declared Node environment before release. Browser acceptance targets 1440×900 and 1920×1080 PC viewports; mobile acceptance is not required for this milestone.

## Compose deployment

The current docker-compose.yml starts the React/Nginx web service, API, worker, Qdrant, Neo4j, and the Streamlit operations console. React is the only daily product entry. Browser-facing URLs are configuration rather than hard-coded component constants.
