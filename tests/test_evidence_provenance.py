from app.evaluation.evaluator import ResearchEvaluator
from app.evidence.provenance import assess_evidence, rerank_evidence_hits
from app.schemas.documents import PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphPath, GraphRelation


def _hit(tier: str, score: float, suffix: str) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=f"chunk:{suffix}",
        paper_id=f"paper:{suffix}",
        title=f"Evidence {suffix}",
        text="GraphRAG knowledge graph retrieval evidence.",
        score=score,
        source_tier=tier,  # type: ignore[arg-type]
        metadata={"source_tier": tier, "source": tier},
    )


def test_offline_demo_is_simulation_and_cannot_be_retained() -> None:
    paper = PaperMetadata(
        id="paper:demo",
        title="Demo",
        source="offline-demo",
        source_tier="offline_demo",
    )

    assessment = assess_evidence([paper], [_hit("offline_demo", 0.95, "demo")], 0.65)

    assert assessment.status == "simulation"
    assert not assessment.admissible
    assert assessment.score == 0.0


def test_primary_fulltext_wins_over_higher_scoring_offline_demo() -> None:
    hits = [
        _hit("offline_demo", 0.98, "demo"),
        _hit("primary_fulltext", 0.42, "pdf"),
    ]

    ranked = rerank_evidence_hits(hits, top_k=2)

    assert [hit.source_tier for hit in ranked] == ["primary_fulltext"]


def test_source_quality_is_a_critical_evaluation_gate() -> None:
    paper = PaperMetadata(
        id="paper:demo",
        title="GraphRAG Demo",
        source="offline-demo",
        source_tier="offline_demo",
    )
    hit = _hit("offline_demo", 0.9, "demo")
    entity = GraphEntity(id="concept:graphrag", name="GraphRAG", type="Concept")
    relation = GraphRelation(
        id="rel:demo",
        source_id="paper:demo",
        target_id=entity.id,
        type="DISCUSSES",
    )
    report = "\n".join(
        ["## 候选论文", "## 检索证据", "## 知识图谱", "## GraphRAG 推理总结"]
    )

    evaluation = ResearchEvaluator().evaluate(
        query="GraphRAG knowledge graph",
        report=report,
        papers=[paper],
        retrieval_hits=[hit],
        graph_entities=[entity],
        graph_relations=[relation],
        graph_paths=[GraphPath(nodes=[entity], relations=[relation], score=1.0)],
    )

    source_metric = next(metric for metric in evaluation.metrics if metric.name == "source_quality")
    assert source_metric.score == 0.0
    assert evaluation.evidence_status == "simulation"
    assert not evaluation.evidence_admissible
    assert not evaluation.passed
