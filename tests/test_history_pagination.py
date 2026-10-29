import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan


@pytest.fixture
def history(tmp_path):
    repository = KnowledgeRepository(str(tmp_path / "history.db"))
    ingestions = []
    reports = []
    for index in range(105):
        ingestion = repository.create_ingestion(
            topic="历史资料", sources=[f"{index}.pdf"], pdf_max_pages=5, enqueue=False
        )
        ingestions.append(ingestion)
        reports.append(repository.reports.create_report(
            query=f"问题 {index}", topic_slugs=[], top_k=5, report_depth="standard"
        ))
    # Tied timestamps must still yield a deterministic, complete traversal.
    with repository._connect() as connection:
        connection.execute("UPDATE ingestions SET created_at = '2026-01-01'")
        connection.execute("UPDATE reports SET created_at = '2026-01-01'")
    return repository, ingestions, reports


@pytest.mark.parametrize("resource", ["ingestions", "reports"])
def test_history_pages_and_legacy_calls(history, resource):
    repository, ingestions, reports = history
    records = ingestions if resource == "ingestions" else reports
    owner = repository if resource == "ingestions" else repository.reports
    legacy = getattr(owner, f"list_{resource}")
    endpoint = "/api/knowledge/ingestions" if resource == "ingestions" else "/api/reports"
    expected_ids = sorted((record.id for record in records), reverse=True)
    assert len(legacy()) == 100
    assert [item.id for item in legacy(2, 100)] == expected_ids[100:102]
    assert len(legacy(105)) == 105
    with TestClient(create_app(knowledge_repository=repository)) as client:
        response = client.get(endpoint)
        assert response.status_code == 200
        assert isinstance(response.json(), list)
        assert len(response.json()) == 100
        seen = []
        offset = 0
        while offset is not None:
            response = client.get(endpoint, params={"limit": 25, "offset": offset})
            assert response.status_code == 200
            page = response.json()
            assert page["total"] == 105
            assert page["offset"] == offset
            assert page["limit"] == 25
            seen.extend(item["id"] for item in page["items"])
            offset = page["next_offset"]
        assert seen == expected_ids
        assert client.get(f"{endpoint}/{seen[-1]}").json()["id"] == seen[-1]
        assert client.get(endpoint, params={"offset": 100}).json()["items"][0]["id"] == seen[100]
        assert client.get(endpoint, params={"limit": 1}).json()["next_offset"] == 1
        empty = client.get(endpoint, params={"offset": 200}).json()
        assert empty["items"] == []
        assert empty["total"] == 105
        assert empty["next_offset"] is None
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            assert client.get(endpoint, params=params).status_code == 422


def test_statistics_include_old_batches_and_all_candidate_kinds(history):
    repository, ingestions, _ = history
    oldest = min(ingestions, key=lambda item: item.id)
    repository.add_candidate_entity(CandidateEntity(
        id="old-pending", ingestion_id=oldest.id, topic_slug=oldest.topic_slug,
        name="旧候选", type="Method", summary="等待审核的旧候选", confidence=0.9,
        evidence=EvidenceSpan(paper_id="paper", chunk_id="chunk", page_start=1,
                              page_end=1, quote="原文证据"),
    ))
    # Counts use candidate state, including relations, rather than ingestion status.
    with repository._connect() as connection:
        row = connection.execute("SELECT * FROM candidates WHERE id = 'old-pending'").fetchone()
        for candidate_id, kind, status in (
            ("old-relation", "relation", "draft"),
            ("old-merged", "entity", "merged"),
            ("old-rejected", "entity", "rejected"),
        ):
            connection.execute(
                """INSERT INTO candidates
                (id, ingestion_id, kind, status, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, oldest.id, kind, status, row["payload_json"],
                 row["created_at"], row["updated_at"]),
            )
    assert oldest.id not in {item.id for item in repository.list_ingestions()}
    with TestClient(create_app(knowledge_repository=repository)) as client:
        response = client.get("/api/knowledge/ingestion-statistics")
        assert response.status_code == 200
        statistics = response.json()
        assert statistics["total"] == 105
        assert statistics["pending_count"] == 2
        assert statistics["published_count"] == 1
        assert statistics["by_ingestion"][oldest.id] == 2
        collection = statistics["by_collection"][oldest.collection_slug]
        assert collection["pending_count"] == 2
        assert collection["published_count"] == 1
        assert len(collection["sources"]) == 105
        assert len(collection["ingestion_ids"]) == 105
        assert statistics["pending_ingestions"] == [{
            "id": oldest.id, "collection": oldest.collection,
            "collection_slug": oldest.collection_slug,
        }]
        assert client.get("/api/knowledge/ingestions/missing").status_code == 404
