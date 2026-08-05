# Development Journal

本日志持续记录项目从需求、实现、验证到产品使用反馈的完整链路，作为后续技术复盘、项目介绍和简历材料的事实依据。架构的稳定结论写入 `architecture.md`，阶段规划写入 `development_roadmap.md`，这里保留每次关键决策、现场数据、验证证据和待改进项。

## 记录模板

- 日期与阶段
- 用户场景与问题
- 相关模块和调用链
- 现场数据与验证方法
- 结论与产品含义
- 已知风险和后续动作
- 可复用的项目成果表述

## 2026-08-04：v1 归档与 Knowledge Core 主线收敛

### 决策

不再兼容 v1。以纯 v1 提交 `1fb7dc6` 创建 `v1-archive-1fb7dc6` tag 和 `archive/v1/` 可运行快照；历史源码、测试、配置、依赖和用户数据不删除。根项目只保留 Research Knowledge Core。

### 实现

- 活动树移出 Planner、Search Agent、13 节点 ResearchState、旧 GraphRAG、Memory、Evaluation、旧报告 API/UI/CLI 和旧 Obsidian Exporter。
- API 只创建 SQLite 持久任务；同镜像单 worker 使用租约执行入库、报告和 projection outbox。
- 审核采用 `BEGIN IMMEDIATE` 与 `UPDATE ... WHERE status='draft'`，正式事实、review event、outbox 同事务提交。
- ingestion 增加 `publishing`；Neo4j/Obsidian 故障可重放，下游不是业务事实源。
- 报告只检索已发布 paper 白名单中的 Qdrant 切片和正式 Neo4j 路径，最多修订一次。
- 新增 30 问题/10 PDF 评测集和 Recall@5 runner。

### 验证

- `archive/v1` 原 38 项测试独立通过。
- 根 knowledge-only 测试覆盖并发幂等、347 候选统计、租约恢复、投影故障、人工笔记保留、报告质量与 API。
- 真实 Qdrant 1.18 与 Neo4j 5 幂等集成测试各 1 项通过。
- 固定 10 PDF / 30 问题 hash 检索基准达到 Recall@5 1.00、grounding 1.00、平均 63.92 ms。
- 隔离浏览器 E2E 贯通入库、3 候选审核、正式投影、报告生成与 p.1 原文证据包，四项报告质量指标均为 100%。
- 真实 LLM 调用改为只发送代码内合成证据，避免外传本地知识库；实际模型结果记录于 2026-08-05 条目。

### 简历口径

可以陈述完成了 v1 冻结归档、主线边界收敛、SQLite 事务审核与 outbox、一致性恢复和正式证据报告；效果指标只引用实际执行并保存结果的基准。

## 2026-08-05：真实 LLM 与 Docker 发布链路收尾

### 数据与权限边界

真实 LLM smoke 不读取 `data/` 或现有知识库，只在 pytest 临时目录创建 SQLite，并向 Provider 发送两段代码内合成证据。测试最多修订一次，避免不可控调用。Docker 验收只创建本地镜像和临时容器，不修改 Docker Desktop 的全局 mirror 配置。

### 验证

- `RUN_LIVE_LLM_INTEGRATION=1 .venv/bin/pytest -q tests/test_report_live_integration.py`：1 项通过。
- DeepSeek `deepseek-v4-flash`：1 次调用，274 输入 token、217 输出 token、4,642.56 ms。
- grounding、引用覆盖、引用忠实度和结构评分均为 1.00，未触发修订。
- Provider 单价未配置，`cost_usd` 如实为 `null`。
- `docker build --pull=false -t research-knowledge-core:verification .`：成功。
- 容器内确认没有 `/app/archive`、没有 `/api/research`，并真实启动后得到 `/health` 200。

### 结论

真实模型适配、质量门、运行元数据和 Docker 发布边界均已形成可复现证据。合成 smoke 不能替代真实论文报告的人工质量评估；美元成本必须在配置明确单价后再记录。

## 2026-08-04：Knowledge Base v2 批量审核行为与幂等性核查

### 用户场景

用户在 Streamlit 审核队列点击“批准所有无冲突候选”后，任务仍为 `needs_review`，页面显示：

- 候选总数：347
- 待审核：20
- 实体：7
- 关系：13

