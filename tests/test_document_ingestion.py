from app.agents.document_agent import DocumentAgent
from app.schemas.documents import ParsedDocument


def test_document_agent_ingests_explicit_pdf_sources(monkeypatch) -> None:
    def fake_parse_pdf_source(source: str, max_pages: int | None = None) -> ParsedDocument:
        assert source == "https://example.com/graphrag.pdf"
        assert max_pages == 3
        return ParsedDocument(
            source="data/raw_papers/graphrag.pdf",
            title="GraphRAG Test Paper",
            text="GraphRAG knowledge graph reasoning " * 60,
            pages=3,
            metadata={"original_source": source},
        )

    monkeypatch.setattr("app.agents.document_agent.parse_pdf_source", fake_parse_pdf_source)
    agent = DocumentAgent()

    result = agent.ingest_pdf_sources(
        ["https://example.com/graphrag.pdf"],
        max_pages=3,
    )
    chunks = agent.build_chunks(result.papers, chunk_size=40, chunk_overlap=10)

    assert not result.errors
    assert result.papers[0].source == "pdf"
    assert result.papers[0].metadata["content_kind"] == "pdf_full_text"
    assert len(chunks) > 1
    assert chunks[0].metadata["content_kind"] == "pdf_full_text"
