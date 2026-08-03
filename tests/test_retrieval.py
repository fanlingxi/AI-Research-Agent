from app.retrieval.chunking import TextChunker, paper_to_retrieval_text
from app.retrieval.embeddings import HashEmbeddingProvider, cosine_similarity
from app.retrieval.rag import RagRetriever
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
