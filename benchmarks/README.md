# Knowledge Core 质量评测

`knowledge_core_questions.json` 固定了十篇演示 PDF 上的 30 个标注问题，以及每个问题应命中的正式论文 ID。它只评估 SQLite 已发布论文允许范围内的 Qdrant 正文切片，不接受未审核候选。

运行：

```bash
.venv/bin/python -m app.knowledge.benchmark \
  --dataset benchmarks/knowledge_core_questions.json \
  --top-k 5 \
  --output data/reports/knowledge_core_benchmark.json
```

发布门槛：Recall@5 不低于 0.80，证据页码/正文落地率为 100%。报告运行自动记录引用覆盖率、引用忠实度、模型、Prompt、token、可选成本与端到端时延；人工审核通过率只能在真实人工评审后写入对应结果文件，未采样时必须明确记为“未采样”，不能用自动 Critic 分数代替。模型、Prompt、数据集和配置发生变化时必须生成新的版本号，不能覆盖历史结果。
