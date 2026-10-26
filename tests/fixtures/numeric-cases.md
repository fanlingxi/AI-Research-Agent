# 数字支持审计命中的结论（启发式：结论中的小数一个都不在所引证据引文中）

## attribution-q10/A0-standard（v8）

结论： 表2报告了 AttrScore 各模型在 AttrEval-GenSearch 上的 F1；其中 Overall 列数值为：零样本 Alpaca \(7B\) 34.3、Alpaca \(13B\) 34.7、Vicuna \(13B\) 41.4、ChatGPT 55.0、GPT-4 85.1；少样本 Alpaca \(7B\) 37.2、Alpaca \(13B\) 34.8、Vicuna \(13B\) 34.1、ChatGPT 53.3、GPT-4 84.3；微调 Roberta \(330M\) 49.8、GPT2 \(1.5B\) 47.4、T5 \(770M\) 61.6、Flan-T5 \(770M\) 64.5、Flan-T5 \(3B\) 75.2、Flan-T5 \(11B\) 72.7、LLaMA \(7B\) 72.5、Alpaca \(7B\) 72.5、Alpaca \(13B\) 73.3、Vicuna \(13B\) 72.1。（适用范围：表2按零样本、少样本、微调三种设置报告 F1；上述为 Overall 列数值） [cite:core-evidence-cde6fa69588446ad8ef81c74b89e6224] 

  引文 core-evidence-cde6fa69588446ad8ef81c74b89e6224: Table 2: The performance (F1 score) of AttrScore with different models on AttrEval-Simulation and AttrEval- GenSearch sets.

## attribution-q10/A1-compact（v8）

结论： 表2报告了AttrScore各模型在AttrEval-GenSearch上的F1分数，其中GPT-4零样本Overall为85.1、少样本为84.3，为各设置最高；ChatGPT零样本55.0、少样本53.3；微调模型中Flan-T5 \(3B\) Overall为75.2，Alpaca \(13B\)为73.3。（适用范围：仅限表2所列模型与设置） [cite:core-evidence-cde6fa69588446ad8ef81c74b89e6224] 

  引文 core-evidence-cde6fa69588446ad8ef81c74b89e6224: Table 2: The performance (F1 score) of AttrScore with different models on AttrEval-Simulation and AttrEval- GenSearch sets.

## hallucination-q5/A1-compact（v10）

结论： 论文指出 FACTSCORE 指标经过归一化，因此可在不同模型间比较；并称通过将输出最多的 Llama-2-70B chat 裁剪为更少事实做了实验验证，其 FACTSCORE 未发生实质变化（例如裁剪到 10 句时从 41.3 变为 42.7）。（适用范围：部分支持：归一化理由有直接证据；裁剪验证的数值细节未在所引证据中） [cite:core-evidence-465487cb5b834ee9a5f7d6072133e42d] 

  引文 core-evidence-465487cb5b834ee9a5f7d6072133e42d: FACTSCORE metric is normalized and hence com- parable across models.

## hallucination-q8/A0-standard（v10）

结论： 表1报告了 Wiki-Category 列表任务上各方法的测试精确率：Llama 65B Few-shot 基线为 0.12，CoVe \(joint\) 为 0.15，CoVe \(two-step\) 为 0.21，CoVe \(factored\) 为 0.22；据此相对基线的差值分别约为 3、9、10 个百分点，但论文未报告置信区间。（适用范围：Wiki-Category 列表任务；Llama 65B 各 CoVe 变体对比 Llama 65B Few-shot 基线） [cite:core-evidence-37d57b123e5b422892e6ace80bbc5ad9] 

  引文 core-evidence-37d57b123e5b422892e6ace80bbc5ad9: Test Precision and average number of positive and negative (hallucination) entities for list-based questions on the Wikidata and Wiki-Category list tasks.

## hallucination-q8/A1-compact（v10）

结论： 表1报告了 Wiki-Category 列表任务上的测试精确率：Llama 65B Few-shot 基线为 0.12，CoVe \(joint\) 为 0.15，CoVe \(two-step\) 为 0.21，CoVe \(factored\) 为 0.22；据此相对基线分别高约 3、9、10 个百分点。（适用范围：Wiki-Category 列表任务；Llama 65B；测试精确率） [cite:core-evidence-37d57b123e5b422892e6ace80bbc5ad9] 

  引文 core-evidence-37d57b123e5b422892e6ace80bbc5ad9: Test Precision and average number of positive and negative (hallucination) entities for list-based questions on the Wikidata and Wiki-Category list tasks.

