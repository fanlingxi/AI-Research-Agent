# ContextSnapshot Contract

## Purpose

Context Builder transforms a Project + WorkspaceTask + governed Knowledge scope into a runtime-safe ContextSnapshot. The snapshot is the contract between persisted facts and an AgentRun; it is not an arbitrary prompt assembly step.

## Inputs

A build request identifies:

- a project and one of its workspace tasks;
- the task’s eligible lifecycle state;
- the project’s declared knowledge scopes;
- a maximum token budget.

The builder reads Project, task, decisions, artifacts, scopes, and trusted Knowledge from one SQLite read-only transaction. A caller cannot substitute a task belonging to another project.

## Build pipeline

~~~text
Project / Task eligibility
        ↓
Declared Knowledge scope
        ↓
Candidate retrieval (optional vector / graph identifiers)
        ↓
Canonical record rehydration and evidence validation
        ↓
Ranking and deterministic budget selection
        ↓
Canonical ContextPackage + SHA-256
        ↓
Immutable ContextSnapshot persistence
~~~

Vector and graph retrieval are candidate aids only. They cannot inject raw external content into the runtime payload or evade scope filtering.

## Package contents

The canonical package includes task and project context, memory context, scoped knowledge claim bundles, source/document/chunk/evidence provenance, constraints, allowed tool context, selection trace, diagnostics, and token usage.

The immutable package is normalized and hashed. The hash stored on AgentRun binds the execution to the context that was actually selected.

## Fail-closed behavior

- A terminal project/task cannot build new runtime context.
- A task/project mismatch is rejected.
- A too-small token budget is rejected rather than truncating mandatory runtime data unpredictably.
- A task with no declared scope yields knowledge_coverage = no_scope, zero selected bundles, and no business outputs.
- Incomplete source/document/chunk/evidence lineage is not exposed as a valid evidence bundle.

## Read/write boundary

Context construction is read-only over existing business facts. Persisting a completed immutable snapshot is Context Builder’s sole write. The Workspace may preview a non-persisted package or request a persisted snapshot; it does not manufacture a second context store.

## Evaluation evidence

Phase 6 covers approved-evidence retrieval, scope exclusion, in-scope-only context, repeatable normalized payload, and no-scope fail-closed behavior through isolated fixture databases. Those checks are deterministic contract evidence, not live-retrieval quality claims.
