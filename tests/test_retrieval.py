from app.agents.reasoning_agent import ReasoningAgent
from app.retrieval.chunking import TextChunker, paper_to_retrieval_text
from app.retrieval.embeddings import HashEmbeddingProvider, cosine_similarity
from app.retrieval.rag import RagRetriever
from app.retrieval.relevance import expand_query_for_retrieval
from app.retrieval.vector_store import InMemoryVectorStore
from app.schemas.documents import PaperMetadata
from app.tools.search_tools import build_offline_demo_papers, build_search_queries, rank_papers


def test_chunker_creates_retrieval_chunks() -> None:
    paper = PaperMetadata(
        id="paper:1",
        title="GraphRAG Test Paper",
        abstract="Graph retrieval improves multi hop reasoning. " * 80,
    )
    chunker = TextChunker(chunk_size=40, chunk_overlap=10)

    chunks = chunker.chunk_paper(paper=paper, text=paper_to_retrieval_text(paper))

    assert chunks
    assert chunks[0].paper_id == "paper:1"
    assert chunks[0].token_count <= 40


def test_hash_embedding_retrieval_prefers_related_chunk() -> None:
    papers = build_offline_demo_papers("GraphRAG", limit=3)
    chunker = TextChunker(chunk_size=80, chunk_overlap=10)
    chunks = []
    for paper in papers:
        chunks.extend(chunker.chunk_paper(paper=paper, text=paper_to_retrieval_text(paper)))

    retriever = RagRetriever(
        embedding_provider=HashEmbeddingProvider(dimension=128),
        vector_store=InMemoryVectorStore(),
    )
    retriever.index(chunks)
    hits = retriever.search("knowledge graph entities and relationships", top_k=2)

    assert len(hits) == 2
    assert hits[0].score >= hits[1].score


def test_search_query_planning_and_reranking_keep_graphrag_on_topic() -> None:
    query = "How does GraphRAG improve query-focused summarization for literature reviews?"
    queries = build_search_queries(query)
    papers = [
        PaperMetadata(
            id="paper:qfs",
            title="Few-shot Query-Focused Summarization",
            abstract="A query-focused summarization method.",
        ),
        PaperMetadata(
            id="paper:graphrag",
            title="From Local to Global: A Graph RAG Approach to Query-Focused Summarization",
            abstract="GraphRAG builds a graph index for query-focused summarization.",
        ),
    ]

    ranked = rank_papers(query, papers)

    assert any("graphrag" in item.lower() for item in queries)
    assert ranked[0].id == "paper:graphrag"


def test_hash_embedding_supports_chinese_text() -> None:
    embedding = HashEmbeddingProvider(dimension=128)
    query_vector = embedding.embed_query("知识图谱用于多跳推理")
    related_vector = embedding.embed_query("知识图谱支持科研分析中的多跳推理")

    assert any(query_vector)
    assert cosine_similarity(query_vector, related_vector) > 0


def test_cross_lingual_query_expansion_shares_english_concepts() -> None:
    embedding = HashEmbeddingProvider(dimension=128)
    expanded_query = expand_query_for_retrieval("比较多智能体协作、长期记忆与智能体评估方法")
    paper_vector = embedding.embed_query(
        "Multi-agent collaboration with long-term memory and agent evaluation"
    )

    assert "multi-agent" in expanded_query
    assert "long-term memory" in expanded_query
    assert cosine_similarity(embedding.embed_query(expanded_query), paper_vector) > 0


def test_reranking_prefers_literature_review_over_generic_graphrag() -> None:
    query = "GraphRAG 如何提升科研文献综述的证据整合与多跳推理能力？"
    papers = [
        PaperMetadata(
            id="paper:plasma",
            title="Plasma GraphRAG for Simulations",
            abstract="GraphRAG improves parameter selection for plasma simulations.",
        ),
        PaperMetadata(
            id="paper:review",
            title="GraphRAG for Scientific Literature Review",
            abstract="GraphRAG supports evidence integration and multi-hop reasoning.",
        ),
    ]

    assert rank_papers(query, papers)[0].id == "paper:review"


def test_reasoning_agent_reports_qdrant_fallback(monkeypatch) -> None:
    paper = PaperMetadata(
        id="paper:fallback",
        title="GraphRAG Fallback",
        abstract="GraphRAG knowledge graph retrieval.",
        source_tier="online_metadata",
    )
    chunks = TextChunker(chunk_size=20, chunk_overlap=2).chunk_paper(
        paper,
        paper_to_retrieval_text(paper),
    )

    class BrokenRetriever:
        vector_store = type("Store", (), {"provider_name": "qdrant"})()

        def index(self, _chunks) -> None:
            raise RuntimeError("qdrant unavailable")

    def fake_retriever(vector_store_provider=None):
        if vector_store_provider == "qdrant":
            return BrokenRetriever()
        return RagRetriever(
            embedding_provider=HashEmbeddingProvider(dimension=128),
            vector_store=InMemoryVectorStore(),
        )

    monkeypatch.setattr("app.agents.reasoning_agent.build_rag_retriever", fake_retriever)
    result = ReasoningAgent().retrieve_and_answer(
        query="GraphRAG retrieval",
        chunks=chunks,
        vector_store_provider="qdrant",
    )

    assert result.vector_store_provider == "memory"
    assert "qdrant unavailable" in (result.fallback_reason or "")
    assert result.hits
