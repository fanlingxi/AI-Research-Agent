# Knowledge Core 检索基准结果：2026-08-04

## 固定版本

- 数据集：`knowledge-core-10pdf-v1`
- 样本：10 PDF、30 个标注问题
- 指标：Recall@5
- SQLite 正式知识：129 个实体、198 条关系、10 个已发布 Paper
- Qdrant：1.18，collection `knowledge_chunks_v2`
- embedding：本地 `hash`，384 维
- 查询规则：已发布 Paper ID 服务端过滤；中英混合问题在 hash 模式保留拉丁技术锚点
- 检索成本：0 USD（本地 hash，无 LLM 调用）

## 结果

| 指标 | 结果 | 门槛 |
|---|---:|---:|
| Recall@5 | 1.0000（30/30） | ≥ 0.80 |
| 正式证据 grounding | 1.0000 | = 1.00 |
| 平均时延 | 63.92 ms | 记录项 |

执行命令：

```bash
.venv/bin/python -m app.knowledge.benchmark --top-k 5 \
  --output /tmp/knowledge_core_benchmark.json
```

说明：第一次请求包含客户端初始化冷启动，后续问题多数约 27–38 ms。该结果只证明固定十 PDF 演示集上的检索门槛，不代表开放域或跨语言语义检索能力；问题集或索引变化后必须重新执行并保存新版本结果。
