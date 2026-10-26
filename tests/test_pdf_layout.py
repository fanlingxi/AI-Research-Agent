import hashlib
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.knowledge.paragraph_chunks import paragraph_chunks
from app.knowledge.service import KnowledgeIngestionService
from app.retrieval.anchors import locate_quote
from app.schemas.documents import PaperMetadata, ParsedDocument
from app.tools.pdf_layout import TextRun, clean_body_line, derive_document_blocks
from app.tools.pdf_tools import parse_pdf_source


def _layout(lines, *, xs=None, ys=None, sizes=None):
    runs = [TextRun(t, (xs or [60] * len(lines))[i],
                    (ys or [700 - 12 * j for j in range(len(lines))])[i],
                    (sizes or [10] * len(lines))[i]) for i, t in enumerate(lines)]
    return runs, 800


def _assert_anchors(blocks, pages):
    for block in blocks:
        raw = "\n".join(pages[s.page - 1][s.start:s.end] for s in block.spans)
        assert locate_quote(block.text, raw) is not None


def test_chinese_cleanup_preserves_mixed_boundaries_and_structured_lines():
    assert clean_body_line("中 文 ， 测 试 BERT 模 型 2024 年 。") == (
        "中文，测试 BERT 模型 2024 年。"
    )
    for value in ["数 据  12  24  36", "变 量 x = y + z", "def 测 试(x):", "中 文\t甲 乙\t丙 丁"]:
        assert clean_body_line(value) == value
    raw = "  数 据\t12\t24\t36  "
    block = derive_document_blocks([raw])[0]
    assert block.kind == "other" and block.text == raw


@pytest.mark.parametrize("chinese", [True, False])
def test_front_matter_abstract_keywords_and_raw_offsets(chinese):
    lines = (["中文检索研究", "张三¹，李四*", "北京大学，计算机学院", "a@pku.edu.cn", "摘 要",
              "本 文 提 出 一 种", "新 的 方 法 。", "关键词：检索", "1 引言", "正 文 内 容 。"]
             if chinese else
             ["Retrieval Study", "John Doe¹, Jane Smith*", "Example University", "a@uni.edu",
              "Abstract", "We propose a new", "method for retrieval.", "Keywords: retrieval",
              "1 Introduction", "Body text."])
    raw = "\n".join(lines)
    blocks = derive_document_blocks([raw], layouts={1: _layout(
        lines, sizes=[18, 12, 10, 10, 12, 10, 10, 10, 12, 10],
    )})
    assert [b.kind for b in blocks[:4]] == ["front_matter"] * 4
    assert any(b.kind == "keywords" for b in blocks)
    assert any(b.kind == "heading" for b in blocks)
    assert any(b.text == ("本文提出一种新的方法。" if chinese else
                          "We propose a new method for retrieval.") for b in blocks)
    _assert_anchors(blocks, [raw])


def test_repeated_margins_references_and_appendix():
    lines = ["计算语言学 2024", "正文 内 容。", "参考文献", "[1] Zhang. 2024. A paper.", "21"]
    next_lines = ["计算语言学 2025", "[2] Li. 2023. Another paper.", "附录 A", "附录 正文。", "22"]
    pages = ["\n".join(lines), "\n".join(next_lines)]
    blocks = derive_document_blocks(pages, layouts={
        1: _layout(lines, ys=[770, 650, 620, 600, 30]),
        2: _layout(next_lines, ys=[770, 700, 650, 620, 30]),
    })
    assert [b.kind for b in blocks].count("header") == 2
    assert [b.kind for b in blocks].count("footer") == 2
    assert [b.kind for b in blocks].count("references") == 3
    assert next(b for b in blocks if b.text == "附录正文。").kind == "paragraph"
    _assert_anchors(blocks, pages)


def test_uncertain_reference_and_coordinate_free_body_are_retained():
    raw = "正文 大学研究发现了新的现象。\nReferences\nThis discusses references in ordinary prose."
    blocks = derive_document_blocks([raw])
    assert all(b.kind == "paragraph" for b in blocks)
    assert len(blocks) == 3
    assert derive_document_blocks(["A recent focus of research."])[0].kind == "paragraph"
    blocks = derive_document_blocks(["The University study finds a result.\nAbstract\nSummary."])
    assert blocks[0].kind == "paragraph"


