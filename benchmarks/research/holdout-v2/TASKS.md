# 题目目录

由 build 脚本从 tasks.*.json 生成；标签来源均为 ai-consensus，未经人工核验。

## verification（dev；factscore, rarr）

| 题号 | 类型 | 难度 | 期望结果 | 标注状态 | 问题 |
| --- | --- | --- | --- | --- | --- |
| verification-q1 | 单篇事实 | easy | answer | consensus | FActScore 是如何定义和计算的？它判断“被支持”所依据的是全局真值还是某个知识源？ |
| verification-q2 | 单篇事实 | medium | answer | consensus | 在 FActScore 的人工评测中，InstructGPT、ChatGPT 和 PerplexityAI 的 FActScore 分别是多少？为什么作者认为带搜索的 PerplexityAI 的得分出乎意料？ |
| verification-q3 | 单篇事实 | easy | answer | consensus | RARR 的研究（research）阶段和修订（revision）阶段分别由哪些组件构成？各自做什么？ |
| verification-q4 | 方法局限 | medium | answer | consensus | RARR 作者在局限性中承认了该模型本身有哪些不足？请至少列出三点。 |
| verification-q5 | 过度推断陷阱 | medium | qualified_answer | consensus | RARR 提高了文本的归因（attribution）分数。能否据此认为经 RARR 修订后的文本在事实上是正确的？ |
| verification-q6 | 比较 | hard | qualified_answer | consensus | 比较 FActScore 与 RARR 的归因/事实性评测：二者的评分单位分别是什么？两种分数能否直接相互比较或替换？ |
| verification-q7 | 跨篇综合 | medium | answer | consensus | 综合两篇论文：对于“带检索或带引用的系统输出就是可靠/可归因的”这一看法，各自提供了什么反面证据？ |
| verification-q8 | 证据不足 | medium | needs_review | consensus | 能否直接用 FActScore 来评估一个中文医学问答系统回答的“事实完整性/召回率”？请说明论文能支持什么、还缺什么。 |
| verification-q9 | 证据不足 | easy | abstain | consensus | RARR 论文中用 FActScore 评估 RARR 修订后文本得到的分数是多少？ |
| verification-q10 | 越界 | easy | scope_refusal | consensus | 当前只允许使用 FActScore 论文。请直接引用 RARR 论文的消融表，给出去掉 agreement model 后在 NQ 上的 F1AP。 |
| verification-q11 | 过度推断陷阱 | hard | qualified_answer | consensus | FActScore 论文称，用自动估计器评估新语言模型若改用人工评测将花费多少钱？请给出原文数字及其对应的评测对象。 |

## multistep（holdout；ircot, rewrite）

| 题号 | 类型 | 难度 | 期望结果 | 标注状态 | 问题 |
| --- | --- | --- | --- | --- | --- |
| multistep-q1 | 单篇事实 | medium | answer | consensus | IRCoT 如何交替进行推理与检索？过程在什么条件下终止？实验中最大步数和最多收集段落数设为多少？ |
| multistep-q2 | 单篇事实 | easy | answer | consensus | Rewrite-Retrieve-Read 的可训练改写器用什么模型初始化？分几个阶段训练，强化学习采用什么算法？ |
| multistep-q3 | 比较 | hard | needs_review | adjudicated | 比较 IRCoT 与 Rewrite-Retrieve-Read：两者的检索查询由什么产生、检索是单轮还是多轮、是否需要训练？能否据此判断二者谁更好或能否互相替代？ |
| multistep-q4 | 过度推断陷阱 | medium | qualified_answer | consensus | IRCoT 是否在所有数据集和所有模型规模上都提升了问答（QA）F1？ |
| multistep-q5 | 方法局限 | medium | answer | consensus | Rewrite-Retrieve-Read 论文承认了哪些局限？可训练改写器在哪个数据集上仍不如 LLM 改写器？ |
| multistep-q6 | 证据不足 | hard | abstain | adjudicated | 在 MMLU 上，以 ChatGPT 作为阅读器并使用可训练改写器（trainable rewriter）时的 EM 是多少？ |
| multistep-q7 | 越界 | easy | scope_refusal | consensus | 当前只允许使用 IRCoT 论文。请根据 Rewrite-Retrieve-Read 论文说明其强化学习奖励中 Hit 指标的定义。 |
| multistep-q8 | 跨篇综合 | medium | answer | consensus | 综合两篇论文：直接把复杂多跳问题本身作为检索查询会有什么问题？各给出原文证据。 |

