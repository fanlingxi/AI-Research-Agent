from app.agents.critic_agent import CriticAgent
from app.agents.memory_agent import MemoryAgent
from app.agents.reflection_agent import ReflectionAgent
from app.evaluation.evaluator import ResearchEvaluator
from app.graph.nodes import memory_context_node
from app.memory.store import JsonMemoryStore
from app.schemas.documents import PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphPath, GraphRelation


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
    assert critique.needs_revision


def test_memory_trace_reports_disabled_state() -> None:
    result = memory_context_node({"query": "GraphRAG", "memory_enabled": False})

    assert result["traces"][0].message == "已关闭长期记忆，跳过召回。"