## pretraining-q9/A0-standard（v10）

结论： 在表 2（基于 DPR 微调的监督评估）中，RetroMAE 在 Natural Question 上的 R@10 为 0.7704，R@100 为 0.8812，两者相差 0.1108。（适用范围：表 2，DPR 微调） [cite:core-evidence-d09ef94baa5a4278968cf14f84ec7cc4] 

  引文 core-evidence-d09ef94baa5a4278968cf14f84ec7cc4: MS MARCO Natural Question Methods MRR@10 MRR@100 R@10 R@100 R@1000 R@10 R@20 R@30 R@50 R@100

结论： 在表 3（基于 ANCE 微调的监督评估）中，RetroMAE 在 Natural Question 上的 R@10 为 0.8044，R@100 为 0.8942，两者相差 0.0898。（适用范围：表 3，ANCE 微调） [cite:core-evidence-d09ef94baa5a4278968cf14f84ec7cc4] 

  引文 core-evidence-d09ef94baa5a4278968cf14f84ec7cc4: MS MARCO Natural Question Methods MRR@10 MRR@100 R@10 R@100 R@1000 R@10 R@20 R@30 R@50 R@100

## pretraining-q9/A1-compact（v8）

结论： 在 Natural Question 的监督评估结果中，RetroMAE 的 R@10 为 0.7704，R@100 为 0.8812。（适用范围：表 2，基于 DPR 微调的监督评估） [cite:core-evidence-d09ef94baa5a4278968cf14f84ec7cc4] 

  引文 core-evidence-d09ef94baa5a4278968cf14f84ec7cc4: MS MARCO Natural Question Methods MRR@10 MRR@100 R@10 R@100 R@1000 R@10 R@20 R@30 R@50 R@100

结论： 在 Natural Question 的监督评估结果中，RetroMAE 的 R@10 为 0.8044，R@100 为 0.8942。（适用范围：表 3，基于 ANCE 微调的监督评估） [cite:core-evidence-d09ef94baa5a4278968cf14f84ec7cc4] 

  引文 core-evidence-d09ef94baa5a4278968cf14f84ec7cc4: MS MARCO Natural Question Methods MRR@10 MRR@100 R@10 R@100 R@1000 R@10 R@20 R@30 R@50 R@100

## query-rewrite-q4/A0-standard（v8）

结论： 在 DL19 的 Table 6 中，无监督 Contriever 的 mAP 为 24.0、nDCG@10 为 44.5、Recall@1k 为 74.6；加入 CSQE 后分别为 44.0、65.6、88.6，即 mAP 提升 20.0、nDCG@10 提升 21.1、Recall@1k 提升 14.0。（适用范围：DL19 数据集，Table 6） [cite:core-evidence-7f7ea5e62440421199cd21d9af5d80d1] 

  引文 core-evidence-7f7ea5e62440421199cd21d9af5d80d1: To test the versatility of CSQE, we apply CSQE on the unsupervised Contriever in Table 6.

## query-rewrite-q4/A1-compact（v10）

结论： 论文将 CSQE 应用于无监督 Contriever，并在表6中报告结果；表6显示 Contriever 的 mAP 为24.0、nDCG@10 为44.5、Recall@1k 为74.6，而 +CSQE 为44.0、65.6、88.6。（适用范围：表6，DL19） [cite:core-evidence-7f7ea5e62440421199cd21d9af5d80d1] 

  引文 core-evidence-7f7ea5e62440421199cd21d9af5d80d1: To test the versatility of CSQE, we apply CSQE on the unsupervised Contriever in Table 6.

结论： 表6中 Contriever 的 mAP 为24.0，+CSQE 为44.0，即 mAP 提升20.0。（适用范围：表6，DL19，mAP） [cite:core-evidence-739b59bb4f5548eeab3eb8e10c77b7b1] 

  引文 core-evidence-739b59bb4f5548eeab3eb8e10c77b7b1: DL19 DL20 mAP nDCG@10 Recall@1k mAP nDCG@10 Recall@1k

结论： 表6中 Contriever 的 nDCG@10 为44.5、Recall@1k 为74.6，+CSQE 为65.6、88.6，即 nDCG@10 提升21.1、Recall@1k 提升14.0。（适用范围：表6，DL19，nDCG@10 与 Recall@1k） [cite:core-evidence-739b59bb4f5548eeab3eb8e10c77b7b1] 

  引文 core-evidence-739b59bb4f5548eeab3eb8e10c77b7b1: DL19 DL20 mAP nDCG@10 Recall@1k mAP nDCG@10 Recall@1k

