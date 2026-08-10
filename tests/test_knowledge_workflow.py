import json
from pathlib import Path

from app.config.settings import Settings
from app.knowledge.extractor import ExtractionResult
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import (
    CandidateDecision,
    CandidateEntity,
    EvidenceSpan,
    ExtractedEntity,
    ExtractedRelation,
    KnowledgeExtraction,
    PaperReading,
)
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.schemas.documents import ParsedDocument
from tests.core_fixtures import persist_evidence_chunk


class FixtureExtractor:
    def extract(self, paper, chunks):
        evidence = EvidenceSpan(
            paper_id=paper.id,
            chunk_id=chunks[0].id,
            page_start=1,
            page_end=1,
            quote="The method lets an agent use tools to plan and solve research tasks.",
        )
        return ExtractionResult(
            payload=KnowledgeExtraction(
                reading=PaperReading(
                    research_problem="研究如何让智能体可靠使用工具完成复杂研究任务。",
                    core_contributions=["提出面向工具调用的可审阅研究流程。"],
                    method_summary="智能体先规划，再选择外部工具，并依据返回结果修正下一步。",
                    key_results=["实验显示工具使用可以覆盖更多研究步骤。"],
                    limitations=["效果受工具质量、成本和上下文窗口限制。"],
                    evidence=evidence,
                ),
                entities=[
                    ExtractedEntity(
                        name="工具增强智能体",
                        type="Method",
                        summary="通过调用外部工具获取信息并完成多步研究任务的智能体方法。",
                        aliases=["Tool-augmented Agent"],
                        confidence=0.91,
                        evidence=evidence,
                    ),
                    ExtractedEntity(
                        name="科研任务自动化",
                        type="Task",
                        summary="使用智能体完成检索、规划、执行和验证等科研工作步骤的任务。",
                        aliases=[],
                        confidence=0.84,
                        evidence=evidence,
                    ),
                ],
                relations=[
                    ExtractedRelation(
                        source_name="工具增强智能体",
                        target_name="科研任务自动化",
                        type="APPLIES_TO",
                        summary="工具增强智能体被用于分解并执行科研任务自动化流程。",
                        confidence=0.83,
                        evidence=evidence,
                    )
                ],
            )
        )


def _parser(*, source: str, max_pages: int) -> ParsedDocument:
    assert max_pages == 4
    return ParsedDocument(
        source=source,
        title="Readable Agent Paper",
        text=("The method lets an agent use tools to plan and solve research tasks. " * 4),
        pages=2,
        page_texts=[
            "The method lets an agent use tools to plan and solve research tasks. " * 3,
            "The evaluation measures reliable tool use for complex research workflows. " * 3,
        ],
        page_numbers=[1, 2],
    )


def _service(tmp_path: Path) -> tuple[KnowledgeRepository, KnowledgeIngestionService, Path]:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    vault = tmp_path / "obsidian_vault_v2"
    service = KnowledgeIngestionService(
        repository,
        settings=Settings(chunk_size=40, chunk_overlap=10),
        extractor=FixtureExtractor(),
        parser=_parser,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(str(vault)),
        require_live_llm=False,
    )
    return repository, service, vault


