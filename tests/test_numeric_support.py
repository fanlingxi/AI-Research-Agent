"""Quote-local arithmetic, exemptions, and the frozen numeric audit examples."""

import re
from pathlib import Path

import pytest

from app.agent.numeric_support import unsupported_numbers
from app.agent.structured_research import NumericSupportResearchWorkflow
from tests.test_research_v10 import prepare, shared_fixture


@pytest.mark.parametrize("assertion,scope,quote", [
    ("从 0.7704 到 0.8812，差 0.1108", "", "Scores are 0.8812 and 0.7704."),
    ("0.12→0.22 提升 10 个百分点", "", "The baseline is 0.12 and the result is 0.22."),
    ("Score 85.1%", "", "The result is 85.1."),
    ("3 个数据集", "", "We evaluated on three datasets."),
    ("Scores .12 and １，０００", "−０．５", "The scores are 0.12, 1000, and -0.5."),
    ("4 and 8, ratio 2, sum 12", "", "Measured values were 4 and 8."),
    ("由 3 到 4，提升 33.3%", "", "The measurements were 3 and 4."),
    ("由 1 到 9，比值 0.111", "", "We measured 1 and 9."),
    ("由 0.1 到 0.225，差 0.13", "", "We measured 0.1 and 0.225."),
    ("分数 12%", "", "The score is 0.12."),
    ("提升 12 个百分点", "", "The change is 0.12."),
    ("准确率为 81-83%", "", "Accuracy ranges from 81 to 83 percent."),
    ("无数值的结论", "", "No numerical evidence."),
    ("三种方法", "", "Chinese numerals are a documented v1 limitation."),
    ("65B Llama-2-70B GPT-4 BM25 nDCG@10 R@100 F1 T5 7B", "", "No quantities."),
    ("表2、图 3、第4、式5、公式6、章7、节8、页9、附录10、算法11", "", "No quantities."),
    ("Table 2, Fig. 3, Figure 4, Section 2.1, Eq. (5), Appendix 6, Algorithm 7",
     "", "No quantities."),
    ("et al., 2020; （2023）; [1, 2–4]", "", "No quantities."),
])
def test_supported_or_exempt_numbers(assertion, scope, quote):
    assert unsupported_numbers(assertion, scope, [quote]) == []


@pytest.mark.parametrize("assertion,scope,quote", [
    ("共 100 项", "", "We measured only R@100 and nDCG@100."),
    ("共 2 项", "", "Table 2 shows results of GPT-2."),
    ("共 3 项", "", "Table three shows the main results."),
    ("下降 -83%", "", "The measured score is 83."),
    ("共 2023 项", "", "The citation is et al., 2023 and (2023)."),
    ("共 12 项", "", "The value is 0.12."),
    ("差 0.1108", "", "The scores are 0.8812 and 0.7704."),
    ("0.7704 和 0.8812 差 0.1108", "", "Results appear in Table 2."),
    ("3 个数据集", "", "We used three-models, not quantities."),
    ("范围结论", "共 100 项", "We report R@100."),
    ("从 3 到 4 提升 33.3", "", "The scores are 3 and 4."),
    ("从 0.12 到 0.22 提升 10", "", "We measured 0.12 and 0.22."),
    ("从 12 到 22 提升 1000 个百分点", "", "We measured 12 and 22."),
    ("2 加 3 为 5，再加为 8", "", "Only the operands 2 and 3 are supported."),
    ("BM25 为24.0，Contriever 为44.0，提升20.0", "", "BM25 achieves 24.0."),
    ("BM25+CSQE 的 mAP 为47.6、nDCG@10 为68.6、Recall@1k 为89.0，"
     "均高于 Contriever+CSQE 的44.0、65.6、88.6", "表6，DL19",
     "BM25+CSQE 47.6 68.6 89.0 in Table 6."),
])
def test_unsupported_numbers_fail_closed(assertion, scope, quote):
    assert unsupported_numbers(assertion, scope, [quote])


def numeric_cases():
    text = (Path(__file__).parent / "fixtures/numeric-cases.md").read_text(encoding="utf-8")
    return re.findall(r"结论： (.*?) \[cite:[^\]]+\].*?引文 [^:]+: ([^\n]+)", text, re.S)


@pytest.mark.parametrize("assertion,quote", numeric_cases())
def test_supplied_numeric_cases_are_omitted(assertion, quote):
    assert unsupported_numbers(assertion, "", [quote])
    package, report = shared_fixture()
    for bundle in package.knowledge.claim_bundles:
        bundle.evidence[0].quote = quote
    finding = report["answer_parts"][0]["findings"][0]
    # Preserve the supplied assertion and scope separately, without citation markup.
    body, _, scope = assertion.partition("（适用范围：")
    finding.update(assertion=body, scope=scope.removesuffix("）"),
                   support=[{"evidence_id": "shared", "quote": quote}])
    draft = prepare(NumericSupportResearchWorkflow, report, package)
    assert draft.answer_status == "insufficient_evidence"
    assert len(draft.numeric_audit["omitted"]) == 1


def test_cases_fixture_is_not_empty():
    assert len(numeric_cases()) == 13
