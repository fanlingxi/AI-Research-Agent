# Resume Project Brief

## Project title

**Evidence-Grounded Personal Knowledge Agent Workspace**
**证据约束的本地优先个人知识 Agent 工作台**

## Resume short version

- 构建本地优先的证据约束知识 Agent 工作台，贯通知识、记忆、上下文、任务与工作区闭环。
- 设计 ContextSnapshot 与引用校验机制，约束 Agent 输出并让 Artifact、MemoryProposal 全程可追溯。
- 实现 Research 与 Game Modeling 双领域插件，支持确定性公式计算、版本校验和失效保护。
- 将 React 工作流收敛为幂等双模式研究指令、显式 Task 选择、可见运行阶段与原位恢复，避免重复资源和错误 Task 执行。
- 以统一 Worker、租约续期与 attempt/owner fencing 执行所有长任务，并在 Snapshot revision 失效或引用验证失败时安全停止。
- 搭建隔离离线评测平面，13 个版本化契约用例全通过，覆盖恢复、幂等、范围与插件边界。

## Expanded interview version

This project is not positioned as a generic RAG assistant. Its central design separates reviewed Knowledge from project Memory, then creates an immutable ContextSnapshot under explicit task scope before an AgentRun can execute. The runtime pins a static domain plugin, records checkpoints and tool audit data, validates output against evidence, and uses a platform-owned finalizer to create an Artifact and optional MemoryProposal. The proposal remains subject to human review, so an Agent does not silently promote generated content into trusted project state.

The Domain Plugin boundary demonstrates extensibility without fragmenting governance. Research validates citations against the selected evidence bundle and falls back to review if a bounded repair cannot resolve an invalid citation. Game Modeling reuses the same runtime/finalization path but performs deterministic Formula/Patch calculations and fails closed on a version mismatch.

Phase 6 adds an independent evaluation plane. Versioned cases build fresh local fixtures and exercise production Context, Runtime, Plugin, Validator, Finalizer, and Workspace paths with network blocking. That makes scope, recovery, provenance, and fail-closed behavior reproducible without turning the evaluator into a second runtime or fact store.

## Verified evidence

| Source | Metric | Baseline status | Experiment |
| --- | --- | --- | --- |
| Backend verification | 181 passed, 3 skipped | Not applicable | 2026-08-27 staged workbench regression |
| React verification | 38 passed; production build passed | Not applicable | 2026-08-27 staged workbench regression |
| PC browser acceptance | 1440×900 and 1920×1080; no horizontal overflow; SPA deep-link refresh passed | Not applicable | 2026-08-27 local isolated services |
| 15-PDF acceptance | 15 Sources, 15 Documents, 707 Chunks, 687 pages; migration hash unchanged | Not applicable | 2026-08-27 isolated database copy |
| Real-provider semantic acceptance | 8 evidence spans across 5 papers; all report quality scores 1.0 | Single controlled run, not a production benchmark | 2026-08-27 unified Worker and prompt v3 |
| Browser-component acceptance | Explicit Task selection, `needs_review` safe actions, inbox-to-Collection move, bounded long lists, command idempotency and Runtime diagnostics | Not applicable | 2026-08-27 mock/scripted fixtures |
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

Every AgentRun carries an immutable plugin pin and finite step/tool budgets. One independent Worker owns all long tasks and renews leases; `job_id + attempt + lease_owner` fencing blocks stale attempts from committing. Before opening a checkpoint, the runtime compares Snapshot Project/Task revisions and stops as `stale_context` when governed inputs changed. LangGraph checkpoints then support continuation after interruption without intentionally replaying completed business transitions.

### 5. How plugin extensibility stays safe

Plugins declare static workflow, permission, Artifact, and knowledge contracts. They receive Runtime ports instead of a database handle. Research and Game Modeling therefore differ in domain logic while sharing ContextSnapshot, validation, finalization, and audit guarantees.

### 6. What the Game Modeling plugin demonstrates

Game Modeling is intentionally deterministic. It uses governed Formula and Patch evidence, performs a pure computation, and rejects patch-version mismatch before producing business output. This proves the plugin mechanism is not just a prompt template wrapper.

### 7. How evaluation avoids a benchmark-only bypass

The evaluator builds isolated fixture data but calls existing Context Builder, Runtime, Plugin, Validator, Finalizer, and Workspace paths. It verifies service-path contracts such as citation governance, no-scope behavior, recovery, plugin isolation, and artifact provenance instead of testing a separate mock implementation.

## Safe claims and limitations

Safe claim: the documented experiments demonstrate deterministic local governance contracts and the targeted React report workflow through isolated fixtures.

Do not claim: real-provider model quality, internet retrieval quality, production-database validation, an accepted benchmark baseline, multi-agent orchestration, MCP, web search, or temporal memory.

The browser acceptance used a deterministic local model and temporary storage. It validates interaction and task isolation, not the semantic quality or cost of a real-provider report.
