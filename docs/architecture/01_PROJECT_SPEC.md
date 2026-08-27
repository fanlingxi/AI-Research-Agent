# Evidence-Grounded Personal Knowledge Agent Workspace

中文名称：**证据约束的本地优先个人知识 Agent 工作台**

## Product position

This is a local-first, project-driven workspace for evidence-grounded knowledge work. It joins reviewed Knowledge, governed project Memory, immutable runtime context, bounded Agent execution, and auditable outputs.

It is not a generic RAG chat surface. A usable result must carry scope, evidence provenance, a selected domain workflow, validation state, and a traceable path to an Artifact or reviewable MemoryProposal.

## Implemented system

~~~mermaid
flowchart LR
    U["User / Project"] --> W["Project Workspace"]
    W --> T["Task + Knowledge Scope"]
    T --> C["Context Builder"]
    K["Knowledge Core"] --> C
    M["Memory Core"] --> C
    C --> S["Immutable ContextSnapshot"]
    S --> P["Static Domain Plugin Pin"]
    P --> R["Bounded LangGraph Runtime"]
    R --> V["Validator + Platform Finalizer"]
    V --> A["Artifact"]
    V --> MP["MemoryProposal"]
    MP --> M
~~~

The evaluation plane is intentionally separate:

~~~mermaid
flowchart LR
    VC["Versioned Evaluation Cases"] --> F["Fresh Isolated Fixture"]
    F --> PS["Existing Service Paths"]
    PS --> E["Deterministic Evaluator"]
    E --> CR["Candidate Report"]
~~~

It creates temporary SQLite/checkpoint/vault paths, blocks network access, and does not open the operational database. It is neither a new runtime nor a new knowledge/memory store.

## Core responsibilities

| Layer | Responsibility | Boundary |
| --- | --- | --- |
| Knowledge Core | Reviewed sources, documents, chunks, entities, relations, claims, and evidence | Generated output cannot silently become trusted knowledge. |
| Memory Core | Projects, tasks, decisions, artifacts, scopes, plugin enablement, and proposals | Agent-initiated changes remain reviewable proposals. |
| Context Builder | Scope-filtered, provenance-complete, token-budgeted input package | Candidate retrievers only nominate IDs; validated records form the snapshot. |
| Agent Runtime | Bounded workflow execution, checkpoints, tool audit, recovery, validation | Static plugin pin and budget constrain each run. |
| Domain Plugin | Domain workflow and governed output contract | No direct database/checkpoint ownership. |
| Workspace | Read-oriented projections over existing facts | No second product fact store. |
| Evaluation Plane | Offline deterministic contracts and demo evidence | No operational database, provider, internet, or human-approved baseline by default. |

## Product path

1. A project task carries a Knowledge scope and a selected first-party plugin.
2. Context Builder reads project memory and approved knowledge under that scope.
3. It canonicalizes and persists a ContextSnapshot.
4. Agent Runtime resolves the exact plugin pin and executes a finite LangGraph workflow with a dedicated checkpoint namespace.
5. Validators and the Platform Finalizer either create governed output/Artifact/proposal records or fail closed.
6. Workspace projections surface provenance without directly reading checkpoints or creating alternate truth.

## Human governance

Knowledge promotion, memory proposal commitment, and Artifact lifecycle are distinct operations. The system records revisions, status transitions, cited evidence, and AgentRun provenance so a reviewer can identify what happened and why.

## Installed domain scope

Two first-party plugins are implemented:

- **Research** — evidence-cited research workflow with bounded citation repair and review fallback.
- **Game Modeling** — deterministic formula calculation over scoped Formula/Patch inputs with patch-version fail-closed behavior.

Creative plugins, third-party dynamic plugins, multi-agent orchestration, MCP, web search, and temporal-memory features are not implemented product claims.

## Storage and compatibility

SQLite is the operational fact store. Qdrant, Neo4j, and Obsidian-compatible projections support retrieval or projection and do not replace business truth. Vector and graph stores nominate candidates only; report and Agent evidence is rehydrated and validated from SQLite before use. The current operational schema contract is v18 and is applied through forward-only, additive migrations. Migration 18 adds many-to-many ingestion/document membership without deleting or rewriting existing document rows. Historical evaluation outputs remain immutable records of the schema version on which they were generated.

## Public release status

The implementation has passed backend and deterministic-evaluation verification, but a Phase 6 result is currently a **candidate** until explicit human baseline approval. Public release readiness also requires a reviewed clean worktree, secret-safe build artifacts, and current frontend validation in a Node environment.
