# 数据和上下文

## 数据归属

| 类别 | 主要记录 | 使用约束 |
| --- | --- | --- |
| 正式知识 | Source、Document、Chunk、Entity、Relation、Claim、Evidence | 通过审核并满足项目范围与版本约束后才能成为证据 |
| 项目记忆 | Project、WorkspaceTask、Decision、Artifact、ProjectKnowledgeScope | 保留归属、生命周期和修订号 |
| 运行记录 | ContextSnapshot、AgentRun、工具调用、输出与检查点 | 快照关联生成依据，检查点用于恢复 |
| 记忆建议 | MemoryProposal | 审核提交后才影响正式项目记忆 |

SQLite 保存正式数据。Qdrant、Neo4j 和 Obsidian Vault 分别承担检索或阅读投影，不替代审核状态、来源版本和业务记录。工作台按 Artifact、AgentRun 和 ContextSnapshot 的关系校验来源后展示内容。

## 候选检索到可信证据

检索索引只返回候选 ID 和分数。服务回读 SQLite，检查允许范围、审核状态、来源版本、正文及定位信息，丢弃不满足约束的候选。索引中的正文不能直接作为报告证据。

`quick_report` 读取 source/chunk 证据；`project_run` 读取完整 claim bundle。共享排序不等于可以用不完整切片替代主张与原文之间的证据关系。没有有效范围或可用证据时，不生成正常业务产物。

## 快照构建

1. 核对 Project、WorkspaceTask 的归属、状态和可用知识范围。
2. 准备候选、按需执行外部检索或模型辅助选材。
3. 回读事实并再次核查状态、版本、证据关系和预算。
4. 规范化上下文内容，生成 SHA-256，原子保存不可变 ContextSnapshot。

外部模型和检索调用不放在新增长数据库事务中。准备后的事实可能已变化，因此冻结前需要重新验证。快照中保留任务与项目上下文、记忆、证据及来源、约束、允许工具、选材记录和预算信息；AgentRun 绑定快照 ID 和哈希。

项目或任务不匹配、必需信息超出预算、证据关系不完整等情况应明确拒绝或记录覆盖不足，不能靠随意截断满足约束。新证据必须在冻结前完成审核；运行后补证通过新快照和新运行处理。

## 迁移与恢复

当前应用迁移到 v20，文件位于 [app/persistence/migrations](../../app/persistence/migrations/)。v18 增加导入与文档的多对多关联，v19 增加运行反馈，v20 记录研究生成尝试。结构迁移按版本增量执行；需要外部投影的数据回填有独立入口。

旧来源定位、快照和失败证据保留。历史基准的版本元数据只描述生成该记录时的条件，不随应用版本升级改写。

业务库与 LangGraph checkpoint 库分别备份。每个 SQLite 备份的一致性不等于跨库原子快照；需要一致恢复点时，在停止所有写入的同一窗口备份两库。操作步骤见[备份与恢复](../GETTING_STARTED.md#sqlite-备份与恢复)。

## 验证入口

[上下文一致性测试](../../tests/test_context_consistency.py)、[查询一致性测试](../../tests/test_query_consistency.py)、[报告提交测试](../../tests/test_report_commit.py)覆盖范围、版本变化与提交边界；[结构迁移测试](../../tests/test_structural_migrations.py)和[备份测试](../../tests/test_backup.py)覆盖持久化行为。确定性通过不代表真实检索召回率或报告语义准确率。
