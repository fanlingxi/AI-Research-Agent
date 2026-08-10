# Resume Project Brief

## Project title

**Evidence-Grounded Personal Knowledge Agent Workspace**
**证据约束的本地优先个人知识 Agent 工作台**

## Resume short version

- 构建本地优先的证据约束知识 Agent 工作台，贯通知识、记忆、上下文、任务与工作区闭环。
- 设计 ContextSnapshot 与引用校验机制，约束 Agent 输出并让 Artifact、MemoryProposal 全程可追溯。
- 实现 Research 与 Game Modeling 双领域插件，支持确定性公式计算、版本校验和失效保护。
- 搭建隔离离线评测平面，13 个版本化契约用例全通过，覆盖恢复、幂等、范围与插件边界。

## Expanded interview version

This project is not positioned as a generic RAG assistant. Its central design separates reviewed Knowledge from project Memory, then creates an immutable ContextSnapshot under explicit task scope before an AgentRun can execute. The runtime pins a static domain plugin, records checkpoints and tool audit data, validates output against evidence, and uses a platform-owned finalizer to create an Artifact and optional MemoryProposal. The proposal remains subject to human review, so an Agent does not silently promote generated content into trusted project state.

The Domain Plugin boundary demonstrates extensibility without fragmenting governance. Research validates citations against the selected evidence bundle and falls back to review if a bounded repair cannot resolve an invalid citation. Game Modeling reuses the same runtime/finalization path but performs deterministic Formula/Patch calculations and fails closed on a version mismatch.

Phase 6 adds an independent evaluation plane. Versioned cases build fresh local fixtures and exercise production Context, Runtime, Plugin, Validator, Finalizer, and Workspace paths with network blocking. That makes scope, recovery, provenance, and fail-closed behavior reproducible without turning the evaluator into a second runtime or fact store.

## Verified evidence

| Source | Metric | Baseline status | Experiment |
| --- | --- | --- | --- |
| Backend verification | 112 passed, 3 skipped | Not applicable | Release audit |
| Phase 6 full suite | 13 passed, 0 failed/error/skipped; 4/4 isolation true | Candidate only | phase6-20260807T101302Z-7b90a436e6 |
| Phase 6 demo subset | 3 passed, 0 failed/error/skipped; isolation passed | Candidate only | phase6-20260807T101505Z-534b3819bd |

Manifest SHA-256: 5c1a4878d037c7df0187b8f50daf2a8b96e6fcadc5f3834a307ee0061f1705c2.

There is no human-approved Phase 6 baseline. Do not use language that suggests a baseline or regression comparison.

## Interview talking points

### 1. Why it is more than RAG

RAG can retrieve text for one response, but it does not inherently explain which project state, source scope, tool permissions, and validation rules governed the answer. This design makes those boundaries explicit, so a reviewer can trace an Artifact back to the ContextSnapshot and evidence set that produced it.

### 2. Why ContextSnapshot matters

The ContextSnapshot is a canonical, hashable handoff between storage and runtime. It contains project memory, scoped claim bundles, source provenance, constraints, tools, selection trace, and token diagnostics. That lets the system reproduce a governed input rather than rebuilding an opaque prompt later.

### 3. How human governance is preserved

Knowledge review and MemoryProposal review are deliberate gates. The runtime may create a proposal, but only a reviewed commit turns it into trusted project state. This avoids the common failure mode where generated output becomes a durable fact without provenance or approval.

### 4. How the runtime stays bounded and recoverable

Every AgentRun carries an immutable plugin pin and finite step/tool budgets. LangGraph checkpoints support continuation after interruption, while tool-call idempotency data and terminal lifecycle checks prevent retries from intentionally replaying completed business transitions.

### 5. How plugin extensibility stays safe

Plugins declare static workflow, permission, Artifact, and knowledge contracts. They receive Runtime ports instead of a database handle. Research and Game Modeling therefore differ in domain logic while sharing ContextSnapshot, validation, finalization, and audit guarantees.

### 6. What the Game Modeling plugin demonstrates

Game Modeling is intentionally deterministic. It uses governed Formula and Patch evidence, performs a pure computation, and rejects patch-version mismatch before producing business output. This proves the plugin mechanism is not just a prompt template wrapper.

### 7. How evaluation avoids a benchmark-only bypass

The evaluator builds isolated fixture data but calls existing Context Builder, Runtime, Plugin, Validator, Finalizer, and Workspace paths. It verifies service-path contracts such as citation governance, no-scope behavior, recovery, plugin isolation, and artifact provenance instead of testing a separate mock implementation.

## Safe claims and limitations

Safe claim: the documented experiments demonstrate deterministic local governance contracts through isolated fixtures.

Do not claim: real-provider model quality, internet retrieval quality, production-database validation, browser validation, an accepted benchmark baseline, multi-agent orchestration, MCP, web search, or temporal memory.

The latest release-audit environment did not have Node/npm, so frontend test/build require a separate declared environment before a public UI release.
