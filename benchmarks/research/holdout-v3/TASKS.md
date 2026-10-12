# holdout-v3 题目

已载入 AI 共识/仲裁记录；非人工核验。

## rerank-q1（dev / single_fact / easy）

RankGPT 在常规基准实验中对哪一个检索器返回的多少篇候选段落重排？滑动窗口的大小、步长和移动方向分别是什么？

允许来源：rankgpt。

预期处理：answer。

- 候选是 BM25 返回的前 100 篇段落。
- 窗口大小为 20、步长为 10，按从列表末端到前端的顺序重排。

## rerank-q2（dev / single_fact / medium）

PRP-Allpair 如何处理同一文档对在交换输入顺序后产生的冲突，以及汇总分数的平局？请说明每种情况下的计分或回退规则。

允许来源：prp。

预期处理：answer。

- 每对文档交换输入顺序询问两次；判断一致时形成局部胜负，否则视为平局。
- 一致获胜的文档得 1 分；冲突或无关输出时双方各得 0.5 分。
- 汇总分数相同时回退到初始排序。

## rerank-q3（dev / comparison / hard）

RankGPT 的滑动窗口与 PRP-Sliding-K 都从后向前处理候选，它们一次模型判断的对象、输出以及形成前 K 名结果的方式有何不同？

允许来源：rankgpt, prp。

预期处理：answer。

- RankGPT 对一组带标识符的段落直接生成按相关性降序排列的标识符序列。
- RankGPT 通过有重叠的多段落窗口从后向前移动；实验窗口为 20、步长为 10。
- PRP 每次比较文档对并按结果交换，单遍从底部以步长 1 扫描，PRP-Sliding-K 执行 K 遍以关注 Top-K 指标。

## rerank-q4（dev / synthesis / hard）

综合两篇论文：RankGPT 的排列蒸馏和 PRP-Sorting 分别通过什么机制降低大模型重排的使用负担？分别需要什么监督或排序操作？不要把二者描述为同一种训练方法。

允许来源：rankgpt, prp。

预期处理：answer。

- RankGPT 以 ChatGPT 生成的排列为蒸馏目标，使用 10,000 个 MS MARCO 查询及每查询 20 个 BM25 候选。
- 学生通过基于 ChatGPT 排列的 RankNet 损失学习排序。
- PRP-Sorting 把成对偏好用于 Heapsort，其排序计算复杂度为 O(N log N)。

## rerank-q5（dev / limitation / medium）

RankGPT 的局限性部分如何约束重排效果的上限和稳定性？请分别解释首阶段召回与初始候选顺序的作用。

允许来源：rankgpt。

预期处理：answer。

- 研究仅处理重排，排序效果上限受初始段落检索的召回率制约。
- LLM 重排效果对初始段落顺序高度敏感，该顺序通常由 BM25 等首阶段检索器决定。

## rerank-q6（dev / overclaim_trap / hard）

有人据 PRP 表 2 宣称：“PRP-Sliding-10（FLAN-UL2 20B）在两个 TREC 数据集的 NDCG@10 都超过了 RankGPT（gpt-4），因此可全面替代后者。”这一说法成立吗？请给出两个数据集上的对应数值与可支持的结论。

允许来源：prp。

预期处理：qualified_answer。

- 表 2 是重排 BM25 前 100 个候选的 TREC-DL2019 与 TREC-DL2020 实验。
- TREC-DL2019 的 NDCG@10：PRP-Sliding-10 为 72.65，RankGPT gpt-4 为 75.59，前者较低。
- TREC-DL2020 的 NDCG@10：PRP-Sliding-10 为 70.46，RankGPT gpt-4 为 70.56，前者也较低；不支持所述全面替代结论。

## rerank-q7（dev / limitation / medium）

NovelEval-2306 为检验未知知识的重排能力采取了怎样的数据构造方式？它能否证明所有既有基准都没有训练数据污染？请明确其证据覆盖范围。

