# Knowledge Core 真实 LLM 合成证据冒烟：2026-08-05

## 目的与数据边界

- 验证真实 Provider、Writer、Critic、引用校验和运行元数据链路。
- 测试只使用代码内构造的两段合成证据和临时 SQLite，不读取 `data/`、现有知识库或用户文档。
- 研究问题、论文名、实体、关系和原文均为测试 fixture；最多允许一次修订。

## 执行命令

```bash
RUN_LIVE_LLM_INTEGRATION=1 \
  .venv/bin/pytest -q tests/test_report_live_integration.py
```

结果：`1 passed in 4.73s`。

## 运行元数据

| 指标 | 结果 |
| --- | ---: |
| Provider | `deepseek` |
| Model | `deepseek-v4-flash` |
| Prompt version | `knowledge-report-v1` |
| Schema version | 4 |
| 生成调用 | 1 |
| 输入 token | 274 |
| 输出 token | 217 |
| 端到端生成时延 | 4,642.56 ms |
| evidence grounding | 1.00 |
| citation coverage | 1.00 |
| citation fidelity | 1.00 |
| structure score | 1.00 |
| 是否修订 | 否 |
| 成本 | 未计价（Provider 单价未配置，`cost_usd = null`） |
| 人工审核通过率 | 未采样；合成 smoke 不代替人工报告评审 |

## 结论与边界

真实模型链路可以在一次调用内生成通过质量门的带引用报告，token、时延、模型、Prompt 和 Schema 版本均已持久化。该结果只证明合成证据的集成连通性，不代表真实用户语料、开放域报告质量或规模化成本。
