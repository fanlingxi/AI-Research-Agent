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

- Dashboard: one explicit dual-mode research command launcher, active projects, recent tasks, runs, artifacts, and pending proposals.
- Project workspace: project status, tasks, scoped knowledge, memory, artifacts, enabled plugins, and an explicit Agent Task selector that never silently chooses the first open Task.
- Task detail: ContextSnapshot preview/creation, selected plugin, AgentRun status, and trace.
- AgentRun: lifecycle, tool-call audit, validation, output provenance, stale-Snapshot guidance, and the two safe `needs_review` actions (fresh-Snapshot rerun or close).
- Artifact: content plus run/context relation.
- Review: explicit MemoryProposal approval or rejection flow.
- Reports: submit-and-execute by default, targeted start for existing queued reports, adaptive polling, visible generation stage, evidence pack, download, and in-place failed retry.
- Knowledge: completed system-inbox ingestions can be moved into an explicit formal Collection before Project use.
- Runtime: API/Worker/LLM/Qdrant/Neo4j/Vault health, executor heartbeat, unified work pagination, AgentRun/report/ingestion/Collection/projection state, queue position, lease ownership, targeted recovery, and deep links.

## Browser dispatch boundary

A browser action persists resource creation and queue intent; it never makes FastAPI own a long-running model, PDF, AgentRun, Collection synchronization, command orchestration, or projection operation. The independent unified Worker is the sole executor. Explicit start and retry actions raise the selected resource's priority without claiming or executing another queued resource in the request process.

Every claimed job records an executor identity, attempt, priority, and lease. The Worker renews all work at one third of the lease and uses `job_id + attempt + lease_owner` as its fencing token. Ingestion and report stage writes, AgentRun lifecycle writes and Platform Finalizer, and projection completion/failure validate that token in the same SQLite transaction as the business mutation. The Worker periodically recovers only expired leases; an API restart does not affect queued or running ownership. Default concurrency remains one to bound SQLite contention and real-LLM cost.

## Unified research command contract

`POST /api/v1/research-commands` requires an `Idempotency-Key`. The server stores only its hash together with a canonical request hash. Replaying the same key and payload returns the original command; reusing the key for a different payload returns a conflict. The persistent lifecycle is `accepted → preparing → target_created → queued → completed | failed`, and partially created Task, ContextSnapshot, report, or AgentRun identifiers remain attached to a failed command for targeted recovery.

`quick_report` is the default mode and requires an instruction plus an optional searchable Collection scope. Its target is an independent evidence report. `project_run` must explicitly name a Project and either an existing Task or a new Task definition. It builds or reuses that Task, an immutable ContextSnapshot, and an AgentRun; it never silently creates a Project. The Dashboard stores the pending command ID and idempotency key locally until a target route exists, polls `GET /api/v1/research-commands/{id}`, and offers `POST .../{id}/retry` from the original surface after an orchestration or target failure.

The command row is an orchestration record, not a replacement for report, Task, ContextSnapshot, AgentRun, or durable job truth. Deterministic target IDs plus SQLite transactions make recovery repeatable, and the Worker synchronizes target terminal state back to the command. The existing Task-level Agent panel remains the advanced manual flow.

## Operations console and next optimization priorities

Streamlit no longer exposes daily ingestion, search, candidate-review, or report creation in its navigation. Its supported surfaces are unified task diagnostics, failed projection retry, deterministic full/per-Collection Qdrant/Neo4j/Vault rebuild, and raw read-only health state. React remains the only daily product entry.

Further UI work is limited to terminology normalization and dense-layout acceptance across Project, Task, AgentRun, Artifact, Review, and Runtime at the supported 1440×900 and 1920×1080 PC viewports. Mobile navigation and responsive adaptation are outside the current product scope.

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

The current docker-compose.yml starts the React/Nginx web service, API, worker, Qdrant, Neo4j, and the Streamlit operations console. React is the only daily product entry. Browser-facing URLs are configuration rather than hard-coded component constants. FastAPI is a small composition root over knowledge, reports, projects, agent, and runtime `APIRouter` modules; it remains one modular-monolith process and does not own a background dispatcher.
