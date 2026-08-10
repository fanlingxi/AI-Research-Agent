# Agent Runtime

## Purpose

Agent Runtime executes one bounded workflow for a persisted AgentRun. It is responsible for lifecycle transitions, checkpoint-aware recovery, tool audit, validation, and finalization. It is not a general autonomous loop and it is not allowed to mutate Knowledge or Memory directly.

## Run contract

Before execution, an AgentRun records:

- Project, WorkspaceTask, ContextSnapshot ID, and context SHA-256;
- immutable plugin key, version, contract version, and workflow key;
- maximum step and tool-call budgets;
- lifecycle state, trace event references, outputs, Artifact reference, and optional MemoryProposal reference.

The runtime resolves the saved plugin pin through the static registry. A current default cannot silently switch the workflow of an already queued run.

## Execution model

~~~text
queued
  ↓
load ContextSnapshot
  ↓
bounded plugin workflow in LangGraph
  ↓
audited tool calls
  ↓
validation / bounded repair where supported
  ↓
Platform Finalizer
  ↓
completed | needs_review | failed | cancelled | stale_context
~~~

Each plugin workflow has a reviewed minimum step/tool budget and checkpoint namespace. LangGraph checkpoints are opened only for the selected workflow. If a worker resumes an interrupted run, it restores the business lifecycle before scheduling the next durable graph node rather than deliberately replaying completed work.

## Tool and output governance

Tool calls are executed through a registry with declared permissions and are written with sequence and idempotency information. The Platform Finalizer validates the plugin pin and owns atomic output, Artifact, and optional MemoryProposal persistence.

A plugin supplies a DomainFinalizationCommand; it does not receive database, repository, or checkpoint handles. This keeps output governance in the platform layer.

## Research behavior

The Research workflow validates citations against the ContextSnapshot evidence bundle. It can apply one controlled citation repair. If citations remain invalid, the run enters review without producing the normal Artifact or MemoryProposal. Its normal research workflow requires a configured live LLM; scripted fixtures exist only for deterministic evaluation.

## Game Modeling behavior

The Game Modeling workflow uses a deterministic compute path over governed Formula and Patch facts. Patch-version mismatch fails closed before business output. The plugin can produce a governed Artifact and proposal only through the same platform finalizer.

## Operational boundaries

- The runtime has no unrestricted database access path for domain workflows.
- Checkpoints are recovery state, not a public fact store.
- No multi-agent scheduler, dynamic plugin loader, MCP server, or web-search loop is part of the implemented runtime.
- Phase 6 exercises existing Runtime paths with temporary settings and network blocking; it does not execute production AgentRuns.
