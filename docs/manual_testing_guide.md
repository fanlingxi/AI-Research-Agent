# Research Knowledge Core 人工验收指南

本指南用于项目交付前的人工回归，覆盖真实 PDF、真实 LLM、人工审核、SQLite 事实、Qdrant 检索、Neo4j/Obsidian 投影和报告证据。默认建议使用隔离环境，避免把测试候选写进现有知识库。

## 1. 测试前准备

准备一篇你有权使用、正文可复制、页码明确的 PDF。建议 3–10 页，至少包含一个方法、一个概念和一条可验证关系。

把 PDF 放到：

```text
data/manual_test/raw/sample.pdf
```

在 `.env` 中配置真实 LLM Provider 和 API Key。不要把真实 Key 写进测试记录或提交到 Git。

### 推荐：隔离存储

先用独立端口和 Compose project 启动测试专用 Qdrant/Neo4j：

```bash
QDRANT_PORT=6433 \
NEO4J_HTTP_PORT=7574 \
NEO4J_BOLT_PORT=7787 \
docker compose -p rkc-manual up -d qdrant neo4j
```

API 和 worker 必须使用完全相同的环境变量。分别打开两个终端，在两个终端都执行：

```bash
export KNOWLEDGE_DB_PATH=data/manual_test/knowledge.db
export KNOWLEDGE_VAULT_PATH=data/manual_test/vault
export KNOWLEDGE_QDRANT_COLLECTION=knowledge_chunks_manual_test
export QDRANT_URL=http://localhost:6433
export NEO4J_URI=bolt://localhost:7787
```

然后分别启动：

```bash
# 终端 A
.venv/bin/uvicorn app.api.main:app --reload --port 8000

# 终端 B
.venv/bin/python -m app.worker
```

第三个终端启动界面：

```bash
AI_RESEARCH_API_URL=http://localhost:8000 \
  .venv/bin/streamlit run app/ui/streamlit_app.py
```

打开 <http://localhost:8501>。API 文档位于 <http://localhost:8000/docs>。

## 2. 快速健康验收

```bash
.venv/bin/python scripts/manual_acceptance.py health --check-legacy
```

通过标准：

- `status_ok`、`schema_at_least_v4`、`live_llm_configured` 为 `true`；
- Qdrant 和 Neo4j `available` 为 `true`；
- `/api/research`、`/api/tasks/*` 均为 `404`；
- jobs/projections 中没有无法解释的失败堆积。

## 3. 浏览器主流程

### TC-01：PDF 入库

1. 进入“知识入库”。
2. 主题填写“人工验收-证据报告”。
3. PDF 填写 `data/manual_test/raw/sample.pdf`。
4. 页数上限设为实际页数或更小的可验证范围。
5. 点击“提交入库任务”。
6. 到“任务历史”刷新，观察 `queued → running → needs_review`。

通过标准：

- `document_count >= 1`、`candidate_count >= 1`；
- PDF 解析或 Qdrant 失败时不能出现可审核半成品；
- 错误信息包含具体失败原因，不回退到 mock 或在线搜索。

API 等价代码：

```bash
.venv/bin/python scripts/manual_acceptance.py ingest \
  --topic "人工验收-证据报告" \
  --pdf data/manual_test/raw/sample.pdf \
  --max-pages 10 \
  --wait
```

保存输出中的 `INGESTION_ID` 和响应里的 `topic_slug`，后续命令分别替换 `INGESTION_ID` 与 `TOPIC_SLUG`。

### TC-02：人工审核

1. 进入“审核队列”，选择刚才的任务。
2. 逐个展开实体和关系。
3. 对照原 PDF 检查名称、类型、摘要、页码和 quote。
4. 修改至少一个候选并点击“保存修改”，刷新后确认修改仍在。
5. 有合并建议时确认 canonical entity；无冲突项可逐一批准。
6. 只在确实检查过全部无冲突候选后，使用“批准所有无冲突候选”。

列出候选：

```bash
.venv/bin/python scripts/manual_acceptance.py candidates INGESTION_ID --status draft
```

修改候选：

```bash
.venv/bin/python scripts/manual_acceptance.py patch-candidate CANDIDATE_ID \
  --summary "人工核对后修订的、长度不少于十二个字符的中文说明。"
```

批准、驳回或合并：

```bash
.venv/bin/python scripts/manual_acceptance.py decide CANDIDATE_ID approve
.venv/bin/python scripts/manual_acceptance.py decide CANDIDATE_ID reject
.venv/bin/python scripts/manual_acceptance.py decide CANDIDATE_ID merge \
  --canonical-id CANONICAL_ENTITY_ID
```