def test_paragraph_indentation_columns_and_hyphens():
    lines = ["We study re-", "trieval methods.", "New paragraph starts.", "Other column."]
    pages = ["\n".join(lines)]
    blocks = derive_document_blocks(pages, layouts={1: _layout(lines, xs=[60, 60, 80, 310])})
    assert [b.text for b in blocks] == ["We study re- trieval methods.", *lines[2:]]
    assert locate_quote("We study retrieval methods.", pages[0]) is not None
    _assert_anchors(blocks, pages)


def test_cross_page_continuation_and_chunk_page_range():
    pages = ["We propose a new", "method for retrieval."]
    blocks = derive_document_blocks(pages, layouts={
        1: _layout([pages[0]], ys=[110]), 2: _layout([pages[1]], ys=[690]),
    })
    assert len(blocks) == 1 and len(blocks[0].spans) == 2
    parsed = ParsedDocument(source="x.pdf", text="\n\n".join(pages),
                            page_texts=pages, blocks=blocks)
    chunks = paragraph_chunks(PaperMetadata(id="p", title="t"), parsed)
    assert chunks[0].metadata["page_start"] == 1 and chunks[0].metadata["page_end"] == 2
    _assert_anchors(blocks, pages)
    pages[0] += "."
    blocks = derive_document_blocks(pages, layouts={
        1: _layout([pages[0]], ys=[110]), 2: _layout([pages[1]], ys=[690]),
    })
    assert len(blocks) == 2


def test_exclusions_are_hard_boundaries_and_long_sentences_are_not_cut():
    raw = "正文 一。\nReferences\n[1] Smith. 2024. Reference.\nAppendix A\n" + "续" * 1300 + "。"
    blocks = derive_document_blocks([raw])
    parsed = ParsedDocument(source="x", text=raw, page_texts=[raw], blocks=blocks)
    chunks = paragraph_chunks(PaperMetadata(id="p", title="t"), parsed)
    assert "References" not in "".join(c.text for c in chunks)
    assert len(chunks[-1].text) == 1301
    assert all(locate_quote(c.text, raw) is not None for c in chunks)
    assert [c.id for c in chunks] == [f"p:page:1:chunk:{i}" for i in range(len(chunks))]


def test_legacy_chunks_are_identical_and_new_chunks_use_sentence_boundaries():
    service = object.__new__(KnowledgeIngestionService)
    service.settings = SimpleNamespace(chunk_size=4, chunk_overlap=1)
    raw = "one two three four five six seven eight"
    parsed = ParsedDocument(source="x", text=raw, page_texts=[raw], page_numbers=[3])
    paper = PaperMetadata(id="p", title="t")
    for version in ("pypdf-v1", "pypdf-v2"):
        chunks = service._page_chunks(paper, parsed, parser_version=version)
        assert [c.text for c in chunks] == [
            "one two three four", "four five six seven", "seven eight",
        ]
        assert [c.id for c in chunks] == [f"p:page:3:chunk:{i}" for i in range(3)]
    raw = "这是一个完整句子。" * 200
    parsed = ParsedDocument(source="x", text=raw)
    chunks = paragraph_chunks(paper, parsed)
    assert all(c.text.endswith("。") and len(c.text) <= 1200 for c in chunks)
    assert chunks[1].metadata["spans"][0]["start"] >= chunks[0].metadata["spans"][-1]["end"]


