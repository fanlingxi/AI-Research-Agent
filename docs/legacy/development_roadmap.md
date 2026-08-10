# Knowledge Core 三迭代路线

最后更新：2026-08-05。旧 Phase 0–5 清单已经转入历史复盘；当前主线按下面三个可验收迭代推进。

## 总览

| 迭代 | 交付目标 | 当前状态 |
|---|---|---|
| 一 | v1 可运行归档、knowledge-only 主线、幂等审核和 Schema 迁移 | 已实现并通过单元验收 |
| 二 | SQLite 持久任务、租约恢复、事务 outbox、可恢复下游 | 已实现并通过故障/并发验收 |
| 三 | 只基于正式知识的报告、证据包、Critic 和版本化评测 | 功能、真实存储、30 问题基准、浏览器 E2E、真实 LLM 合成证据 smoke 和 Docker 镜像已验收 |

## 迭代一：归档 v1，建立 Knowledge Core 基线

### 已交付

- 以 `1fb7dc6` 为基线创建 tag `v1-archive-1fb7dc6`。
- 完整快照位于 `archive/v1/`，包含源码、CLI、API、Streamlit、Docker、配置、依赖、锁文件、文档和 38 项测试。
- 根活动树移除旧 Planner、Search Agent、ResearchState、GraphRAG、Memory、Evaluation、旧 Obsidian Exporter，以及 `/api/research`、`/api/tasks/*` 和 CLI `run`。
- Ruff、Docker context、覆盖率和根 pytest 与归档隔离。
- SQLite 条件更新保证候选只从 `draft` 进入一次终态。
- 相同审核决定幂等返回，不同决定返回 `409`。
- 批量审核返回 `published_entities`、`published_relations`、`skipped_conflicts`、`blocked_relations` 和最新 ingestion。
- 建立 `schema_migrations` 顺序迁移机制。

### 自动验收

- 归档：38 项测试通过。
- 主线：不存在归档导入和旧 API 路由。
- 顺序与双线程并发审核只产生一条审核事件、一份正式事实和一个投影事件。
- 347 候选 fixture 精确得到 327 项已发布、7 个实体冲突、13 条阻塞关系。

## 迭代二：可靠任务与跨存储一致性

### 已交付

- `knowledge_jobs` 替代 FastAPI `BackgroundTasks`。
- API 只创建任务，单 worker 处理 PDF、Qdrant、LLM 抽取、报告和正式投影。
- 任务拥有状态、租约、尝试次数和最近错误；过期任务可重新领取。
- 正式事实与 projection outbox 在同一 SQLite 事务提交。
- Neo4j 使用稳定 ID `MERGE`；Obsidian 重建保留 `## 人工笔记`。
- ingestion 增加 `publishing`，并区分抽取失败和投影失败的重试路径。
- Compose 增加同镜像 `worker`，未引入 Redis/Celery。
- 健康接口返回队列、投影、Qdrant、Neo4j、Schema 和真实 LLM 状态。

### 自动验收

- 任务租约过期后 attempts 累增并被重新领取。
- Neo4j 投影失败时 SQLite 正式事实保留，失败 outbox 可重排队。
- Qdrant 在候选创建前索引；失败不会留下可审核候选。
- Obsidian 重建保留人工笔记。
- Qdrant/Neo4j 适配器具备稳定命名空间和幂等写入测试。

### 发布前容器验收

```bash
docker compose up -d qdrant neo4j
RUN_STORE_INTEGRATION=1 .venv/bin/pytest -q tests/test_store_integration.py
```

## 迭代三：基于 Knowledge Core 重建报告能力

### 已交付

- 新报告流程只消费 SQLite 已发布 paper 白名单内的 Qdrant 切片和 Neo4j 正式路径。
- 新接口：创建、列表、详情、证据包和 Markdown 下载。
- 参数：`query`、`topic_slugs`、`top_k`、`report_depth`。
- Writer 使用证据 ID；Critic 检查结构、grounding、覆盖和引用忠实度。
- 最多一次修订；报告、证据和评估持久化到 SQLite。
- mock、在线搜索、未审核候选、无正式证据都不会成为兜底。
- `benchmarks/knowledge_core_questions.json` 固定十篇 PDF、30 个标注问题和期望论文 ID。
- benchmark runner 记录 Recall@K、证据落地率和时延；报告另记录覆盖率与忠实度。

### 发布验收门槛

- Recall@5 ≥ 0.80；
- 正式知识 evidence grounding = 100%；
- 报告引用覆盖率 ≥ 0.90，引用忠实度 = 100%；
- “入库—审核—检索—生成报告”浏览器 E2E 通过；
- 每项主要结论可回溯到 PDF 页码和原文；
- 评测记录模型、Prompt、数据、配置版本、成本和端到端时延。

2026-08-04 固定 hash 检索基准结果：Recall@5 = 1.00、evidence grounding = 1.00、平均时延 63.92 ms；同日隔离浏览器 E2E 已贯通入库、3 候选审核、正式投影、报告与证据包。2026-08-05 使用完全合成证据完成 DeepSeek 真实调用，1 次生成、274/217 输入/输出 token、4,642.56 ms，四项质量指标均为 1.00；根 Docker 镜像也完成构建、归档隔离和 HTTP 健康验收。详见 `benchmarks/results/`。Provider 单价尚未配置，因此只记录 token 与时延，不宣称美元成本。

运行基准：

```bash
.venv/bin/python -m app.knowledge.benchmark --top-k 5 \
  --output data/reports/knowledge_core_benchmark.json
```

## 已完成的知识治理扩展

- 入库主题已调整为可选“知识集合”；空值进入系统“收件箱”，历史主题保留为同名集合。
- 支持全库探索、集合筛选和在稳定状态下移动入库任务；移动通过持久任务重建 Neo4j/Obsidian 投影。
- 候选支持 `defer`：待定项保存论文内来源提及，但不进入正式图谱；批量批准不再自动发布关系。
- 新增来源提及和概念词义持久层；同名词可明确发布为不同词义，链接已有词义必须由审核者操作。
- Web 词条详情显示关系说明、来源论文阅读卡与页码证据。

## 后续开发方向

发布门槛全部满足后，再按以下顺序扩展：

1. 将已验证的浏览器 fixture 流程固化为 CI 可调用的自动浏览器作业；
2. 为报告任务增加显式重试 API，并配置 provider 单价完成美元成本计量；
3. 增加 outbox 管理视图和按 ingestion 重建投影；
4. 为词义链接增加专家复审与拆分工作流；
5. 只有本地单用户边界改变后，才讨论登录、租户或分布式队列。
