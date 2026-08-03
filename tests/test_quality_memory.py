from app.agents.critic_agent import CriticAgent
from app.agents.memory_agent import MemoryAgent
from app.agents.reflection_agent import ReflectionAgent
from app.agents.writer_agent import WriterAgent
from app.evaluation.evaluator import ResearchEvaluator
from app.graph.nodes import memory_context_node
from app.llms.provider import LLMClient
from app.memory.store import JsonMemoryStore
from app.schemas.documents import PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphPath, GraphRelation
from app.schemas.research import ResearchPlan


class ReportLLM(LLMClient):
    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        if "返回 JSON" in prompt:
            return '{"issues": ["结论需要收紧证据边界。"], "suggestions": ["补充引用编号。"]}'
        return "\n".join(
            [
                "## 核心发现",
                "证据 [1] 支持 GraphRAG 的图增强检索价值。",
                "## 机制分析",
                "图谱路径和向量检索共同提供可追溯上下文。",
                "## 局限性",
                "现有证据覆盖有限，且不同论文分别覆盖方法、记忆和评估子主题，不能将它们直接解释为统一的性能结论。",
                "## 结论与下一步",
                "需要继续补充全文和基准证据，并针对相同任务设置比较检索、图谱构建和报告生成的实际效果。",
            ]
        )


def _quality_inputs():
    paper = PaperMetadata(id="paper:1", title="GraphRAG Paper", source="test")
    hit = RetrievalHit(
        chunk_id="chunk:1",
        paper_id="paper:1",
        title="GraphRAG Paper",
        text="GraphRAG combines vector retrieval and graph paths.",
        score=0.9,
    )
    entity = GraphEntity(id="concept:graphrag", name="GraphRAG", type="Concept")
    relation = GraphRelation(
        id="rel:1",
        source_id="paper:1",
        target_id="concept:graphrag",
        type="DISCUSSES",
    )
    path = GraphPath(nodes=[entity], relations=[relation], score=1.0)
    report = "\n".join(
        [
            "## 候选论文",
            "## 检索证据",
            "Source: test",
            "## 知识图谱",
            "## GraphRAG 推理总结",
        ]
    )
    return report, [paper], [hit], [entity], [relation], [path]


def test_evaluator_critic_and_reflection_append_quality_sections() -> None:
    report, papers, hits, entities, relations, paths = _quality_inputs()

    evaluation = ResearchEvaluator().evaluate(
        query="GraphRAG vector retrieval",
        report=report,
        papers=papers,
        retrieval_hits=hits,
        graph_entities=entities,
        graph_relations=relations,
        graph_paths=paths,
    )
    critique = CriticAgent().review(report=report, evaluation=evaluation)
    reflection = ReflectionAgent().revise(
        report=report,
        evaluation=evaluation,
        critique=critique,
    )

    assert evaluation.metrics
    assert "## 评估结果" in reflection.revised_report
    assert "## Critic 审查" in reflection.revised_report


def test_memory_agent_recalls_saved_research(tmp_path) -> None:
    store = JsonMemoryStore(str(tmp_path / "memory.json"))
    agent = MemoryAgent(store=store, enabled=True)
    report, papers, hits, entities, relations, paths = _quality_inputs()
    evaluation = ResearchEvaluator().evaluate(
        query="GraphRAG literature review",
        report=report,
        papers=papers,
        retrieval_hits=hits,
        graph_entities=entities,
        graph_relations=relations,
        graph_paths=paths,
    )

    record = agent.remember(
        query="GraphRAG literature review",
        report=report,
        evaluation=evaluation,
        tags=["GraphRAG"],
    )
    snapshot = agent.recall("GraphRAG graph review")

    assert record is not None
    assert snapshot.records
    assert "GraphRAG literature review" in snapshot.summary


def test_memory_agent_recalls_related_chinese_research(tmp_path) -> None:
    store = JsonMemoryStore(str(tmp_path / "memory.json"))
    agent = MemoryAgent(store=store, enabled=True)
    report, papers, hits, entities, relations, paths = _quality_inputs()
    evaluation = ResearchEvaluator().evaluate(
        query="多智能体协作科研分析方法",
        report=report,
        papers=papers,
        retrieval_hits=hits,
        graph_entities=entities,
        graph_relations=relations,
        graph_paths=paths,
    )

    agent.remember(
        query="多智能体协作科研分析方法",
        report=report,
        evaluation=evaluation,
        tags=["多智能体", "科研分析"],
    )
    snapshot = agent.recall("多智能体系统在科研分析中的协作方法")

    assert snapshot.records
    assert "多智能体协作科研分析方法" in snapshot.summary


def test_evaluator_and_critic_flag_irrelevant_evidence() -> None:
    report, papers, hits, entities, relations, paths = _quality_inputs()
    unrelated_paper = papers[0].model_copy(
        update={"title": "Particle Physics Education", "abstract": "School outreach."}
    )
    unrelated_hit = hits[0].model_copy(
        update={"title": unrelated_paper.title, "text": unrelated_paper.abstract}
    )

    evaluation = ResearchEvaluator().evaluate(
        query="GraphRAG query-focused summarization",
        report=report,
        papers=[unrelated_paper],
        retrieval_hits=[unrelated_hit],
        graph_entities=entities,
        graph_relations=relations,
        graph_paths=paths,
    )
    critique = CriticAgent().review(report=report, evaluation=evaluation)
    relevance = next(
        metric for metric in evaluation.metrics if metric.name == "retrieval_relevance"
    )

    assert relevance.score < 0.65
    assert not evaluation.passed
    assert critique.needs_revision


def test_memory_trace_reports_disabled_state() -> None:
    result = memory_context_node({"query": "GraphRAG", "memory_enabled": False})

    assert result["traces"][0].message == "已关闭长期记忆，跳过召回。"


def test_writer_and_reflection_use_a_bounded_revision() -> None:
    report, papers, hits, _, _, paths = _quality_inputs()
    writer = WriterAgent(llm=ReportLLM())
    draft = writer.draft(
        query="GraphRAG vector retrieval",
        plan=ResearchPlan(objective="验证 Writer"),
        papers=papers,
        retrieval_hits=hits,
        graph_paths=paths,
    )
    evaluation = ResearchEvaluator().evaluate(
        query="GraphRAG vector retrieval",
        report=report,
        papers=papers,
        retrieval_hits=hits,
        graph_entities=[],
        graph_relations=[],
        graph_paths=[],
    )
    critique = CriticAgent(llm=ReportLLM()).review(report=report, evaluation=evaluation)
    critique.needs_revision = True
    reflection = ReflectionAgent(writer=writer).revise(
        report=draft,
        evaluation=evaluation,
        critique=critique,
        query="GraphRAG vector retrieval",
        include_audit=False,
    )

    assert "## 核心发现" in draft
    assert "## 修订后的研究报告" in reflection.revised_report
    assert "结论需要收紧证据边界。" in critique.issues
