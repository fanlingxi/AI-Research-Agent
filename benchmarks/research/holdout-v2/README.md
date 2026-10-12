# 研究报告保留集 holdout-v2（AI 共识标签）

2026-10-12。数据集 `research-holdout-v2`，协议 `research-holdout-v2-task`。
中文问题、英文原始论文：8 篇 ACL Anthology 论文（168 页），35 题（开发 11 / 保留 24；共识 30、裁决 5）。

> **标签来源：ai-consensus，不是人工金标（human gold），未经任何人工核验。**
> 题目、期望要点与证据跨度由 Claude（claude-opus-5.5）起草；Codex CLI 与 DeepSeek Harness
> 在看不到期望答案的条件下独立作答并判断可答性；有分歧或要点覆盖不足的题再由一个全新的
> Claude 子代理盲答作为第三意见，最后由起草者书面裁决。起草者与第三意见同属 Claude 模型家族，
> 存在同源偏差。产品负责人已授权：在明确标注为非人工标签的前提下，可用本集决定候选策略能否成为默认。

题目目录见 [TASKS.md](TASKS.md)；机器数据见 [清单](manifest.json)、[开发题](tasks.dev.json)、
[保留题](tasks.holdout.json)、[来源与证据跨度](corpus.json)、[共识记录](consensus.json)、
[结构定义](schema.json)、[冻结指纹](freeze.json)。

## 为什么需要新集

此前的合成 v1、papers-v1（8 篇英文论文）、closeout-v1（DPR/FiD/HyDE/GAR/FLARE）与中文 CCL RAG
演示资料都已用于调优或演示，不再“未见”。本集论文与上述资料无重叠（按 ACL Anthology 标识和标题核对），
专门覆盖已知失败模式：逐字原文锚点、跨篇综合、比较、证据不足时正确拒答或说明缺口、越界请求，
以及过度推断（例如从“作用阶段不同”推出“不能互相替代”）。

## 划分

| 集合 | 论文族 | 论文 | 题数 |
| --- | --- | --- | --- |
| dev | verification | FActScore、RARR | 11 |
| holdout | multistep | Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions、Query Rewriting in Retrieval-Augmented Large Language Models | 8 |
| holdout | noise | Chain-of-Note、LongLLMLingua | 8 |
| holdout | longtail | When Not to Trust Language Models、Dense X Retrieval | 8 |

按论文族划分，两集不共享论文 ID 或 PDF；跨篇题只在同族内。题型：单篇事实 9、过度推断陷阱 8、证据不足/无答案 5、跨篇综合 4、越界 4、比较 3、方法局限 2。
难度为起草者主观标注。同族论文主题相关、存在互相引用（如 Rewrite-Retrieve-Read 使用 PopQA 数据集，
FActScore 引用 PopQA 论文），保留集只保证论文身份不重叠，不保证概念层面隔离。
制作过程中 Claude、Codex、DeepSeek 都读过全部题目，保留集不是对这些模型的盲测；
一旦按保留集结果调整提示或策略，后继版本须把对应论文族移入开发集。

## 标签产生过程与一致性

1. **起草**：Claude 阅读抽取文本，起草 35 题；每条必需结论绑定逐字证据跨度（PDF 页 + 原文）。
   68 条起草引文全部在声明页上逐字且唯一出现（程序检查）；裁决时新增 3 条，同样逐字。
2. **独立标注**：按论文族打包（论文全文 + 问题 + 允许来源，不含期望答案），Codex（`codex exec`，
   只读沙箱，4 次调用）与 DeepSeek（`dsh headless`，deepseek-flash，4 次调用）各自给出可答性、
   中文要点与逐字引文。
3. **比较**：程序比较可答性，并把标注者引文定位回原文（逐字 / 忽略空白 / 统一引号 / 他页 / 找不到），
   检查是否覆盖起草证据跨度；要点是否覆盖起草结论由起草者逐题判断（full / partial / none）。
