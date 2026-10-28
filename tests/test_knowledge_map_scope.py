import json

import pytest

from app.knowledge.map_view import KnowledgeMapReader
from app.knowledge.repository import KnowledgeRepository


@pytest.mark.parametrize("legacy_id", [None, "legacy:shared"])
@pytest.mark.parametrize("chunk", [False, True], ids=["location-only", "chunk"])
@pytest.mark.parametrize("outside", ["a", "b-other"])
def test_entity_evidence_is_scoped_to_membership_ingestion_documents(
    tmp_path, legacy_id, chunk, outside
):
    repository = KnowledgeRepository(str(tmp_path / "map.db"))
    node_id = legacy_id or "shared"
    with repository.database.connect() as db:
        for collection in ("a", "b"):
            db.execute(
                "INSERT INTO knowledge_collections VALUES (?, ?, 0, 't', 't')",
                (collection, collection),
            )
        for ingestion in ("a", "b", "b-other"):
            db.execute(
                "INSERT INTO ingestions (id, topic, topic_slug, sources_json, pdf_max_pages,"
                " status, created_at, updated_at)"
                " VALUES (?, ?, ?, '[]', 1, 'completed', 't', 't')",
                (ingestion, ingestion, ingestion),
            )
            db.execute(
                "INSERT INTO ingestion_collections VALUES (?, ?)", (ingestion, ingestion[0])
            )
            db.execute(
                "INSERT INTO sources (id, source_type, title, uri, created_at, updated_at)"
                " VALUES (?, 'pdf', ?, ?, 't', 't')",
                (ingestion, ingestion, f"uri:{ingestion}"),
            )
            db.execute(
                "INSERT INTO documents (id, ingestion_id, title, source, pages, metadata_json,"
                " source_id) VALUES (?, ?, ?, 'pdf', 1, '{}', ?)",
                (ingestion, ingestion, ingestion, ingestion),
            )
            db.execute(
                "INSERT INTO ingestion_documents VALUES (?, ?, 't')", (ingestion, ingestion)
            )
            db.execute(
                "INSERT INTO chunks (id, document_id, content, content_sha256, chunk_index,"
                " page_start, page_end, created_at, updated_at)"
                " VALUES (?, ?, 'quote', ?, 0, 1, 1, 't', 't')",
                (ingestion, ingestion, ingestion),
            )
        db.execute(
            "INSERT INTO entities (id, legacy_id, name, normalized_name, entity_type,"
            " properties_json, status, created_at, updated_at)"
            " VALUES ('shared', ?, 'Shared', 'shared', 'Concept', '{}', 'published', 't', 't')",
            (legacy_id,),
        )
        # The entity belongs to both collections, but only these two ingestions.
        for collection in ("a", "b"):
            db.execute(
                "INSERT INTO collection_memberships VALUES ('entity', ?, ?, ?, 't')",
                (node_id, collection, collection),
            )

    def add_evidence(evidence_id, document, *, use_chunk):
        with repository.database.connect() as db:
            db.execute(
                "INSERT INTO claims (id, entity_id, claim_type, statement, status, created_at,"
                " updated_at) VALUES (?, 'shared', 'definition', 'Shared', 'published', 't', 't')",
                (evidence_id,),
            )
            db.execute(
                "INSERT INTO evidences (id, source_id, chunk_id, quote, quote_sha256,"
                " location_json, created_at) VALUES (?, ?, ?, 'quote', ?, ?, 't')",
                (
                    evidence_id,
                    document,
                    document if use_chunk else None,
                    evidence_id,
                    json.dumps({"paper_id": document}),
                ),
            )
            db.execute(
                "INSERT INTO claim_evidence_links VALUES (?, ?, 'supports', 't')",
                (evidence_id, evidence_id),
            )

    # Exercise both document paths without combining location-only evidences
    # into the legacy query's single NULL chunk-document group.
    add_evidence("inside", "b", use_chunk=not chunk)
    reader = KnowledgeMapReader(repository.database)
    before = reader.graph(collection="b").model_dump()
    assert {n["id"] for n in before["nodes"]} == {node_id, "source:b"}
    scoped_node = next(n for n in before["nodes"] if n["id"] == node_id)
    assert scoped_node["evidence_count"] == scoped_node["paper_count"] == 1

    # Even another ingestion in collection b is outside this entity's membership.
    add_evidence("outside", outside, use_chunk=chunk)
    assert reader.graph(collection="b").model_dump() == before
    global_node = next(n for n in reader.graph().nodes if n.id == node_id)
    assert global_node.evidence_count == global_node.paper_count == 2
    if outside == "a":
        other = reader.graph(collection="a")
        assert {n.id for n in other.nodes} == {node_id, "source:a"}
        assert next(n for n in other.nodes if n.id == node_id).evidence_count == 1

    with repository.database.connect() as db:
        db.execute("DELETE FROM ingestion_documents WHERE ingestion_id = 'b'")
    empty = reader.graph(collection="b")
    assert empty.nodes == [] and empty.edges == []
    assert empty.stats.total_nodes == empty.stats.total_edges == 0
