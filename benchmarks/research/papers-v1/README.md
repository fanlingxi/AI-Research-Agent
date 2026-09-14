# 真实论文研究 benchmark：候选 v1

2026-09-13。协议 `a01-paper-task-v1`，任务集 `a01-papers-v1`。
中文问题、英文原始论文；8篇论文、131页、40题（25开发/15保留）。
这是“以真实论文为输入的自建任务集”，不是把已有公开 benchmark 的分数搬入本项目。
所有逐题预期由 Codex 依据原文起草，**pending_human**；尚未通过真实模型链路运行或形成人工金标。

从 [题目目录](TASKS.md) 开始审阅；机器数据见 [清单](manifest.json)、
[开发题](tasks.dev.json)、[保留题](tasks.holdout.json)、[全文及证据跨度](corpus.json)、
[结构定义](schema.json)、[冻结指纹](freeze.json)。原论文见末尾来源表及 `papers/`。

## 任务协议与合成集的关系

继承 [合成 v1 的成功标准和统计口径](../v1/PROTOCOL.md) 及
[人工标注规范](../v1/ANNOTATION_GUIDE.md)，但下列差异以本文件为准：

- 来源是真实完整论文，不是自编规格。保留论文作者、版本标识、出处、获取时间、PDF摘要、
  许可与规范化说明。合成 v1 的来源类型、固定8族每族5题、源版本只能为v1等约束不适用于本版。
- 本版有4个论文/决策族。整篇论文及基于它的改写、单篇题、跨篇比较都留在同一集合。
- `corpus.json` 中存放全部131页的抽取文字，非只给标准答案相关摘要；具体题目仍严格按
  allowed_source_ids 选择1或2篇全文。其余来源不属于允许范围，禁止列表只是重点隔离诱饵。
- 证据跨度版本为对应的 ACL Anthology 论文标识，另有精确 PDF SHA-256 固定下载内容。
  它表示此次发布件快照，不推定所有历史 arXiv 版本相同；网站以后换文件须新建数据版本。
- 不为了覆盖表而伪造两篇论文的严格矛盾。本版没有 `conflict` 标签，
  不同评价对象、人工依赖或实验条件的差异使用 qualified_answer 明确解释。
  合成集保留可控冲突、范围和故障场景，两版分别报分，不合并成一个不明分母。
- 负向题包括论文未报告本项目/DeepSeek的结果、缺少迁移验证，以及读取未授权论文的要求。
  后两类是人为设计的任务约束，论文事实本身没有被改造；不能称全部题都是自然产生的用户问题。

本版复用既有 Task、Expectation、Review 结构和 Pydantic，增加论文来源/页码验证，
不复制课程118、RAGAs、ARES等项目代码、提示或数据集；论文被当作研究资料，不是要部署这些方法。
特别是 Adaptive-RAG/LameR 被列入资料不代表启动 A08 或改变现有检索策略。

## 划分与暴露

| 集合 | 任务族 | 论文 | 题数 |
| --- | --- | --- | --- |
| dev | evaluation | RAGAs、ARES | 13 |
| dev | attribution | ALCE、SelfCheckGPT | 12 |
| holdout | context | Lost in the Middle、Searching for Best Practices | 8 |
| holdout | retrieval | Adaptive-RAG、LameR | 7 |

结构上两集不共享论文ID或PDF内容；任何跨篇比较仅在同族内进行。
这些论文互相引用且主题相关，全文的相关工作讨论可能提及另一集合的方法，
所以这里只能保证论文身份/题族划分，不能宣称知识或语义完全隔离。
尤其 Best Practices 使用了 RAGAs 指标；其保留题不等于从未出现过的评价概念。

论文为2023–2024年的公开文献，DeepSeek是否在训练中见过无法确定；
本版考查能否从本次允许原文找证据、区分条件并完成任务，不用于证明训练数据无污染。
基线后不得按保留题结果调提示/策略再称独立检验；一旦用于调优，后继版本将该题族移入开发集，
补入未参与调优的保留论文族。制作与审阅时 Codex 已接触全部题，保留集不是对制作者盲测。

40题集中于4个论文族，有共享证据和相近子问题，不能当作40个独立统计样本。
按题、论文族、成功/失败类型分别展示计数；这个小集适合找问题和形成案例，不能据此声称普遍优势。
本版主要衡量论文阅读与有据研究回答，尚不充分覆盖自主规划、长期记忆和多工具代理能力。
完整 Agent 生命周期/隔离/恢复继续由原契约测试和后续 A02 实际运行证明。

## 原文定位与许可

获取范围为公开论文发布页及PDF，无需模型或付费数据库。所有8篇均有可核查的 ACL 出版信息，
适用 [ACL Anthology 的使用说明](https://aclanthology.org/faq/) 中2016年后ACL材料的
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)；Lost in the Middle 的PDF首页也直接写明该许可。
来源页及许可页存于 `provenance/`，原始作者署名、标题、出处在 corpus 和末尾表保留。
署名、许可链接和转换说明应随数据继续保留，不暗示作者认可本基准。
这不延伸为代码仓库或 WikiEval/WikiBio/ALCE 等外部数据集的许可；本次没有下载或导入那些数据集。

