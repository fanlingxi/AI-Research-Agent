from app.agents.graph_reasoning_agent import GraphReasoningAgent
from app.graphrag.extractor import GraphExtractor
from app.graphrag.store import InMemoryGraphStore
from app.retrieval.chunking import TextChunker, paper_to_retrieval_text
from app.schemas.documents import PaperMetadata, RetrievalHit


def _demo_chunks():
    paper = PaperMetadata(
        id="paper:graphrag",
        title="GraphRAG for scientific literature review",
        abstract=(
            "GraphRAG combines knowledge graph construction, vector search, "
            "entity extraction, relation extraction, and multi-hop reasoning "
            "for citation-aware research reports."
        ),
        source="test",
        year=2026,
    )
    return TextChunker(chunk_size=80, chunk_overlap=10).chunk_paper(
        paper=paper,
        text=paper_to_retrieval_text(paper),
    )


def test_graph_extractor_builds_entities_and_relations() -> None:
    chunks = _demo_chunks()
    result = GraphExtractor(max_entities_per_chunk=8).extract(chunks)

    entity_names = {entity.name for entity in result.entities}
    relation_types = {relation.type for relation in result.relations}

    assert "GraphRAG" in entity_names
    assert "knowledge graph" in entity_names
    assert "DISCUSSES" in relation_types
    assert "CO_OCCURS_WITH" in relation_types


def test_graph_extractor_supports_chinese_research_terms() -> None:
    paper = PaperMetadata(
        id="paper:zh-graphrag",
        title="多智能体科研分析系统的评估方法",
        abstract="系统结合知识图谱、向量检索、实体抽取、关系抽取和多跳推理评估科研报告质量。",
        source="test",
        year=2026,
    )
    chunks = TextChunker(chunk_size=120, chunk_overlap=10).chunk_paper(
        paper=paper,
        text=paper_to_retrieval_text(paper),
    )
    result = GraphExtractor(max_entities_per_chunk=8).extract(chunks)

    entity_names = {entity.name for entity in result.entities}
    relation_types = {relation.type for relation in result.relations}

    assert "知识图谱" in entity_names
    assert "向量检索" in entity_names
    assert "DISCUSSES" in relation_types
    assert "CO_OCCURS_WITH" in relation_types


def test_graph_extractor_filters_capitalized_metadata_noise() -> None:
    paper = PaperMetadata(
        id="paper:noise",
        title="This Evaluation of GraphRAG",
        abstract="This evaluation uses GraphRAG for knowledge graph reasoning on arXiv.",
        source="test",
    )
    chunks = TextChunker(chunk_size=120, chunk_overlap=10).chunk_paper(
        paper=paper,
        text=paper_to_retrieval_text(paper),
    )

    result = GraphExtractor(max_entities_per_chunk=8).extract(chunks)
    normalized_names = [entity.name.lower() for entity in result.entities]

    assert "this" not in normalized_names
    assert "arxiv" not in normalized_names
    assert normalized_names.count("evaluation") == 1


def test_in_memory_graph_store_retrieves_paths() -> None:
    chunks = _demo_chunks()
    graph = GraphExtractor(max_entities_per_chunk=8).extract(chunks)
    store = InMemoryGraphStore()
    store.upsert_graph(entities=graph.entities, relations=graph.relations)

    paths = store.retrieve_paths("GraphRAG knowledge graph reasoning", max_hops=2, limit=3)

    assert paths
    assert paths[0].nodes
    assert paths[0].relations
    assert all(0.0 <= path.score <= 1.0 for path in paths)


def test_graph_reasoning_combines_vector_and_graph_context() -> None:
    chunks = _demo_chunks()
    graph = GraphExtractor(max_entities_per_chunk=8).extract(chunks)
    store = InMemoryGraphStore()
    store.upsert_graph(entities=graph.entities, relations=graph.relations)
    paths = store.retrieve_paths("GraphRAG knowledge graph reasoning", max_hops=2, limit=3)
    hit = RetrievalHit(
        chunk_id=chunks[0].id,
        paper_id=chunks[0].paper_id,
        title=chunks[0].title,
        text=chunks[0].text,
        score=0.9,
    )

    result = GraphReasoningAgent().reason(
        query="GraphRAG knowledge graph reasoning",
        vector_hits=[hit],
        graph_paths=paths,
    )

    assert "GraphRAG 推理摘要" in result.answer
    assert result.metadata["vector_hits"] == 1
    assert result.metadata["graph_paths"] >= 1
