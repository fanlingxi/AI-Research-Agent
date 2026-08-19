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

The ordinary report workflow does not require a separately started generic Worker. A browser action persists report creation and its `auto_execute` intent in one SQLite transaction. The built-in dispatcher claims only marked report jobs, continuously recovers an expired lease, and keeps the lease alive during long model calls. Repeated clicks cannot create another active attempt, and an explicitly selected report cannot consume an older ingestion, report, or AgentRun. Failed retries retain the report identity while clearing stale content, evidence, evaluation, and error state in the same transaction as the new dispatch intent.

The general Worker remains the batch-execution path and shares the same report heartbeat. Attempt-number fencing protects every report stage write; report content and its durable job terminal state commit atomically, so an expired executor cannot overwrite a newer attempt. The Runtime page exposes whether the built-in report executor is online.

## Next optimization priorities

1. Give PDF ingestion the same create/start/progress/retry loop, including drag-and-drop files, URL preflight, deduplication, and per-document progress.
2. Add a project-research command mode that can create or reuse a WorkspaceTask and ContextSnapshot automatically, while keeping the existing Agent panel as the governed advanced path.
3. Add an idempotency key to command submission, queue position/attempt details, task filters, and executor last-heartbeat time for stronger recovery guidance.
4. Replace large Collection chip groups with searchable multi-select, remember the last evidence scope, and paginate or virtualize long report and runtime histories.
5. Turn the mobile sidebar into a drawer or bottom navigation and finish Chinese terminology normalization across Project, Task, AgentRun, Artifact, and Review surfaces.

## UI safety model

The browser receives bounded projections rather than checkpoint databases, arbitrary internal files, or unbounded raw runtime state. It cannot use a UI action to bypass knowledge review, proposal review, plugin pinning, or runtime validation.

## Development entry points

The React application is in frontend/ and uses React, TypeScript, Vite, Vitest, and TanStack Query. It is run separately with a Node/pnpm toolchain:

~~~bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
~~~

Run pnpm test and pnpm build in a declared Node environment before release. Node/npm were not available during the latest release audit, so those commands were not rerun there.

## Compose compatibility note

The current docker-compose.yml starts the API, worker, supporting services, and a legacy-compatible Streamlit UI. It does not package the React Workspace. A deployment guide must choose the intended UI rather than presenting the two as the same application.
