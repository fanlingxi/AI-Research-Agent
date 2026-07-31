from app.retrieval.chunking import TextChunker, paper_to_retrieval_text
from app.retrieval.embeddings import HashEmbeddingProvider
from app.retrieval.rag import RagRetriever
from app.retrieval.vector_store import InMemoryVectorStore
from app.schemas.documents import PaperMetadata
from app.tools.search_tools import build_offline_demo_papers


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
