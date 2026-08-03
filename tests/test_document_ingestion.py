from app.agents.document_agent import DocumentAgent
from app.schemas.documents import ParsedDocument
from app.tools.pdf_tools import _infer_title


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
    assert result.papers[0].source_tier == "primary_fulltext"
    assert result.papers[0].metadata["content_kind"] == "pdf_full_text"
    assert len(chunks) > 1
    assert chunks[0].metadata["content_kind"] == "pdf_full_text"
    assert chunks[0].source_tier == "primary_fulltext"


def test_pdf_title_inference_joins_wrapped_heading() -> None:
    title = _infer_title(
        [
            "From Local to Global: A GraphRAG Approach to\n"
            "Query-Focused Summarization\n"
            "Darren Edge1† Ha Trinh1†"
        ]
    )

    assert title == "From Local to Global: A GraphRAG Approach to Query-Focused Summarization"
