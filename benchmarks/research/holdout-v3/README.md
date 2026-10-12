# holdout-v3：AI 共识保留集

已载入 AI 共识/仲裁记录；非人工核验。

标签来源为 AI 共识（ai-consensus），非人工核验。出题者 Codex（GPT），独立标注者 DeepSeek，仲裁者 Claude；drafted_by 记为 Codex。Claude 的复核为非盲审；逐题标注、一致性与仲裁理由见 consensus.json。

共 31 题，dev 10 题，holdout 21 题；删除 0 题。rerank 为开发集；expansion、retriever、need 为保留集。

论文均来自 ACL Anthology，许可证 CC BY 4.0；标题、作者、署名、许可链接、PDF 与落地页 SHA-256 见 corpus.json。仅跟踪元数据和短引文，不含全文。本地 papers/<ACL编号>.pdf 是规范输入，texts/ 不写入仓库。fetched_at 使用本地落地页文件修改时间，不宣称是经验证的下载时间。

抽取使用 pypdf plain：逐页合并空白为一个空格，页间用两个换行连接；不修复断词、不翻译、不重建表格。span 偏移和逐字引文均基于此规则。

v3 的拒答题可引用说明缺失信息的证据；越界题可将其他家族论文列为禁止来源，但允许来源仍严格限制在本家族内。

在仓库根目录运行 `python -m scripts.build_holdout_v3`。默认读取 `D:/AI-Research-Agent/data/evaluation/holdout-v3`；可用 `--data`、`--output` 指定路径。缺少共识记录时生成待仲裁占位，不得将 status=consensus 或结构校验通过解释为语义核验通过。

`python -m scripts.validate_holdout_v2 --dataset benchmarks/research/holdout-v3`

`python -m app.benchmarking.holdout_v2 --dataset v3 --arms-preset d030 --plan`

运行器仅向模型提供题号、问题和允许来源；评分器由运行 config.json 选择数据集。D030 比较 A-default（research）和 C-candidate（research_v8）。保留集用于调优后不再是保留集；这些 AI 标签不能替代人工核验。
