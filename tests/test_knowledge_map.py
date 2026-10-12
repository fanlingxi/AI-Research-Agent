import sqlite3

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.extractor import ExtractionResult
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import (
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
from app.worker import KnowledgeWorker

QUOTE = "Agents use tools to solve research tasks with evidence."


class _MapExtractor:
    def extract(self, paper, chunks):
        evidence = EvidenceSpan(
            paper_id=paper.id, chunk_id=chunks[0].id, page_start=1, page_end=1, quote=QUOTE
        )

        def entity(name: str, entity_type: str) -> ExtractedEntity:
            return ExtractedEntity(
                name=name,
                type=entity_type,
                summary=f"{name}是用于地图测试的知识实体说明。",
                confidence=0.9,
                evidence=evidence,
            )

        return ExtractionResult(
            KnowledgeExtraction(
                reading=PaperReading(
                    research_problem="研究智能体如何基于证据调用工具完成复杂任务。",
                    core_contributions=["提供可审核知识抽取流程。"],
                    method_summary="通过工具调用和证据记录完成多步任务。",
                    evidence=evidence,
                ),
                entities=[
                    entity("证据驱动智能体", "Method"),
                    entity("工具调用", "Concept"),
                    entity("研究任务集", "Dataset"),
                ],
                relations=[
                    ExtractedRelation(
                        source_name="证据驱动智能体",
                        target_name="工具调用",
                        type="USES",
                        summary="证据驱动智能体依赖工具调用获取证据。",
                        confidence=0.9,
                        evidence=evidence,
                    )
                ],
            )
        )


def _parser(*, source: str, max_pages: int) -> ParsedDocument:
    return ParsedDocument(
        source=source,
        title="Map Fixture Paper",
        text=QUOTE,
        pages=1,
        page_texts=[QUOTE],
        page_numbers=[1],
    )


def _build(tmp_path):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
        chunk_size=40,
        chunk_overlap=10,
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    service = KnowledgeIngestionService(
        repository,
        settings=settings,
        extractor=_MapExtractor(),
        parser=_parser,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(settings.knowledge_vault_path),
        require_live_llm=False,
    )
    reports = KnowledgeReportService(repository, settings=settings, require_live_llm=False)
    worker = KnowledgeWorker(
        repository, ingestion_service=service, report_service=reports, lease_seconds=30
    )
    app = create_app(
        knowledge_repository=repository, knowledge_service=service, report_service=reports
    )
    return app, repository, worker


def _seed(client: TestClient, worker: KnowledgeWorker) -> dict[str, str]:
    """Publish the paper, two entities and one relation; leave one entity as a draft."""

    ingestion_id = client.post(
        "/api/knowledge/ingestions",
        json={"topic": "智能体地图", "sources": ["map-fixture.pdf"], "pdf_max_pages": 1},
    ).json()["id"]
    assert worker.run_once()
    candidates = client.get(f"/api/knowledge/ingestions/{ingestion_id}/candidates").json()
    by_name = {
        item["candidate"]["name"]: item["candidate"]["id"]
        for item in candidates
        if item["kind"] == "entity"
    }
    for name in ("证据驱动智能体", "工具调用", "Map Fixture Paper"):
        response = client.post(
            f"/api/knowledge/candidates/{by_name[name]}/decision", json={"decision": "approve"}
        )
        assert response.status_code == 200, response.text
    uses = next(
        item["candidate"]["id"]
        for item in candidates
        if item["kind"] == "relation" and item["candidate"]["type"] == "USES"
    )
    assert (
        client.post(
            f"/api/knowledge/candidates/{uses}/decision", json={"decision": "approve"}
        ).status_code
        == 200
    )
    while worker.run_once():
        pass
    return {"ingestion_id": ingestion_id, "draft_entity": by_name["研究任务集"]}


def _candidate_states(path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT id, status FROM candidates ORDER BY id").fetchall()


def test_knowledge_map_is_read_only_and_approved_only_by_default(tmp_path) -> None:
    app, repository, worker = _build(tmp_path)
    with TestClient(app) as client:
        seeded = _seed(client, worker)
        before = _candidate_states(repository.path)

        default = client.get("/api/knowledge/map").json()
        pending = client.get("/api/knowledge/map?include_pending=true").json()
        after = _candidate_states(repository.path)

    labels = {node["label"]: node for node in default["nodes"]}
    assert set(labels) == {"Map Fixture Paper", "证据驱动智能体", "工具调用"}
    assert all(node["status"] == "published" for node in default["nodes"])
    assert seeded["draft_entity"] not in {node["id"] for node in default["nodes"]}
    assert default["stats"]["pending_nodes"] == 0
    assert default["type_counts"] == {"Concept": 1, "Method": 1, "Paper": 1}

    relation = next(edge for edge in default["edges"] if edge["kind"] == "relation")
    assert relation["type"] == "USES"
    assert relation["source"] == labels["证据驱动智能体"]["id"]
    assert relation["target"] == labels["工具调用"]["id"]
    evidence_edges = [edge for edge in default["edges"] if edge["kind"] == "evidence"]
    assert {edge["source"] for edge in evidence_edges} == {labels["Map Fixture Paper"]["id"]}
    assert all(edge["status"] == "published" for edge in default["edges"])

    pending_node = next(node for node in pending["nodes"] if node["id"] == seeded["draft_entity"])
    assert pending_node["status"] == "pending"
    assert pending_node["label"] == "研究任务集"
    assert pending["stats"]["pending_nodes"] == 1
    assert pending["stats"]["pending_edges"] >= 1
    assert after == before


def test_knowledge_map_filters_and_limit(tmp_path) -> None:
    app, repository, worker = _build(tmp_path)
    with TestClient(app) as client:
        _seed(client, worker)
        collection = next(item.slug for item in repository.list_collections() if not item.is_system)
        methods = client.get("/api/knowledge/map?entity_type=Method").json()
        multi = client.get("/api/knowledge/map?entity_type=Method&entity_type=Concept").json()
        searched = client.get("/api/knowledge/map", params={"q": "工具"}).json()
        limited = client.get("/api/knowledge/map?limit=1").json()
        scoped = client.get("/api/knowledge/map", params={"collection_slug": collection}).json()
        other = client.get("/api/knowledge/map?collection_slug=inbox").json()
        invalid = client.get("/api/knowledge/map?limit=0")

    assert [node["type"] for node in methods["nodes"]] == ["Method"]
    assert methods["type_counts"]["Paper"] == 1  # legend counts ignore the type filter
    assert {node["type"] for node in multi["nodes"]} == {"Method", "Concept"}
    assert [edge["type"] for edge in multi["edges"]] == ["USES"]

    matched = [node["label"] for node in searched["nodes"] if node["matched"]]
    assert matched == ["工具调用"]
    # Matches keep their direct neighbours for context.
    assert {node["label"] for node in searched["nodes"]} >= {"工具调用", "证据驱动智能体"}

    assert limited["stats"]["shown_nodes"] == 1
    assert limited["stats"]["total_nodes"] == 3
    assert limited["stats"]["truncated"] is True
    assert limited["edges"] == []
    assert limited["nodes"][0]["score"] == max(node["score"] for node in scoped["nodes"])

    assert len(scoped["nodes"]) == 3
    assert other["nodes"] == [] and other["edges"] == []
    assert invalid.status_code == 422


def test_knowledge_map_node_detail_drills_down_to_evidence(tmp_path) -> None:
    app, _, worker = _build(tmp_path)
    with TestClient(app) as client:
        seeded = _seed(client, worker)
        nodes = client.get("/api/knowledge/map").json()["nodes"]
        method_id = next(node["id"] for node in nodes if node["label"] == "证据驱动智能体")
        paper_id = next(node["id"] for node in nodes if node["type"] == "Paper")

        detail = client.get(f"/api/knowledge/map/nodes/{method_id}").json()
        hidden_draft = client.get(f"/api/knowledge/map/nodes/{seeded['draft_entity']}")
        draft = client.get(
            f"/api/knowledge/map/nodes/{seeded['draft_entity']}?include_pending=true"
        ).json()
        missing = client.get("/api/knowledge/map/nodes/entity-missing")

    assert detail["node"]["status"] == "published"
    assert detail["node"]["summary"].startswith("证据驱动智能体")
    evidence = detail["claims"][0]["evidence"][0]
    assert evidence["quote"] == QUOTE
    assert evidence["page_start"] == 1
    assert evidence["paper_title"] == "Map Fixture Paper"
    assert evidence["paper_node_id"] == paper_id
    uses = next(item for item in detail["relations"] if item["type"] == "USES")
    assert uses["direction"] == "out" and uses["other"]["label"] == "工具调用"
    assert all(item["status"] == "published" for item in detail["relations"])
    assert detail["papers"][0]["node_id"] == paper_id

    assert hidden_draft.status_code == 404
    assert draft["node"]["status"] == "pending"
    assert draft["review"]["candidate_id"] == seeded["draft_entity"]
    assert draft["claims"][0]["evidence"][0]["paper_title"] == "Map Fixture Paper"
    assert missing.status_code == 404
