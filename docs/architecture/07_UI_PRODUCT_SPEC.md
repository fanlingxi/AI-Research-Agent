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
- Runtime: API/Worker/LLM/Qdrant/Neo4j/Vault health, executor heartbeat, unified work pagination, AgentRun/report/ingestion/Collection/projection state, queue position, lease ownership, targeted recovery, and deep links.

## Browser dispatch boundary

A browser action persists resource creation and queue intent; it never makes FastAPI own a long-running model, PDF, AgentRun, Collection synchronization, or projection operation. The independent unified Worker is the sole executor for all five work kinds. Explicit start and retry actions raise the selected resource's priority without claiming or executing another queued resource in the request process.

Every claimed job records an executor identity, attempt, priority, and lease. The Worker renews all work at one third of the lease and uses `job_id + attempt + lease_owner` as its fencing token. Ingestion and report stage writes, AgentRun lifecycle writes and Platform Finalizer, and projection completion/failure validate that token in the same SQLite transaction as the business mutation. The Worker periodically recovers only expired leases; an API restart does not affect queued or running ownership. Default concurrency remains one to bound SQLite contention and real-LLM cost.

## Next optimization priorities

1. Add Worker heartbeat, queue position/attempt details, projection backlog, task filters, and pagination to the React Runtime surface.
2. Add a project-research command mode that can create or reuse a WorkspaceTask and ContextSnapshot automatically, while keeping the existing Agent panel as the governed advanced path.
3. Add an idempotency key to command submission and make command orchestration recoverable by phase.
4. Replace large Collection chip groups with searchable multi-select, remember the last evidence scope, and add server-side pagination or virtualization beyond the current bounded, internally scrolling histories.
5. Finish Chinese terminology normalization and dense-layout acceptance across Project, Task, AgentRun, Artifact, Review, and Runtime at the supported 1440×900 and 1920×1080 PC viewports. Mobile navigation and responsive adaptation are outside the current product scope.

## UI safety model

The browser receives bounded projections rather than checkpoint databases, arbitrary internal files, or unbounded raw runtime state. It cannot use a UI action to bypass knowledge review, proposal review, plugin pinning, or runtime validation.

## Runtime observability contract

`GET /api/v1/runtime/overview` is the bounded health and aggregate projection. It reports configured service state, executor identity/version/last heartbeat/current job, counts by work kind and status, and projection backlog. `GET /api/v1/runtime/work` uses an opaque keyset cursor and supports kind/status filters; it unifies ingestion, report, AgentRun, Collection synchronization, and projection events without copying them into a new table. Each item carries business/job status, stage, queue position, attempt, lease, owner, error, detail route, and permitted recovery actions.

`POST /api/v1/runtime/projections/{event_id}/retry` accepts only a failed event and requeues exactly that event in one SQLite transaction. Report, ingestion, and AgentRun recovery continue through their resource-specific endpoints so Runtime cannot bypass state cleanup or validation. Qdrant and Neo4j checks are read-only connectivity probes; their unavailability never grants them fact-store authority.

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