允许来源：rankgpt。

预期处理：qualified_answer。

- NovelEval-2306 包含来自 4 个领域的 21 个新问题，问题和段落发布于 GPT-4 发布之后。
- 每题通过 Google 搜索获得 20 个候选段落，人工相关性标签为 0、1、2。
- 作者仍指出闭源模型训练数据未披露带来的既有基准污染风险；新测试集不能证明所有旧基准无污染。

## rerank-q8（dev / insufficient_evidence / hard）

准备将 RankGPT 原文的 GPT-4 在线重排方案用于一个要求端到端 P95 延迟不超过 1 秒的系统。论文报告了什么延迟证据？仅据这些证据能否给出该系统的实测 P95 与达标结论？

允许来源：rankgpt。

预期处理：needs_review。

- 作者测试条件下，gpt-4 平均每查询运行时间为 32 秒，gpt-3.5-turbo 为 11 秒。
- 运行时间受 OpenAI 服务、API 版本和网络环境影响。
- 论文平均时间不能给出目标系统的实测端到端 P95；只能指出原方案有显著延迟风险，不能确认目标系统达标。

## rerank-q9（dev / insufficient_evidence / easy）

请给出 PRP 表 2 中 gpt-4 模型由 OpenAI 正式披露的精确参数总数；不要采用公众估算值。

允许来源：prp。

预期处理：abstain。

- 允许材料未提供所要求的官方精确参数量，无法给出该数值。
- 表 2 明确说 OpenAI 未公开模型参数量，相关数字来自公众估算。

## rerank-q10（dev / out_of_scope / easy）

当前只允许使用 RankGPT 和 PRP 论文。请详细还原 Query2doc 论文的稀疏与稠密查询拼接公式，并给出该论文选用的查询重复次数。

允许来源：rankgpt, prp。

预期处理：scope_refusal。

- 请求的 Query2doc 方法细节属于当前允许论文之外，应拒绝越界作答，并说明需要将 Query2doc 纳入允许来源。

## expansion-q1（holdout / single_fact / easy）

Query2doc 对 BM25 与稠密检索分别如何拼接原始查询和伪文档？BM25 中原查询重复几次，这个数是否针对每个数据集单独调优？

允许来源：query2doc。

预期处理：answer。

- BM25 使用原查询重复 n 次后拼接伪文档，以平衡短查询和较长伪文档的权重。
- 论文使用 n = 5，未按数据集单独调优。
- 稠密检索把原查询与伪文档通过 [SEP] 分隔后直接拼接。

## expansion-q2（holdout / synthesis / hard）

综合 Query2doc 和 ITER-RETGEN，生成文本分别在何时、以什么形式参与检索？为什么两篇论文都不支持把生成的检索线索直接当作已经核验的事实？

允许来源：query2doc, iter-retgen。

预期处理：qualified_answer。

- Query2doc 先通过少样本提示生成伪文档，再与原查询拼接以进行检索。
- ITER-RETGEN 首轮仅用问题检索，后续把上一轮生成结果与问题拼接检索，再结合检索段落和问题生成新输出。
- Query2doc 明确展示生成内容的事实错误；ITER-RETGEN 明确不保证上一轮输出正确，因此检索线索的用途不能等同于事实已被核验。

## expansion-q3（holdout / comparison / medium）

Query2doc 的检索评测与 ITER-RETGEN 的任务输出评测使用哪些指标？能否直接拿两篇论文的 nDCG@10 和 Acc† 数值比较哪种方法效果更好？

允许来源：query2doc, iter-retgen。

预期处理：qualified_answer。

- Query2doc 的指标包含 MRR@10、R@k（k 为 50 或 1k）和 nDCG@10，用于检索评测。
- ITER-RETGEN 在多跳问答上使用 EM、F1，在事实核查和常识推理上使用准确率，并用 text-davinci-003 判断输出正确性得到 Acc†。
- nDCG@10 和 Acc† 的被评对象与度量含义不同，不能仅按两个数值的大小形成方法优劣排名。

