from app.agents.critic_agent import CriticAgent
from app.agents.memory_agent import MemoryAgent
from app.agents.reflection_agent import ReflectionAgent
from app.evaluation.evaluator import ResearchEvaluator
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
            "## Candidate Papers",
            "## Retrieved Evidence",
            "Source: test",
            "## Knowledge Graph",
            "## GraphRAG Summary",
        ]
    )
    return report, [paper], [hit], [entity], [relation], [path]


def test_evaluator_critic_and_reflection_append_quality_sections() -> None:
    report, papers, hits, entities, relations, paths = _quality_inputs()

    evaluation = ResearchEvaluator().evaluate(
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
    assert "## Evaluation" in reflection.revised_report
    assert "## Critic Review" in reflection.revised_report


def test_memory_agent_recalls_saved_research(tmp_path) -> None:
    store = JsonMemoryStore(str(tmp_path / "memory.json"))
    agent = MemoryAgent(store=store, enabled=True)
    report, papers, hits, entities, relations, paths = _quality_inputs()
    evaluation = ResearchEvaluator().evaluate(
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
