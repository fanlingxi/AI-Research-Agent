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

The UI uses the existing Workspace, Memory, Context, and Agent API projections. Workspace services validate provenance for ContextSnapshot evidence and Artifact content before returning a projection.

## Workspace surfaces

- Dashboard: active projects, recent tasks, runs, artifacts, and pending proposals.
- Project workspace: project status, tasks, scoped knowledge, memory, artifacts, and enabled plugins.
- Task detail: ContextSnapshot preview/creation, selected plugin, AgentRun status, and trace.
- AgentRun: lifecycle, tool-call audit, validation, and output provenance.
- Artifact: content plus run/context relation.
- Review: explicit MemoryProposal approval or rejection flow.

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