def test_real_pdf_raw_hash_and_visitor_coordinates_are_unchanged(tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): writer._add_object(font),
    })})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 10 Tf 60 700 Td (We study a new) Tj 0 -12 Td (method.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_blank_page(width=600, height=800)
    path = tmp_path / "fixture.pdf"
    writer.write(path)
    old = parse_pdf_source(str(path))
    new = parse_pdf_source(str(path), derive_blocks=True)
    expected = "We study a new\nmethod."
    def digest(text):
        return hashlib.sha256(text.encode()).hexdigest()
    assert digest(old.text) == digest(new.text) == digest(expected)
    assert old.page_texts == new.page_texts == [expected]
    assert new.page_numbers == [1] and new.pages == 2
    assert new.blocks[0].text == "We study a new method."


def test_version_only_reparse_preserves_old_rows_and_is_idempotent(tmp_path, monkeypatch):
    from tests.test_source_reparse import _setup

    repository, service, versions, old, parsed, _, worker = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("app.knowledge.service.PARSER_VERSION", "pypdf-v3")
    with repository.database.connect() as connection:
        old_document = dict(connection.execute("SELECT * FROM documents").fetchone())
        old_chunks = [tuple(r) for r in connection.execute("SELECT * FROM chunks ORDER BY id")]
        old_evidence = [tuple(r) for r in connection.execute("SELECT * FROM evidences ORDER BY id")]
    queued = service.reparse(old["document_id"])
    assert queued["status"] == "queued"
    assert service.reparse(old["document_id"])["ingestion_id"] == queued["ingestion_id"]
    assert worker.run_once()
    new = next(d for d in versions.documents() if d["document_id"] != old["document_id"])
    assert new["parser_version"] == "pypdf-v3"
    assert new["version"].endswith(":parser:pypdf-v3")
    assert service.reparse(new["document_id"])["status"] == "unchanged"
    assert service.reparse(old["document_id"])["ingestion_id"] == queued["ingestion_id"]
    with repository.database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM documents WHERE id = ?",
                                       (old["document_id"],)).fetchone()) == old_document
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM chunks WHERE document_id = ? ORDER BY id", (old["document_id"],),
        )] == old_chunks
        assert [tuple(r) for r in connection.execute("SELECT * FROM evidences ORDER BY id")
                if tuple(r) in old_evidence] == old_evidence


def test_figure_reference_label_and_author_initial_are_not_section_boundaries():
    raw = ("References\n[1] coolcosmos.ipac.caltech.edu\nQ: What is the price?\n"
           "The study (Smith, 2024) shows results.\n2 Method\nBody text.\nReferences\n"
           "Gianni Amati and Cornelis Joost Van Rijsbergen. 2002.\nAn article.\n"
           "J. Bhogal, A. Macfarlane, and P. Smith. 2007. A review\nMore text.\n"
           "A Additional Results\nAppendix body.")
    blocks = derive_document_blocks([raw])
    assert blocks[0].kind == "paragraph"
    assert next(b for b in blocks if b.text.startswith("J. Bhogal")).kind == "references"
    assert blocks[-1].kind == "paragraph"


def test_bilingual_titles_and_numbered_affiliations_are_front_matter():
    lines = ["中文论文标题", "张三 李四", "1 第一大学", "2 实验室，深圳", "x@uni.edu",
             "摘要", "本文讨论研究。", "关键词：研究", "A Study of Retrieval",
             "John Doe, Jane Smith", "1 Faculty of Engineering", "2 Example University",
             "x@uni.edu", "Abstract", "We study retrieval.", "1 Introduction", "Body text."]
    blocks = derive_document_blocks(["\n".join(lines)])
    assert all(b.kind == "front_matter" for b in blocks[:5])
    assert all(b.kind == "front_matter" for b in blocks[8:13])
    assert blocks[-1].kind == "paragraph"


def test_frozen_legacy_reparse_keeps_legacy_parser_after_default_upgrade(tmp_path, monkeypatch):
    from tests.test_source_reparse import _new_parse, _setup

    repository, service, versions, old, parsed, _, worker = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "parser", lambda **kwargs: _new_parse(parsed))
    queued = service.reparse(old["document_id"])
    assert repository.reparse_payload(queued["ingestion_id"])["parser_version"] == "pypdf-v2"
    monkeypatch.setattr("app.knowledge.service.PARSER_VERSION", "pypdf-v3")
    assert worker.run_once()
    new = next(d for d in versions.documents() if d["document_id"] != old["document_id"])
    assert new["parser_version"] == "pypdf-v2"
    assert not new["version"].endswith(":parser:pypdf-v3")


def test_new_import_identity_does_not_overwrite_legacy_or_reused_upstream_version():
    service = object.__new__(KnowledgeIngestionService)
    parsed = ParsedDocument(source="x", text="Body", metadata={"etag": "unchanged"})
    old = service._paper_from_document(parsed, "x", parser_version="pypdf-v2")
    new = service._paper_from_document(parsed, "x", parser_version="pypdf-v3")
    changed = service._paper_from_document(parsed.model_copy(update={"text": "New body"}),
                                           "x", parser_version="pypdf-v3")
    assert len({old.id, new.id, changed.id}) == 3
    assert new.metadata["parser_version"] == "pypdf-v3"