需要解释剩余内容的含义，并判断多次重复点击是否会污染 SQLite 审计数据、Neo4j 正式图谱或 Obsidian 投影。

### 统计口径与调用链

页面先读取任务的全部候选；“候选总数”包含 `draft`、`published`、`merged` 和 `rejected` 等所有状态。其余三个数字只基于 `status == "draft"` 的候选统计，因此本次 7 个实体与 13 条关系共同构成 20 个待审核项。

批量按钮调用：

```text
Streamlit 审核队列
  -> POST /api/knowledge/ingestions/{ingestion_id}/approve-ready
  -> KnowledgeIngestionService.approve_ready
     1. 批准没有 merge_suggestions 的实体
     2. 批准两个端点均已有 canonical_id 的关系
     3. 仍有 draft 时保持 needs_review，否则转为 completed
```

### 现场数据验证

任务：`ing-052c0d73ec49497b8c8ef9af05f0ced5`（Agent 智能体研究）。对 `data/knowledge/knowledge.db` 进行只读查询，结果为：

| 状态 | 类型 | 数量 |
| --- | --- | ---: |
| `published` | entity | 129 |
| `published` | relation | 198 |
| `draft` | entity | 7 |
| `draft` | relation | 13 |

总计 347；已发布 327；待审核 20。`review_events` 共有 327 条，且 327 个已发布候选各对应一次审核事件，没有发现重复审核事件。

7 个待审核实体均有 1 个 `exact` 合并建议：思维链、ReAct、HotPotQA、Chain-of-Thought (CoT) prompting、Language Agent、Large Language Model (LLM)、Large Language Model。13 条待审核关系全部至少有一个端点指向这 7 个尚未取得 `canonical_id` 的实体，因此关系会继续留在队列中。

### 结论

在当前页面状态下，按顺序多次点击批量按钮不会重复创建实体、关系或审核事件：

- 7 个实体因为已有合并建议，会被批处理主动跳过，等待人工选择“确认合并”或在允许时明确批准为新实体。
- 13 条关系因为端点尚未发布，发布校验失败后保持 `draft`，不会写入正式关系表和审核事件表。
- 不会重新调用 LLM、重新解析 PDF、重新写入 Qdrant，也不会重跑知识抽取。
- 每次请求仍会产生少量 SQLite 查询、关系依赖校验和任务状态刷新；服务重启后的首次正式图谱预检还可能访问 Neo4j。数据风险很低，但重复点击没有推进作用，且当前成功提示容易让用户误以为所有内容都已处理。

建议操作顺序：先逐一审查并合并 7 个冲突实体，再点击一次“批准所有无冲突候选”，系统即可继续发布其余端点已满足的关系。若最终没有 `draft`，任务状态会变为 `completed`。

### 幂等性边界与后续改进

当前结论适用于 Streamlit 页面上的顺序重复点击。接口尚未实现严格的并发幂等保护：两个并发 `approve-ready` 请求可能同时读取同一批 `draft`；单候选 decision 接口也没有先拒绝已终态候选。页面的同步请求与立即 rerun 使该风险较低，但直接 API 重放或并发调用仍是需要补强的工程边界。

后续建议：

1. decision 写入使用条件更新或事务锁，只允许 `draft` 状态进入终态。
2. 为已发布关系建立候选来源唯一约束或幂等键。
3. 批量接口返回 `published`、`skipped_conflict`、`blocked_relation` 的数量，页面据此显示准确反馈。
4. 当只剩冲突实体和被阻塞关系时，隐藏或禁用批量按钮，并提示下一步先处理实体合并。
5. 增加顺序重放与并发重放测试，覆盖 SQLite、Neo4j 投影和审核事件三层一致性。

### 可复用的项目成果表述

- 设计并实现 PDF 到结构化知识的人工审核发布链路，以 SQLite 保存候选与审计事实，以 Neo4j、Qdrant 和 Obsidian 分别承担图投影、向量检索和可读知识库职责。
- 针对 347 个真实候选完成批量审核行为核查，通过候选状态、关系端点和审核事件三方对账定位 7 个实体冲突及 13 条依赖阻塞关系，并识别并发幂等边界。
- 将批处理结果从单一成功语义拆解为发布、冲突跳过和依赖阻塞，为后续可观测性、交互反馈与并发安全改造形成明确方案。
