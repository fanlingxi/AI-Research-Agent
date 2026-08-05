import json

from app.obsidian.exporter import ObsidianVaultExporter
from app.schemas.documents import PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphRelation
from app.schemas.quality import EvaluationMetric, EvaluationResult


def _formal_evaluation() -> EvaluationResult:
    return EvaluationResult(
        overall_score=0.9,
        passed=True,
        summary="正式证据已通过门槛。",
        metrics=[EvaluationMetric(name="source_quality", score=1.0, reason="真实全文。")],
        evidence_status="formal",
        evidence_admissible=True,
    )


def test_vault_export_writes_linked_markdown_and_graph_artifacts(tmp_path) -> None:
    paper = PaperMetadata(
        id="pdf:graphrag",
        title="GraphRAG Paper",
        source="pdf",
        source_tier="primary_fulltext",
        pdf_url="data/raw_papers/graphrag.pdf",
        metadata={"local_path": "data/raw_papers/graphrag.pdf"},
    )
    hit = RetrievalHit(
        chunk_id="pdf:graphrag:chunk:0",
        paper_id=paper.id,
        title=paper.title,
        text="GraphRAG supports graph retrieval.",
        score=0.8,
        source_tier="primary_fulltext",
        metadata={"source": "pdf", "source_tier": "primary_fulltext"},
    )
    entity = GraphEntity(
        id="concept:graphrag",
        name="GraphRAG",
        type="Concept",
        source_chunk_ids=[hit.chunk_id],
    )
    paper_entity = GraphEntity(
        id="paper:graphrag",
        name=paper.title,
        type="Paper",
        source_chunk_ids=[hit.chunk_id],
    )
    relation = GraphRelation(
        id="rel:discusses",
        source_id=paper_entity.id,
        target_id=entity.id,
        type="DISCUSSES",
        source_chunk_ids=[hit.chunk_id],
    )
    exporter = ObsidianVaultExporter(str(tmp_path / "vault"))

    result = exporter.export(
        run_id="run-test",
        query="GraphRAG research",
        report="# Report\n\n## 核心发现",
        evaluation=_formal_evaluation(),
        papers=[paper],
        retrieval_hits=[hit],
        entities=[paper_entity, entity],
        relations=[relation],
    )
    repeat = exporter.export(
        run_id="run-test",
        query="GraphRAG research",
        report="# Report\n\n## 核心发现",
        evaluation=_formal_evaluation(),
        papers=[paper],
        retrieval_hits=[hit],
        entities=[paper_entity, entity],
        relations=[relation],
    )

    assert result.exported
    assert result.run_note_path == repeat.run_note_path
    assert "[[report--run-test]]" in open(result.run_note_path, encoding="utf-8").read()
    assert open(result.report_note_path, encoding="utf-8").read().startswith("---")
    graph = json.loads(open(result.graph_json_path, encoding="utf-8").read())
    assert len(graph["nodes"]) == 2
    assert open(result.graphml_path, encoding="utf-8").read().startswith("<?xml")


def test_vault_export_refuses_non_admissible_research(tmp_path) -> None:
    evaluation = _formal_evaluation().model_copy(update={"evidence_admissible": False})
    result = ObsidianVaultExporter(str(tmp_path / "vault")).export(
        run_id="run-blocked",
        query="Demo",
        report="# Demo",
        evaluation=evaluation,
        papers=[],
        retrieval_hits=[],
        entities=[],
        relations=[],
    )

    assert not result.exported
    assert not (tmp_path / "vault").exists()


def test_vault_export_refuses_quality_failed_research(tmp_path) -> None:
    evaluation = _formal_evaluation().model_copy(update={"passed": False})
    result = ObsidianVaultExporter(str(tmp_path / "vault")).export(
        run_id="run-quality-failed",
        query="Demo",
        report="# Demo",
        evaluation=evaluation,
        papers=[],
        retrieval_hits=[],
        entities=[],
        relations=[],
    )

    assert not result.exported
    assert not (tmp_path / "vault").exists()
