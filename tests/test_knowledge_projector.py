from app.config.settings import Settings
from app.knowledge.projector import Neo4jKnowledgeProjector
from app.knowledge.schemas import EvidenceSpan, PublishedEntity, PublishedRelation


class _Session:
    def __init__(self, calls) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def run(self, query: str, **params) -> None:
        self.calls.append((query, params))


class _Driver:
    def __init__(self) -> None:
        self.calls = []

    def session(self):
        return _Session(self.calls)

    def close(self) -> None:
        return None


def test_neo4j_projector_uses_only_v2_labels_and_controlled_edge(monkeypatch) -> None:
    driver = _Driver()
    monkeypatch.setattr("neo4j.GraphDatabase.driver", lambda *args, **kwargs: driver)
    projector = Neo4jKnowledgeProjector(Settings())
    evidence = EvidenceSpan(
        paper_id="paper:test",
        chunk_id="paper:test:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="A grounded statement from the paper.",
    )
    paper = PublishedEntity(
        id="entity-paper",
        name="Readable Paper",
        type="Paper",
        summary="一篇用于测试正式知识投影的可读论文笔记。",
        evidence=[evidence],
        topic_slugs=["agent"],
    )
    method = PublishedEntity(
        id="entity-method",
        name="Tool Agent",
        type="Method",
        summary="使用外部工具执行多步骤任务的智能体方法。",
        evidence=[evidence],
        topic_slugs=["agent"],
    )
    relation = PublishedRelation(
        id="relation-presents",
        source_entity_id=paper.id,
        target_entity_id=method.id,
        type="PRESENTS",
        summary="论文提出了该工具智能体方法。",
        confidence=0.91,
        evidence=[evidence],
        topic_slugs=["agent"],
    )

    projector.upsert_entities([paper, method])
    projector.upsert_relations([relation])

    queries = "\n".join(query for query, _ in driver.calls)
    assert "KnowledgeEntityV2" in queries
    assert "KnowledgeTopicV2" in queries
    assert "KG_RELATION_V2" in queries
    assert "MATCH (node:KnowledgeEntityV2 {id: $entity_id})" in queries
    assert "ResearchEntity" not in queries
    assert "CO_OCCURS_WITH" not in queries
    assert "RELATED" not in queries