def test_reviewable_workflow_publishes_readable_vault_only_after_approval(tmp_path) -> None:
    repository, service, vault = _service(tmp_path)
    ingestion = service.submit(
        topic="智能体工具使用",
        sources=["fixture-agent-paper.pdf"],
        pdf_max_pages=4,
    )

    pending = service.run(ingestion.id)

    assert pending.status == "needs_review"
    assert pending.document_count == 1
    assert not vault.exists()
    drafts = repository.list_candidates(ingestion.id, status="draft")
    assert drafts
    assert all(item["candidate"]["evidence"]["page_start"] == 1 for item in drafts)
    assert all(item["candidate"]["evidence"]["quote"] for item in drafts)

    approved = service.approve_ready(ingestion.id)

    assert approved.ingestion.status == "needs_review"
    assert approved.published_entities == 3
    assert approved.published_relations == 0
    assert approved.blocked_relations == 3
    for item in repository.list_candidates(ingestion.id, status="draft"):
        if item["kind"] == "relation":
            service.decide(item["candidate"]["id"], CandidateDecision(decision="approve"))
    assert repository.get_ingestion(ingestion.id).status == "publishing"
    assert not vault.exists()
    assert service.drain_projections() == 6
    assert repository.get_ingestion(ingestion.id).status == "completed"
    paper_path = vault / "10_Papers" / "Readable Agent Paper.md"
    canvas_path = vault / "80_Canvases" / "智能体工具使用.canvas"
    assert paper_path.exists()
    paper_content = paper_path.read_text(encoding="utf-8")
    assert "## 研究问题" in paper_content
    assert "## 人工笔记" in paper_content
    assert "CO_OCCURS_WITH" not in paper_content
    assert not (vault / "30_Claims").exists()
    canvas = json.loads(canvas_path.read_text(encoding="utf-8"))
    assert len([node for node in canvas["nodes"] if node["type"] == "file"]) <= 18
    assert len(canvas["edges"]) <= 18
    assert canvas["edges"]
    assert canvas["metadata"]["layout"] == "reading-flow"
    assert all(
        edge["fromSide"] in {"top", "right", "bottom", "left"}
        and edge["toSide"] in {"top", "right", "bottom", "left"}
        and "label" not in edge
        for edge in canvas["edges"]
    )
    graph_config = json.loads((vault / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
    assert graph_config["search"] == '-path:"80_Canvases"'
    assert {item["query"] for item in graph_config["colorGroups"]} >= {
        'path:"10_Papers"',
        'path:"20_Concepts"',
        'path:"30_Methods"',
    }

    paper_path.write_text(
        paper_content.replace("在这里记录你的阅读、判断和待验证问题。", "我的人工判断。"),
        encoding="utf-8",
    )
    service._render_topics([ingestion.topic_slug])
    assert "我的人工判断。" in paper_path.read_text(encoding="utf-8")


def test_repository_persists_history_and_marks_running_work_interrupted(tmp_path) -> None:
    repository, service, _ = _service(tmp_path)
    ingestion = service.submit(
        topic="持久化审核任务",
        sources=["fixture-agent-paper.pdf"],
        pdf_max_pages=4,
    )
    repository.update_ingestion(ingestion.id, status="running")

    reopened = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    reopened.mark_interrupted()
    recovered = reopened.get_ingestion(ingestion.id)

    assert recovered.status == "interrupted"
    assert "重试" in (recovered.error or "")


def test_repository_requires_an_explicit_entity_merge_and_preserves_aliases(tmp_path) -> None:
    repository, service, _ = _service(tmp_path)
    ingestion = service.submit(
        topic="实体消歧审核",
        sources=["fixture-agent-paper.pdf"],
        pdf_max_pages=4,
    )
    evidence = EvidenceSpan(
        paper_id="paper:fixture",
        chunk_id="paper:fixture:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="The method lets an agent use tools to plan and solve research tasks.",
    )
    first = CandidateEntity(
        id="candidate-first",
        ingestion_id=ingestion.id,
        topic_slug=ingestion.topic_slug,
        name="GraphRAG",
        type="Method",
        summary="结合图结构与检索证据完成回答生成的方法。",
        aliases=["Graph RAG"],
        confidence=0.9,
        evidence=evidence,
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    repository.add_candidate_entity(first)
    canonical = repository.publish_entity(first.id)
    suggestions = repository.find_merge_suggestions(
        name="Graph RAG",
        entity_type="Method",
        aliases=[],
    )
    second = first.model_copy(
        update={
            "id": "candidate-second",
            "name": "Graph RAG",
            "aliases": ["GraphRAG retrieval"],
            "merge_suggestions": suggestions,
        }
    )
    repository.add_candidate_entity(second)

    merged = repository.publish_entity(second.id, canonical_id=canonical.id)
    reviewed = repository.get_candidate(second.id)

    assert suggestions[0].entity_id == canonical.id
    assert suggestions[0].match_kind == "exact"
    assert merged.id == canonical.id
    assert "Graph RAG" in merged.aliases
    assert reviewed["candidate"]["status"] == "merged"


def test_collection_migration_backfills_legacy_relation_memberships(tmp_path) -> None:
    repository, service, _ = _service(tmp_path)
    ingestion = service.submit(
        topic="旧关系迁移", sources=["fixture-agent-paper.pdf"], pdf_max_pages=4
    )
    service.run(ingestion.id)
    service.approve_ready(ingestion.id)
    for item in repository.list_candidates(ingestion.id, status="draft"):
        if item["kind"] == "relation":
            service.decide(item["candidate"]["id"], CandidateDecision(decision="approve"))
    relation_ids = {
        item.id for item in repository.list_published_relations(ingestion.topic_slug)
    }

    with repository._connect() as connection:
        connection.execute(
            "DELETE FROM collection_memberships WHERE aggregate_type = 'relation'"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")

    migrated = KnowledgeRepository(str(tmp_path / "knowledge.db"))

    assert {
        item.id for item in migrated.list_published_relations(ingestion.topic_slug)
    } == relation_ids


def test_ten_pdf_batch_creates_ten_readable_paper_notes_without_offline_demo(tmp_path) -> None:
    repository, service, vault = _service(tmp_path)

    def batch_parser(*, source: str, max_pages: int) -> ParsedDocument:
        parsed = _parser(source=source, max_pages=max_pages)
        return parsed.model_copy(update={"title": f"Agent Paper {Path(source).stem}"})

    service.parser = batch_parser
    ingestion = service.submit(
        topic="十篇智能体论文",
        sources=[f"agent-{index:02d}.pdf" for index in range(10)],
        pdf_max_pages=4,
    )
    assert service.run(ingestion.id).status == "needs_review"

    bulk_result = service.approve_ready(ingestion.id)
    assert bulk_result.ingestion.status == "needs_review"
    assert any(
        item["candidate"]["merge_suggestions"]
        for item in repository.list_candidates(ingestion.id, status="draft")
        if item["kind"] == "entity"
    )

    for item in repository.list_candidates(ingestion.id, status="draft"):
        candidate = item["candidate"]
        if item["kind"] == "entity" and candidate["type"] == "Paper":
            service.decide(candidate["id"], CandidateDecision(decision="approve"))
    for item in repository.list_candidates(ingestion.id, status="draft"):
        if item["kind"] != "entity":
            continue
        candidate = item["candidate"]
        try:
            service.decide(candidate["id"], CandidateDecision(decision="approve"))
        except ValueError:
            refreshed = repository.get_candidate(candidate["id"])["candidate"]
            target = refreshed["merge_suggestions"][0]["entity_id"]
            service.decide(
                candidate["id"],
                CandidateDecision(decision="merge", canonical_id=target),
            )
    service.approve_ready(ingestion.id)
    service.drain_projections()

    published_papers = [
        entity
        for entity in repository.list_published_entities(ingestion.topic_slug)
        if entity.type == "Paper"
    ]
    paper_notes = list((vault / "10_Papers").glob("*.md"))

    assert len(published_papers) == 10
    assert len(paper_notes) == 10
    assert all("offline_demo" not in note.read_text(encoding="utf-8") for note in paper_notes)