## expansion-q4（holdout / overclaim_trap / hard）

根据 Query2doc 的域外实验，是否可以宣称“查询扩展对所有检索器、所有域外数据集都只会提升，不会下降”？请用表 2 中 BM25 和 SimLM 在 NFCorpus、Scifact 上的 nDCG@10 给出判断。

允许来源：query2doc。

预期处理：qualified_answer。

- 这些是 BEIR 五个低资源数据集上的零样本域外 nDCG@10 结果。
- BM25 在 NFCorpus 从 32.5 升至 34.9，在 Scifact 从 66.5 升至 68.6。
- SimLM 在 NFCorpus 从 32.7 降至 32.1，在 Scifact 从 62.4 降至 59.5；因此不能宣称所有组合必然改善。

## expansion-q5（holdout / limitation / medium）

Query2doc 是否展示了伪文档中的具体事实错误？请说明错误和论文给出的纠正，并据论文计算全部评测查询中伪文档含事实错误的比例。

允许来源：query2doc。

预期处理：needs_review。

- 论文指出 Monk 主题曲 “It’s a Jungle Out There” 从 2003 年第二季起使用，而不是生成内容声称的 2002 年。
- 文中该处展示的是两条查询的案例分析。
- 案例不足以计算全部评测查询的伪文档事实错误率，所求总体比例无法确定。

## expansion-q6（holdout / insufficient_evidence / medium）

ITER-RETGEN 论文所学习的“逐题自适应停止迭代阈值”具体是多少，它用什么损失函数训练？请只报告论文实际提出的阈值和训练目标。

允许来源：iter-retgen。

预期处理：abstain。

- 允许材料没有提出题干所说的逐题自适应停止阈值及其训练目标，无法报告这些值。
- 方法描述为执行 T 次迭代并将最后的 yT 作为最终响应。

## expansion-q7（holdout / out_of_scope / easy）

当前只允许使用 Query2doc 和 ITER-RETGEN 论文。请报告 PRP 论文表 4 中 PRP-Sliding-10 在反转 BM25 初始顺序前后的 TREC-DL2019 NDCG@10 数值。

允许来源：query2doc, iter-retgen。

预期处理：scope_refusal。

- 所问 PRP 表 4 的结果来自允许范围之外，应拒绝提供该表数值，并说明需要允许使用 PRP 论文。

## retriever-q1（holdout / single_fact / medium）

REPLUG LSR 的训练如何避免检索到训练查询的原始文档而形成平凡答案？为什么训练时要更新文档索引，实际多久重算一次文档嵌入、共训练多少步？

允许来源：replug。

预期处理：answer。

- 外部语料的文档与训练查询所采样自的文档不重叠，以避免平凡检索。
- 检索器参数持续更新会使预计算的文档嵌入过时，因此需要更新嵌入和检索索引。
- 实现中每 3k 步重算文档嵌入，检索器总共微调 25k 步。

## retriever-q2（holdout / comparison / hard）

REPLUG LSR 与 AAR 如何从语言模型获取用于检索器训练的监督？请区分 LM 输出似然分布的匹配与 FiD 注意力产生正例的做法，并说明 AAR 是否抛弃人工正例。

允许来源：replug, aar。

预期处理：answer。

- REPLUG LSR 用给定文档和上下文时真实后续文本的 LM 概率衡量文档帮助，并最小化 LM 似然分布与检索似然分布间的 KL 散度。
- REPLUG LSR 只更新检索器，LM 参数保持固定。
- AAR 汇总源 LM 第一个解码 token 在所有层、头和输入 token 上的 FiD 交叉注意力，得到文档分数。
- AAR 将注意力分数最高的 Top-K 文档与人工正例取并集，形成离散正例集合，并未抛弃人工正例。

## retriever-q3（holdout / synthesis / hard）

