# Data and Governance Model

## Trust domains

The system has four different record categories. They must not be conflated.

| Domain | Primary records | Governance |
| --- | --- | --- |
| Knowledge | Source, Document, Chunk, Entity, Relation, Claim, Evidence | Formal knowledge is reviewed before it becomes usable trusted context. |
| Memory | Project, WorkspaceTask, Decision, Artifact, ProjectKnowledgeScope, ProjectDomainPlugin | Project state has lifecycle status and revisions. |
| Runtime | ContextSnapshot, AgentRun, trace events, tool calls, outputs, checkpoints | Context and run provenance are retained; checkpoints are recovery infrastructure, not project memory. |
| Proposal | MemoryProposal | An Agent may propose a change; review/commit controls whether it affects Memory. |

## Knowledge Core

Knowledge records preserve source and evidence lineage. A claim bundle selected for runtime context includes the needed source/document/chunk/evidence references rather than an ungrounded text fragment.

Candidate retrieval may use supporting vector or graph projections, but those retrievers only return candidate identifiers. The canonical Knowledge reader rehydrates and validates the final records before they can enter a snapshot.

## Memory Core

### Project and WorkspaceTask

A Project is the ownership boundary for tasks, artifacts, decisions, enabled plugins, and knowledge scopes. A WorkspaceTask carries status, priority, goal, and routing metadata such as domain_plugin_key.

### Decision and Artifact

A Decision captures a governed project choice. An Artifact is a versioned result with project/task ownership and provenance metadata. Workspace projections verify the relation among Artifact, AgentRun, and ContextSnapshot before exposing content.

### Knowledge scopes and plugin enablement

ProjectKnowledgeScope limits the collections a task may use. ProjectDomainPlugin enables or disables a statically installed plugin for a project. A task cannot treat an arbitrary plugin implementation as data.

### MemoryProposal

A MemoryProposal has a proposal type, structured payload, rationale, lifecycle status, review note, and optional committed-record reference. It exists specifically so runtime output cannot directly rewrite trusted project memory.

## Context and Runtime records

### ContextSnapshot

A ContextSnapshot is an immutable canonical package with project/task context, selected knowledge bundles, evidence provenance, constraints, tools, selection trace, diagnostics, and canonical hash. Context Builder reads source facts in a read-only transaction and persists only the completed snapshot.

### AgentRun

An AgentRun persists the task/project references, context snapshot reference and hash, immutable plugin pin, workflow identity, execution budgets, lifecycle status, and trace/output references. Tool calls carry a sequence, declared permission, arguments/result summary, and idempotency key.

### Checkpoint

A LangGraph checkpoint is keyed by the AgentRun thread and plugin workflow namespace. It supports restart/recovery but is not a public Workspace record and does not replace an Artifact, Decision, or proposal.

## Persistence boundary

SQLite is the operational source of business truth. Qdrant, Neo4j, and Obsidian-compatible stores are supporting indexes/projections and must be recoverable from governed source facts. The current operational schema contract is v16. Migration 16 adds queue priority, lease ownership, projection ownership, and executor heartbeats without deleting business rows. Evaluation uses fresh temporary databases and never opens the operational store.