批量批准：

```bash
.venv/bin/python scripts/manual_acceptance.py approve-ready INGESTION_ID
```

写操作会要求输入精确的 `YES`；CI 或一次性测试环境可显式加 `--yes`。

通过标准：

- 相同决定重放返回 `applied=false`、`replayed=true`；
- 对同一候选重放不同决定返回 `409`；
- 批量结果分别展示 `published_entities`、`published_relations`、`skipped_conflicts`、`blocked_relations`；
- 有冲突实体时，依赖它的关系保持阻塞，不会错误发布。

### TC-03：投影与正式知识

审核完成后等待：

```bash
.venv/bin/python scripts/manual_acceptance.py watch-ingestion INGESTION_ID \
  --until completed
```

进入“知识探索”，选择测试主题，检查正式节点和关系。再检查：

- Neo4j 中存在正式节点/关系，重复投影没有重复记录；
- `data/manual_test/vault` 已生成主题、论文和概念笔记；
- 在某个笔记 `## 人工笔记` 下写一句话，再触发同主题的后续投影，人工文字必须保留；
- `/health` 中失败投影数为 0。

读取正式主题：

```bash
.venv/bin/python scripts/manual_acceptance.py topic TOPIC_SLUG
```

### TC-04：正式检索

```bash
.venv/bin/python scripts/manual_acceptance.py search \
  --query "论文的核心方法解决了什么问题？" \
  --topic-slug TOPIC_SLUG \
  --top-k 5
```

通过标准：每条 evidence 都有正式 `paper_id`、`chunk_id`、有效页码和非空正文；内容能在原 PDF 对应页找到。

### TC-05：报告与证据包

浏览器进入“研究报告”，填写研究问题、主题范围、`top_k` 和深度，点击“生成报告”，刷新到 `completed`。

API 等价代码：

```bash
.venv/bin/python scripts/manual_acceptance.py report \
  --query "论文的核心方法、主要发现和局限分别是什么？" \
  --topic-slug TOPIC_SLUG \
  --top-k 5 \
  --depth standard \
  --wait \
  --output-dir /tmp/rkc-manual-results
```

通过标准：

- evidence grounding = 1.00；
- citation coverage >= 0.90；
- citation fidelity = 1.00；
- structure score >= 0.80；
- 每个主要事实后有 `[E编号]`，且编号存在于证据包；
- 人工抽查每项主要结论，能回溯到证据包页码和 PDF 原文；
- 下载的 Markdown、evidence JSON 和 metadata JSON 可重新打开；
- metadata 包含模型、Prompt、Schema、token、调用次数、时延，以及配置价格后的 `cost_usd`。

人工报告通过率必须由人实际阅读后记录，不能用自动 Critic 的 `passed` 代替。

## 4. 故障恢复测试

### TC-06：worker 重启恢复

1. 提交入库后立即停止 worker。
2. 确认 API 仍可访问，任务保持 `queued`，或运行任务等待租约过期。
3. 重启 worker。
4. 确认任务继续推进，`attempts` 合理增加，不创建重复候选。

### TC-07：Qdrant 失败不留候选

1. 停止测试 Qdrant：`docker compose -p rkc-manual stop qdrant`。
2. 提交一项新入库并让 worker 执行。
3. 确认任务 `failed`，候选列表为空。
4. 启动 Qdrant：`docker compose -p rkc-manual start qdrant`。
5. 执行：

```bash
.venv/bin/python scripts/manual_acceptance.py retry-ingestion INGESTION_ID
```

6. 确认重试后正常进入 `needs_review`。

### TC-08：Neo4j 失败不丢审核事实

1. 候选进入 `needs_review` 后停止 Neo4j。
2. 批准候选，确认 SQLite 正式事实已经存在，任务处于 `publishing` 或带明确投影错误的 `failed`。
3. 启动 Neo4j，并对失败任务执行 `retry-ingestion`。
4. 确认最终 `completed`，Neo4j 没有重复节点/关系。

## 5. 验收记录模板

```text
日期：
测试人：
Git commit / worktree：
PDF 名称与页数：
LLM Provider / Model：
Ingestion ID：
Report ID：
入库结果：通过 / 不通过
审核与幂等：通过 / 不通过
投影恢复：通过 / 不通过
检索 grounding：
报告引用覆盖 / 忠实度 / 结构：
人工报告结论核对：通过 / 不通过
输入 / 输出 token：
时延：
成本：
问题与截图路径：
最终结论：通过 / 有条件通过 / 不通过
```

测试专用容器可用下面的命令停止；该命令不会删除测试 volumes：

```bash
docker compose -p rkc-manual down
```