综合 REPLUG 和 AAR：“把检索器作为可插拔组件”是否意味着多文档在目标 LM 中必须采用相同的融合方式？请分别描述 REPLUG、AAR 对 decoder-only LM 的接入，以及 AAR 采用的 FiD 多文档接入思路。

允许来源：replug, aar。

预期处理：answer。

- REPLUG 把每篇检索文档分别前置到输入，分别通过 LM，再集成各次输出概率。
- AAR 对 decoder-only LM 可将查询和全部增强文档直接拼接，由 LM 自回归生成答案。
- FiD 则分别编码每个文档与查询的拼接，解码器关注所有编码部分；可插拔并不意味着这三种文档融合方式相同。

## retriever-q4（holdout / overclaim_trap / hard）

AAR 既然声称是通用检索插件，是否已经证明“任意 decoder-only 模型都能不改动地充当源模型，而且所有规模的目标模型均已验证有效”？请用源模型选择和未评估模型范围反驳或支持。

允许来源：aar。

预期处理：qualified_answer。

- AAR 的流程使用 encoder-decoder 源 LM 提供偏好信号，再迁移到源任务之外的目标任务和未见目标 LM。
- 由于 OPT、GPT-neo 的零样本表现及 decoder-only 文档注意力拆分的困难，作者没有选择它们作为源 LM；可增强 decoder-only 目标 LM 不等于已验证它们都可作源 LM。
- 论文明确未评估 11B 的 Flan-T5XXL，以及参数量大于 1.3B 的 OPT，因此不能说所有规模均已验证。

## retriever-q5（holdout / limitation / medium）

REPLUG 能否逐条判定生成答案来自检索文档还是模型参数知识？请说明论文承认的来源归因局限，并报告其评测中依赖检索知识的答案所占比例。

允许来源：replug。

预期处理：needs_review。

- 作者明确说尚不清楚模型何时依赖检索知识、何时依赖参数中的知识。
- 能够追踪答案来自检索数据还是内部参数的可解释系统属于后续研究方向。
- 文中没有提供所求逐答案来源归因统计，不能计算依赖检索知识的答案比例。

## retriever-q6（holdout / insufficient_evidence / easy）

仅根据 AAR 论文，Flan-T5XXL（11B）接入 AAR 后在 MMLU 上的实际测得准确率是多少？请给出原文报告值，不接受按其他规模外推。

允许来源：aar。

预期处理：abstain。

- 允许材料无法提供该模型的实测准确率。
- 作者明确表示受计算资源限制，没有评估参数量为 11B 的 Flan-T5XXL。

## retriever-q7（holdout / out_of_scope / easy）

当前只允许使用 REPLUG 和 AAR 论文。请还原 Query2doc 表 4 中仅用查询、仅用伪文档及二者拼接在 TREC 19 与 TREC 20 上的全部数值。

允许来源：replug, aar。

预期处理：scope_refusal。

- 所求 Query2doc 消融表来自当前允许范围之外，应拒绝给出表中数值，并说明需将 Query2doc 纳入允许来源。

## need-q1（holdout / single_fact / medium）

SKR 收集训练问题的“自知识”标签时，如何根据有检索和无检索的回答划分 D+ 与 D−？两种方式都回答错误的问题，以及两种方式都正确的问题，分别如何处理？

允许来源：skr。

预期处理：answer。

- 依据评价指标 E，无检索表现不低于有检索时归入 D+，否则归入 D−。
- 两种方式都回答错误的问题被丢弃；在准确率或 EM 的二值判定下，两者都正确的保留问题归入 D+。
- D+ 对应可不借助外部信息直接答对的 LLM knowns；D− 对应外部信息带来更准确回答的 LLM unknowns。

## need-q2（holdout / comparison / hard）

M3-Embedding 的 self-knowledge distillation 与 SKR 的 self-knowledge 指的是同一件事吗？请分别说明它们的信号来自哪里，以及用于训练检索表示还是决定是否调用检索。