4. **第三意见与裁决**：对 7 道有可答性分歧或要点覆盖不足的题（verification-q7、multistep-q3、multistep-q6、noise-q5、
   noise-q6、noise-q7、longtail-q4），由一个全新的 Claude 子代理在只看原文与问题的条件下盲答（第一次启动因会话额度中断，
   重新启动一次后完成；只采用完成的那一次）。随后起草者逐题裁决：2 题按多数改了期望类别
   （multistep-q3 改为 needs_review，multistep-q6 改为 abstain），3 题修订了必需结论或证据
   （noise-q5 把作者局限声明降为非必需、改为接受任一表格反例；noise-q6 因 Codex 与第三意见都指出 Chain-of-Note
   正文“全噪声时不如不检索”与表格无法同条件核对，改用表3的噪声趋势；noise-q7 放宽“各自单独评测”的举证方式）。
   其余 30 题两名独立标注者可答性一致、要点 full/partial，记为 consensus。没有题目因无法裁决而丢弃。
   所有裁决理由写在每题 `annotation.adjudication` 与 [consensus.json](consensus.json)。

起草稿（裁决前）的统计（35 题全部计入分母）：

| 指标 | 结果 |
| --- | --- |
| 可答性一致：Claude 起草 vs Codex | 34/35 |
| 可答性一致：Claude 起草 vs DeepSeek | 34/35 |
| 可答性一致：Codex vs DeepSeek | 33/35 |
| Codex 要点覆盖起草结论（起草者判定） | full 31，partial 4，none 0 |
| DeepSeek 要点覆盖起草结论（起草者判定） | full 33，partial 2，none 0 |
| Codex 引文定位 | 181 条：exact 66，ws 114，punct 0，other_page 0，none 1 |
| DeepSeek 引文定位 | 188 条：exact 172，ws 0，punct 6，other_page 5，none 5 |
| 第三意见（7 道争议题）可答性与起草一致 | 5/7；与 Codex 6/7；与 DeepSeek 6/7 |
| 第三意见引文定位 | 70 条：exact 70，ws 0，punct 0，other_page 0，none 0 |
| 起草引文 | 68 条起草 + 3 条裁决新增，全部在声明页逐字唯一出现 |

标注者引文中“忽略空白才匹配”多为跨行复制，不视为错误；“找不到”多为把相邻句拼接或改写。
这些统计说明的是三方 AI 之间的一致程度，不是标签正确率。

**丢弃题**：无。

## 原文与许可

全部论文来自 ACL Anthology，各论文页脚注明 2016 年后出版物采用
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)；本地保存的论文页 SHA-256 记录在
`corpus.json` 的 `landing_page_sha256`。署名见下表与 `attribution` 字段；不暗示作者认可本基准。
仓库只跟踪元数据、每页偏移和短引文，**不跟踪 PDF 与全文**；PDF、抽取文本、标注者原始输出和工作脚本
保存在本地 `data/evaluation/holdout-v2/`（不提交）。

抽取方式与 papers-v1 相同：pypdf `plain` 模式逐页抽取，页内连续空白折叠为一个空格，页间以两个 LF 连接；
不拼回断词、不修表格。证据跨度保存页码、全文码点偏移和原文。原计划加入 1–2 篇中文 CCL 论文，
但 2025 年 CCL 模板的 PDF 经 pypdf 抽取后拉丁字母和数字变成乱码，无法做逐字锚点，故放弃；
问题仍为中文。