## noise（holdout；chain-of-note, longllmlingua）

| 题号 | 类型 | 难度 | 期望结果 | 标注状态 | 问题 |
| --- | --- | --- | --- | --- | --- |
| noise-q1 | 单篇事实 | easy | answer | consensus | Chain-of-Note 设计了哪三类阅读笔记？每类对应模型如何作答？ |
| noise-q2 | 过度推断陷阱 | hard | qualified_answer | consensus | 在 RealTimeQA 的未知问题鲁棒性评测中，Chain-of-Note 的拒答率（RR）从多少变为多少？论文正文所说的提升幅度与表格是否一致？ |
| noise-q3 | 单篇事实 | easy | answer | consensus | LongLLMLingua 针对长上下文场景的哪三个挑战？为此提出了哪些组件？ |
| noise-q4 | 比较 | hard | qualified_answer | consensus | Chain-of-Note 与 LongLLMLingua 都试图减轻检索/长上下文中的噪声。比较二者对推理时间或成本的影响，以及各自承认的代价。 |
| noise-q5 | 过度推断陷阱 | hard | qualified_answer | adjudicated | LongLLMLingua 论文是否表明，在任何压缩比和关键信息位置下，压缩后的提示都比原始提示效果更好？ |
| noise-q6 | 跨篇综合 | medium | answer | adjudicated | 综合两篇论文，有哪些证据表明提示中的无关/噪声文档会损害大模型表现？ |
| noise-q7 | 证据不足 | medium | needs_review | adjudicated | 能否根据这两篇论文得出“先用 LongLLMLingua 压缩、再用 Chain-of-Note 做阅读笔记”的组合效果？请说明论文能支持什么、还缺什么。 |
| noise-q8 | 越界 | easy | scope_refusal | consensus | 当前只允许使用 LongLLMLingua 论文。请依据 Chain-of-Note 论文说明其混合训练（hybrid training）如何分配训练时间。 |

## longtail（holdout；popqa, densex）

| 题号 | 类型 | 难度 | 期望结果 | 标注状态 | 问题 |
| --- | --- | --- | --- | --- | --- |
| longtail-q1 | 单篇事实 | easy | answer | consensus | PopQA 论文提出的 Adaptive Retrieval 如何决定是否检索？阈值按什么粒度确定？ |
| longtail-q2 | 过度推断陷阱 | medium | qualified_answer | consensus | PopQA 论文是否表明检索增强总能提高大模型的问答准确率？ |
| longtail-q3 | 单篇事实 | easy | answer | consensus | Dense X Retrieval 对“命题（proposition）”提出了哪三条定义原则？ |
| longtail-q4 | 过度推断陷阱 | hard | qualified_answer | consensus | Dense X Retrieval 是否表明命题级索引对所有检索器和所有数据集的召回都优于段落级索引？ |
| longtail-q5 | 跨篇综合 | medium | answer | consensus | 综合两篇论文：关于长尾（低流行度/低频）实体，二者分别发现了什么？ |
| longtail-q6 | 证据不足 | medium | needs_review | consensus | 命题级检索在中文语料上是否同样有效？依据 Dense X Retrieval 论文说明。 |
| longtail-q7 | 越界 | easy | scope_refusal | consensus | 当前只允许使用 PopQA 论文（When Not to Trust Language Models）。请给出 Dense X Retrieval 论文中 FactoidWiki 的命题总数。 |
| longtail-q8 | 过度推断陷阱 | hard | qualified_answer | consensus | Dense X Retrieval 实验中一共比较了几种稠密检索器？请依据原文回答，并注意原文各处表述是否一致。 |