允许来源：m3-embedding, skr。

预期处理：answer。

- M3 将 dense、lexical、multi-vector 的预测分数加权融合，并把融合分数作为蒸馏教师，改进各检索功能的训练。
- SKR 从同一问题有检索与无检索时的答题表现差异收集已知或未知标签。
- SKR 利用提示或小模型预测当前问题是否需要外部知识，已知时直接回答，未知时调用检索；这不同于 M3 的检索表示训练。

## need-q3（holdout / synthesis / hard）

若要构建一个按需调用长文档检索的问答系统，SKR 和 M3-Embedding 在方法层面可分别承担什么职责？请给出由两文支持的组合思路，说明 M3 的文档长度边界，并保留“这种组合是否已实验证实”的证据边界。

允许来源：m3-embedding, skr。

预期处理：qualified_answer。

- 可由 SKR 判断问题属于已知还是未知；已知时直接回答，未知时进入检索增强流程。
- 需要检索时，可考虑用 M3 提供 dense、multi-vector 或 sparse 检索，并在其所述最多 8,192 tokens 的文档输入范围内处理长文档。
- 上述分工是由两文机制推导的候选组合，而非已有的联合系统实验证明；材料未提供 M3 与 SKR 联用的质量或成本增益。

## need-q4（holdout / overclaim_trap / hard）

M3 的 MLDR 表 3 中 All 的平均 nDCG@10 高于 Dense+Sparse，是否因此能说“加入 multi-vector 后每一种语言都会更好”？请至少核对平均值、英语和中文三处结果，并说明该结论的边界。

允许来源：m3-embedding。

预期处理：qualified_answer。

- 表 3 评估 MLDR 多语言长文档检索，指标是 nDCG@10。
- Dense+Sparse 平均值为 64.8，All 为 65.0，平均值略高。
- 英语从 Dense+Sparse 的 64.2 降到 All 的 63.8，中文从 42.0 降到 40.0，因此平均提升不表示每种语言都提升。
- 原文将三种相关性得分加权融合，权重取决于下游场景；表中结果不能转化为任意权重或任意语言下的必然优势。
- Dense+Sparse 的权重为 (0.2, 0.8, 0)，All 为 (0.15, 0.5, 0.35)；两配置同时改变融合权重，不能把差异解释为保持其他因素不变时增加 multi-vector 的纯因果效果。

## need-q5（holdout / limitation / medium）

SKR 为什么没有只采用 direct prompting 与 in-context learning 判断是否需要检索？请列出论文对这两类方法的具体限制，以及随后提出的两种小模型替代方案。

允许来源：skr。

预期处理：answer。

- 两者都需设计提示并对每个新问题调用 LLM。
- in-context learning 还受上下文偏差和敏感性影响；最大 token 数限制也使其无法利用全部训练问题。
- 论文另外提出训练分类器和最近邻搜索，两者借助显式较小模型完成自知识判断。

## need-q6（holdout / insufficient_evidence / medium）

仅根据 M3-Embedding 论文，请给出原模型在保持全文、不截断也不分块的 16,384-token 文档输入下，已经实测并报告的 nDCG@10。

允许来源：m3-embedding。

预期处理：abstain。

- 材料未报告题目指定的 16,384-token 全文输入实验，不能给出其 nDCG@10。
- 论文明确的输入支持范围为最多 8,192 tokens，超出指定长度上限的文档表现仍需研究。

## need-q7（holdout / out_of_scope / easy）

当前只允许使用 M3-Embedding 和 SKR 论文。请给出 REPLUG LSR 论文中检索器训练查询的数量、每条查询的 token 切分方式和索引更新步数。

允许来源：m3-embedding, skr。

预期处理：scope_refusal。

- 所求 REPLUG LSR 的训练配置不在允许来源范围内，应拒绝越界提供，并说明需允许使用 REPLUG 论文。