| 论文 | 作者 | ACL Anthology | 页数 |
| --- | --- | --- | --- |
| FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation | Sewon Min; Kalpesh Krishna; Xinxi Lyu; Mike Lewis; Wen-tau Yih; Pang Koh; Mohit Iyyer; Luke Zettlemoyer; Hannaneh Hajishirzi | [2023.emnlp-main.741](https://aclanthology.org/2023.emnlp-main.741/) | 25 |
| RARR: Researching and Revising What Language Models Say, Using Language Models | Luyu Gao; Zhuyun Dai; Panupong Pasupat; Anthony Chen; Arun Tejasvi Chaganty; Yicheng Fan; Vincent Zhao; Ni Lao; Hongrae Lee; Da-Cheng Juan; Kelvin Guu | [2023.acl-long.910](https://aclanthology.org/2023.acl-long.910/) | 32 |
| Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions | Harsh Trivedi; Niranjan Balasubramanian; Tushar Khot; Ashish Sabharwal | [2023.acl-long.557](https://aclanthology.org/2023.acl-long.557/) | 24 |
| Query Rewriting in Retrieval-Augmented Large Language Models | Xinbei Ma; Yeyun Gong; Pengcheng He; Hai Zhao; Nan Duan | [2023.emnlp-main.322](https://aclanthology.org/2023.emnlp-main.322/) | 13 |
| Chain-of-Note: Enhancing Robustness in Retrieval-Augmented Language Models | Wenhao Yu; Hongming Zhang; Xiaoman Pan; Peixin Cao; Kaixin Ma; Jian Li; Hongwei Wang; Dong Yu (于东) | [2024.emnlp-main.813](https://aclanthology.org/2024.emnlp-main.813/) | 14 |
| LongLLMLingua: Accelerating and Enhancing LLMs in Long Context Scenarios via Prompt Compression | Huiqiang Jiang; Qianhui Wu; Xufang Luo; Dongsheng Li; Chin-Yew Lin; Yuqing Yang; Lili Qiu | [2024.acl-long.91](https://aclanthology.org/2024.acl-long.91/) | 20 |
| When Not to Trust Language Models: Investigating Effectiveness of Parametric and Non-Parametric Memories | Alex Mallen; Akari Asai; Victor Zhong; Rajarshi Das; Daniel Khashabi; Hannaneh Hajishirzi | [2023.acl-long.546](https://aclanthology.org/2023.acl-long.546/) | 21 |
| Dense X Retrieval: What Retrieval Granularity Should We Use? | Tong Chen; Hongwei Wang; Sihao Chen; Wenhao Yu; Kaixin Ma; Xinran Zhao; Hongming Zhang; Dong Yu (于东) | [2024.emnlp-main.845](https://aclanthology.org/2024.emnlp-main.845/) | 19 |

## 校验

从仓库根目录（只读、不联网、不调模型）：

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_holdout_v2
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_holdout_v2.py
```

本地存在 `data/evaluation/holdout-v2/papers/*.pdf` 时，校验器会重新抽取每页并逐条核对引文偏移与文本；
仓库外缺少 PDF 时只做结构与冻结校验。冻结后不修改本目录；修订另建后继版本。

## 生产链路对照（基线 vs 候选）

运行器 [holdout_v2.py](../../../app/benchmarking/holdout_v2.py) 复用 resume closeout 的准备/执行流程：
原文段落经审核/发布入口写入隔离评测 SQLite，Qdrant 建索引，每题一个冻结 ContextSnapshot，
两个工作流（默认 `research_v4`、候选 `research_v7`）分别经 Worker 与最终提交运行。
进入模型的只有题目 id、问题与允许范围；期望结论、禁止结论和标注记录不会进入模型输入。

```powershell
# 0) 计划（不联网、不调模型）
.\.venv\Scripts\python.exe -m app.benchmarking.holdout_v2 --split holdout
# 1) 离线演练：PDF/文本指纹 + 段落导入临时库（无向量、无模型）
.\.venv\Scripts\python.exe -m app.benchmarking.holdout_v2 --verify-corpus --split holdout
# 2) 准备：需本地 Qwen 嵌入与 Qdrant（scripts/vector-service.ps1），不调用生成模型
powershell -ExecutionPolicy Bypass -File scripts/vector-service.ps1 start
.\.venv\Scripts\python.exe -m app.benchmarking.holdout_v2 --prepare --split holdout
# 3) 执行：付费生成调用，由集成者在确认预算后运行
$env:RUN_LIVE_LLM_INTEGRATION = "1"; $env:RUN_STORE_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m app.benchmarking.holdout_v2 --execute data\evaluation\holdout-v2\runs\<prepared-folder>
```

保留集 24 题 × 2 工作流 = 48 次运行，上限 216 次模型调用（每次运行 4 次 + 每题 1 次选材）。
执行器沿用 closeout 的保护：生产代码指纹或冻结文件变化即拒绝执行、已执行的归档不可覆盖、
正式知识库指纹前后核对。产物语义评分需按本集期望结论/禁止结论逐题判定，报告中必须注明
标签来源为 ai-consensus。