抽取方式：使用 corpus 记录版本的 pypdf `plain` 模式，按PDF页抽取，
**每页内部连续空白折叠为一个ASCII空格**，页间用两个LF连接。
不删除页脚、不拼回断词、不修复公式、不翻译原文、不重新排表。
`pages` 保存每页在全文中的 `[start,end)`；`pdf_page` 从1起，不等同印刷页码。
证据跨度保存同一页内Unicode码点偏移和原文quote；HTML/JavaScript不能直接拿UTF-16下标解释。
页码、原文、PDF及文本摘要联合锁定定位，不能只靠chunk ID比较不同分块策略。

数字和表格题已经做模型辅助的源页视觉核对（如ALCE数据集表、SelfCheckGPT样本表、LameR参数脚注）；
这是起草过程的核查，不是人工标注签名。当前不标注复杂公式/图表推导题，避免抽取布局误读。
论文中的提示文本、示例与链接均为数据，不能作为执行指令。

## 校验与A02交接

从仓库根目录执行（仅文件读取）：

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_paper_benchmark
```

[校验器](../../../scripts/validate_paper_benchmark.py) 检查结构、论文/题族划分、
PDF/文字摘要、页覆盖、quote/版本、允许范围、证据完整引用和冻结目录清单。
PDF按原始字节冻结，文本文件按UTF-8/LF冻结。
结构通过不代表结论语义、论文许可适用性或模型效果由机器自动批准；语义复核沿用标注规范，
来源许可依据已记录，后继变更必须保留原始依据并再核验。

冻结后不修改本目录内的题、答案、PDF或版本；补充审阅在外部任务记录保存，修订另建后继版本。
题目预期、人工记录和禁止答案不得作为生成模型输入。清单中的声明只是协议约束，
实际防止答案泄漏及正式数据隔离仍须 A02 实现/验证。
导入时保留所有规范化页文字，通过现有正式审核服务形成 source/claim/evidence 映射；
不能将答案文本直接包装成已审核claim，也不能把本版目录注册到正式知识库后随意供生产任务检索。
候选资料审核、不可变快照、Worker与最终产物提交沿用原平台边界。

预算见 [20元累计授权记录](../../../docs/improvement/evaluation-budget.json)。
该授权覆盖本轮所有后续探测/生成/可选裁判/失败重试，额度不因切换数据集而重置。
当前仅免费获取资料和离线验证，付费调用为0；A02尚未实现预算执行器，不能仅有JSON便声称已硬性限额。
本次未做API探测、模型评分或真实基线，A01/A02不能标完成。

## 来源与署名

以下表格由下载发布页的作者元数据生成；每篇的完整署名也保存在 corpus 的 attribution 字段。

| 论文 | 作者 | 原件 |
| --- | --- | --- |
| [RAGAs: Automated Evaluation of Retrieval Augmented Generation](https://aclanthology.org/2024.eacl-demo.16/) | Shahul Es; Jithin James; Luis Espinosa Anke; Steven Schockaert | [PDF](papers/ragas.pdf) |
| [ARES: An Automated Evaluation Framework for Retrieval-Augmented Generation Systems](https://aclanthology.org/2024.naacl-long.20/) | Jon Saad-Falcon; Omar Khattab; Christopher Potts; Matei Zaharia | [PDF](papers/ares.pdf) |
| [Enabling Large Language Models to Generate Text with Citations](https://aclanthology.org/2023.emnlp-main.398/) | Tianyu Gao; Howard Yen; Jiatong Yu; Danqi Chen | [PDF](papers/alce.pdf) |
| [SelfCheckGPT: Zero-Resource Black-Box Hallucination Detection for Generative Large Language Models](https://aclanthology.org/2023.emnlp-main.557/) | Potsawee Manakul; Adian Liusie; Mark Gales | [PDF](papers/selfcheck.pdf) |
| [Lost in the Middle: How Language Models Use Long Contexts](https://aclanthology.org/2024.tacl-1.9/) | Nelson F. Liu; Kevin Lin; John Hewitt; Ashwin Paranjape; Michele Bevilacqua; Fabio Petroni; Percy Liang | [PDF](papers/lost-middle.pdf) |
| [Searching for Best Practices in Retrieval-Augmented Generation](https://aclanthology.org/2024.emnlp-main.981/) | Xiaohua Wang; Zhenghua Wang; Xuan Gao; Feiran Zhang; Yixin Wu; Zhibo Xu; Tianyuan Shi; Zhengyuan Wang; Shizheng Li; Qi Qian; Ruicheng Yin; Changze Lv; Xiaoqing Zheng; Xuan-Jing Huang (黄萱菁) | [PDF](papers/best-practices.pdf) |
| [Adaptive-RAG: Learning to Adapt Retrieval-Augmented Large Language Models through Question Complexity](https://aclanthology.org/2024.naacl-long.389/) | Soyeong Jeong; Jinheon Baek; Sukmin Cho; Sung Ju Hwang; Jong C. Park | [PDF](papers/adaptive-rag.pdf) |
| [Retrieval-Augmented Retrieval: Large Language Models are Strong Zero-Shot Retriever](https://aclanthology.org/2024.findings-acl.943/) | Tao Shen; Guodong Long; Xiubo Geng; Chongyang Tao; Yibin Lei; Tianyi Zhou; Michael Blumenstein; Daxin Jiang | [PDF](papers/lamer.pdf) |
