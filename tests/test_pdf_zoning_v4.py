import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.knowledge.paragraph_chunks import paragraph_chunks
from app.knowledge.service import KnowledgeIngestionService
from app.retrieval.anchors import locate_quote
from app.schemas.documents import PaperMetadata, ParsedDocument
from app.tools.pdf_layout import TextRun, derive_document_blocks
from app.tools.pdf_tools import parse_pdf_source


def derive(pages):
    """Each row is (text, x, baseline, size); None means missing coordinates."""
    texts = ["\n".join(row[0] for row in page) for page in pages]
    layouts = {number: ([TextRun(*row) for row in page if row[1] is not None], 800)
               for number, page in enumerate(pages, 1)}
    blocks = derive_document_blocks(texts, layouts=layouts, version="pypdf-v4")
    for block in blocks:
        raw = "\n".join(texts[span.page - 1][span.start:span.end] for span in block.spans)
        assert locate_quote(block.text, raw) is not None
    return blocks


def containing(blocks, text):
    return next(block for block in blocks if text in block.text)


def test_frozen_v3_serialization_and_chunk_bytes():
    cases = json.loads((Path(__file__).parent / "fixtures/pdf-layout-v3.json").read_text("utf-8"))
    for case in cases:
        layouts = {int(number): ([TextRun(**run) for run in runs], height)
                   for number, (runs, height) in case["layouts"].items()}
        blocks = derive_document_blocks(case["texts"], layouts=layouts, version="pypdf-v3")
        assert [block.model_dump_json().encode() for block in blocks] == [
            value.encode() for value in case["blocks_json"]]
        parsed = ParsedDocument(source="fixture.pdf", text="\n\n".join(case["texts"]),
                                page_texts=case["texts"], blocks=blocks)
        chunks = paragraph_chunks(PaperMetadata(id="fixture", title="Regression fixture"), parsed,
                                  parser_version="pypdf-v3")
        assert [chunk.model_dump_json().encode() for chunk in chunks] == [
            value.encode() for value in case["chunks_json"]]
        assert ParsedDocument.model_validate_json(parsed.model_dump_json()) == parsed


def test_margin_repetition_numbers_and_uncertain_body():
    blocks = derive([
        [("Journal 2024 — 1", 60, 770, 9), ("Body repeated line.", 60, 650, 10),
         ("Page 1 of 2", 270, 25, 9)],
        [("Journal 2025 — 2", 60, 770, 9), ("Body repeated line.", 60, 650, 10),
         ("第 2 页", 270, 25, 9)],
    ])
    assert [b.kind for b in blocks] == ["header", "paragraph", "footer"] * 2
    blocks = derive([[("Uncertain margin prose", 60, 770, 10), ("12", 60, 400, 10)]])
    assert all(b.kind == "paragraph" for b in blocks)


@pytest.mark.parametrize("note", [
    "*通讯作者：张三", "基金项目：研究资助", "©2025 Example", "Creative Commons license",
    "Proceedings of a Conference", "https://example.test/data", "¹An explanatory footnote.",
    "1Our code is available online.", "†Equal contribution",
])
def test_first_and_later_page_footnotes_with_wrapped_continuation(note):
    page = [("A substantial body line describes the experiment.", 60, 400, 10),
            (note, 70, 150, 8), ("a wrapped explanatory note.", 60, 140, 8),
            ("Body text in the next column.", 310, 140, 10)]
    blocks = derive([page, page])
    assert containing(blocks, note).kind == "footer"
    assert containing(blocks, "a wrapped explanatory").kind == "footer"
    assert containing(blocks, "next column").kind == "paragraph"


def test_coordinate_free_repeated_venue_needs_positive_evidence():
    page = [("Ordinary repeated prose", None, None, None),
            ("CCL 2024", None, None, None)]
    blocks = derive([page, page])
    assert [b.kind for b in blocks] == ["paragraph", "footer"] * 2


@pytest.mark.parametrize("title", ["参考文献：", "参 考 文 献", "7 References", "Bibliography",
                                  "Works Cited", "REFERENCES AND NOTES:"])
@pytest.mark.parametrize("entry", ["[1] Zhang. 2024. A study of retrieval.",
                                  "1. Zhang, J. 2024. A study of retrieval.",
                                  "Jane Smith and John Doe. 2024. A study.",
                                  "AllenAI. 2018. A public reasoning dataset."])
def test_reference_variants_cross_pages_and_appendix(title, entry):
    blocks = derive([
        [("Body text.", 60, 700, 10), (title, 60, 600, 12), (entry, 60, 580, 10),
         ("1", 270, 25, 9)],
        [("a continuation of the reference.", 60, 700, 10),
         ("Appendix A", 60, 650, 12), ("Appendix body.", 60, 630, 10)],
    ])
    assert containing(blocks, title).kind == "references"
    assert containing(blocks, "a continuation").kind == "references"
    assert containing(blocks, "Appendix body").kind == "paragraph"
    assert containing(blocks, "Appendix body").paragraph_start is True


def test_reference_heading_without_entry_and_prose_math_are_retained():
    texts = ["References", "This ordinary prose discusses references in a study.",
             "We use x = y + z in our model with 3 layers and 50% dropout.",
             "This  widely   spaced   prose remains body text.",
             "Figure 2 shows how our method performs.",
             "表 2 统计了数据集的查询数量。", "图 3(c)分别展示了检索性能。",
             "Table 8. As illustrated, the model has a tendency",
             "知识 pθ(y|x,d) = Q"]
    blocks = derive([[(text, 60, 700 - 12 * i, 10) for i, text in enumerate(texts)]])
    assert all(b.kind == "paragraph" for b in blocks)
    blocks = derive([[("Metric  12  24  36", 60, 700, 10), ("x = y + z", 60, 650, 10)]])
    assert all(b.kind == "other" for b in blocks)


def test_columns_indentation_heading_and_figure_continuation():
    blocks = derive([[
        ("1 Introduction", 60, 730, 12),
        ("The first paragraph starts here and continues", 60, 710, 10),
        ("across the next line.", 60, 698, 10),
        ("An indented new paragraph begins", 70, 680, 10),
        ("and continues after a figure", 60, 668, 10),
        ("Figure 1: A diagram.", 60, 500, 9),
        ("without starting a new paragraph.", 60, 300, 10),
        ("The other column begins a paragraph", 320, 710, 10),
        ("with an unindented continuation.", 310, 698, 10),
        ("A second right-column paragraph", 320, 680, 10),
        ("continues here.", 310, 668, 10),
    ]])
    assert containing(blocks, "first paragraph").paragraph_start is True
    assert containing(blocks, "An indented").paragraph_start is True
    assert containing(blocks, "without starting").paragraph_start is False
    assert containing(blocks, "other column").paragraph_start is True
    assert containing(blocks, "second right-column").paragraph_start is True
    assert containing(blocks, "Figure 1").kind == "caption"


def test_inset_abstract_is_not_indented_body_and_bullet_wraps_continue():
    blocks = derive([[
        ("A Study of Retrieval", 60, 770, 18), ("Example University", 60, 750, 10),
        ("Abstract", 85, 700, 12), ("The abstract begins with a long sentence", 85, 680, 9),
        ("and continues without a new paragraph.", 85, 669, 9),
        ("1 Introduction", 60, 620, 12), ("Body text has its own column boundary.", 60, 600, 10),
        ("• A list item begins", 60, 580, 10), ("and continues here.", 70, 568, 10),
    ]])
    abstract = containing(blocks, "abstract begins")
    assert "and continues without" in abstract.text
    assert abstract.paragraph_start is True
    assert containing(blocks, "• A list").paragraph_start is True
    assert "and continues here" in containing(blocks, "• A list").text


def test_cross_page_hyphen_continuation_raw_spans_and_serialization():
    blocks = derive([
        [("1 Method", 60, 160, 12), ("We study re-", 60, 100, 10), ("1", 270, 25, 9)],
        [("Journal 2025", 60, 770, 9), ("trieval across pages.", 60, 700, 10),
         ("New paragraph begins.", 70, 680, 10), ("and continues.", 60, 668, 10)],
        [("Journal 2026", 60, 770, 9), ("Continuation on the next page.", 60, 700, 10)],
    ])
    assert containing(blocks, "trieval").paragraph_start is False
    assert containing(blocks, "New paragraph").paragraph_start is True
    assert containing(blocks, "Continuation on").paragraph_start is False
    parsed = ParsedDocument(source="x", text="x", blocks=blocks)
    assert ParsedDocument.model_validate_json(parsed.model_dump_json()).blocks == blocks
    assert locate_quote("We study retrieval", "We study re-\ntrieval") is not None


def test_local_pdf_raw_text_unchanged_for_both_versions(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 10 Tf 60 700 Td (We study re-) Tj 0 -12 Td (trieval.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "paper.pdf"
    writer.write(path)
    legacy = parse_pdf_source(str(path))
    for version in ("pypdf-v3", "pypdf-v4"):
        parsed = parse_pdf_source(str(path), derive_blocks=True, parser_version=version)
        assert parsed.text.encode() == legacy.text.encode()
        assert parsed.page_texts == legacy.page_texts
        assert parsed.page_numbers == legacy.page_numbers
    service = object.__new__(KnowledgeIngestionService)
    service.parser = parse_pdf_source
    service.settings = SimpleNamespace(pdf_parser_version="pypdf-v4")
    assert service._parse(source=str(path), max_pages=1).blocks[0].paragraph_start is True


def _tex_pdf(tmp_path, fonts, lines):
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    resources = DictionaryObject()
    for resource, (name, base) in fonts.items():
        cmap = DecodedStreamObject()
        cmap.set_data((
            "/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
            "/CMapName /Test def /CMapType 2 def "
            "1 begincodespacerange <00> <FF> endcodespacerange "
            f"1 beginbfrange <00> <FF> <{base:04X}> endbfrange "
            "endcmap CMapName currentdict /CMap defineresource pop end end"
        ).encode())
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/" + name),
            NameObject("/FirstChar"): NumberObject(0),
            NameObject("/LastChar"): NumberObject(255),
            NameObject("/Widths"): ArrayObject([NumberObject(500)] * 256),
            NameObject("/ToUnicode"): writer._add_object(cmap),
        })
        resources[NameObject("/" + resource)] = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): resources})
    stream = DecodedStreamObject()
    commands = ["BT"]
    for index, line in enumerate(lines):
        commands.append(f"1 0 0 1 60 {700 - index * 12} Tm")
        for resource, data in line:
            commands.append(f"/{resource} 10 Tf <{data.hex()}> Tj")
    commands.append("ET")
    stream.set_data(" ".join(commands).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "tex.pdf"
    writer.write(path)
    return path


def test_tex_font_decoding_preserves_cjk_v3_and_layout(tmp_path):
    from pypdf import PdfReader

    from app.tools.pdf_layout import _lines, collect_text_runs
    from app.tools.pdf_tools import decode_tex_font_page

    path = _tex_pdf(tmp_path, {
        "F1": ("ABCDEF+CMR10", 0x5700), "F2": ("GHIJKL+gbsnu57", 0x5700),
    }, [
        [("F1", b'Table'), ("F1", b'2:\\quote"\x7b\x0c'), ("F2", b"2")],
        [("F1", b"Continued.")],
    ])
    page = PdfReader(path).pages[0]
    raw = page.extract_text()
    legacy = parse_pdf_source(str(path))
    v3 = parse_pdf_source(str(path), derive_blocks=True, parser_version="pypdf-v3")
    v4 = parse_pdf_source(str(path), derive_blocks=True, parser_version="pypdf-v4")
    assert v3.text == legacy.text == raw.strip()
    assert v3.page_numbers == legacy.page_numbers == v4.page_numbers
    assert v3.page_texts == legacy.page_texts
    assert v3.metadata == legacy.metadata
    assert v4.page_texts == ['Table2:\u201cquote\u201d\u2013fi\u5732\nContinued.']
    assert v4.metadata["tex_font_decoded_pages"] == [1]
    assert v4.metadata["tex_font_decoded_fonts"] == ["ABCDEF+CMR10 U+5700"]
    chunks = []
    assert page.extract_text(visitor_text=lambda t, cm, tm, f, s: chunks.append((t, f))) == raw
    decoded, decoder = decode_tex_font_page(raw, chunks)
    runs, height = collect_text_runs(page, decoder)
    lines = _lines(decoded, 1, runs, height)
    assert [(line.x, line.y, line.size) for line in lines] == [(60, 700, 10), (60, 688, 10)]
    # parse_pdf_source must pass the decoder too: coordinates allow this join.
    assert len(v4.blocks) == 1
    for block in v4.blocks:
        anchored = "\n".join(v4.page_texts[s.page - 1][s.start:s.end] for s in block.spans)
        assert locate_quote(block.text, anchored) is not None


def test_tex_short_run_and_same_shifted_cjk_glyph(tmp_path):
    path = _tex_pdf(tmp_path, {
        "F1": ("ABCDEF+CMR10", 0x5B00), "F2": ("GHIJKL+gbsnu5b", 0x5B00),
    }, [[("F2", b"1"), ("F1", b"1")]])
    v3 = parse_pdf_source(str(path))
    v4 = parse_pdf_source(str(path), parser_version="pypdf-v4")
    assert v3.text == "\u5b31\u5b31"
    assert v4.text == "\u5b311"
    assert v4.metadata["tex_font_decoded_fonts"] == ["ABCDEF+CMR10 U+5B00"]


@pytest.mark.parametrize("fonts", [
    {"F1": ("ABCDEF+gbsnu57", 0x5700), "F2": ("GHIJKL+gbsnu5b", 0x5B00)},
    {"F1": ("ABCDEF+CMR10", 0x5700), "F2": ("ABCDEF+CMR10", 0x5B00)},
    {"F1": ("ABCDEF+CMR10", 0x5700), "F2": ("ABCDEF+CMR10", 0)},
    {"F1": ("ABCDEF+CMMI10", 0x5700), "F2": ("ABCDEF+CMR10extra", 0x5700)},
])
def test_tex_ineligible_fonts_are_unchanged(tmp_path, fonts):
    path = _tex_pdf(tmp_path, fonts, [[("F1", b"12"), ("F2", b"34")]])
    v3 = parse_pdf_source(str(path))
    v4 = parse_pdf_source(str(path), parser_version="pypdf-v4")
    assert v4.model_dump() == v3.model_dump()
    assert "tex_font_decoded_pages" not in v4.metadata


@pytest.mark.parametrize(("family", "expected"), [
    ("CMR10", 'Γ∆Ωıȷ\u0060´ˇ˘¯˚¸ßæœøÆŒØfffi\u201d¡¿\u201cˆ˙\u2013\u2014˝˜¨$'),
    ("cmti12", 'Γ∆Ωıȷ\u0060´ˇ˘¯˚¸ßæœøÆŒØfffi\u201d¡¿\u201cˆ˙\u2013\u2014˝˜¨£'),
    ("CMTT9", 'Γ∆Ωıȷ\u0060´ˇ˘¯˚¸ßæœøÆŒØ↑↓␣"<>\\^_{|}~¨$'),
])
def test_tex_ot1_encoding_variants(tmp_path, family, expected):
    codes = bytes([0, 1, 10, *range(0x10, 0x20), 11, 12, 0x20, 0x22, 0x3C,
                   0x3E, 0x5C, 0x5E, 0x5F, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F, 0x24])
    path = _tex_pdf(tmp_path, {"F1": ("ABCDEF+" + family, 0x5700)}, [[("F1", codes)]])
    assert parse_pdf_source(str(path), parser_version="pypdf-v4").text == expected


def test_tex_attribution_requires_full_coverage_and_skips_duplicate_chunks():
    from app.tools.pdf_tools import decode_tex_font_page

    font = {"/BaseFont": "/ABCDEF+CMR10"}
    first, second = "\u5731", "\u5732"
    raw = first + second + "\n"
    chunks = [(first, font), (first, font), (second + "\n", font)]
    decoded, decoder = decode_tex_font_page(raw, chunks)
    assert decoded == "12\n" and decoder is not None
    assert decode_tex_font_page(raw, chunks[:-1]) == (raw, None)


def test_zoning_projects_only_verified_decoded_pages():
    from app.benchmarking.parser_trial.zoning import project_lines, span_predictions
    from app.schemas.documents import DocumentBlock, TextSpan

    pages = [{"page": 3, "lines": [
        {"id": "a", "page": 3, "start": 0, "end": 2, "text": "表\u570c"},
        {"id": "b", "page": 3, "start": 3, "end": 5, "text": "文\u5b31"},
    ]}]
    parsed = ParsedDocument(source="test", text="", page_numbers=[3],
                            page_texts=["表\u570c\n文\u5b31"])
    assert project_lines(pages, parsed)[0] is pages[0]
    parsed.page_texts = [" 表fi \r\n \n 文1 "]
    with pytest.raises(ValueError, match="Prepared raw text changed: a"):
        project_lines(pages, parsed)
    parsed.metadata = {"tex_font_decoded_pages": [3]}
    projected = project_lines(pages, parsed)
    assert projected[0]["lines"] == [
        {"id": "a", "page": 3, "start": 1, "end": 4, "text": "表fi"},
        {"id": "b", "page": 3, "start": 10, "end": 12, "text": "文1"},
    ]
    blocks = [DocumentBlock(kind="paragraph", text=line["text"], spans=[
        TextSpan(page=3, start=line["start"], end=line["end"]),
    ]) for line in projected[0]["lines"]]
    assert set(span_predictions(projected, blocks)) == {"a", "b"}
    assert pages[0]["lines"][0]["end"] == 2
    for text in ["表fi", "表fi\n字1", "表fi\n文1\nextra"]:
        parsed.page_texts = [text]
        with pytest.raises(ValueError, match="Prepared raw text changed: a"):
            project_lines(pages, parsed)


def test_numbered_prose_does_not_become_heading_and_run_in_heading_starts_paragraph():
    blocks = derive([[
        ("We compare models using GPT-", 60, 700, 10),
        ("4 for automatic attribution evaluation", 60, 688, 10),
        ("and examine the results.", 60, 676, 10),
        ("Metrics. We use precision and recall", 60, 654, 10),
        ("to evaluate the model.", 60, 642, 10),
        ("See Appendix section 10 for details", 60, 630, 10),
        ("Appendix section 10 contains results.", 60, 618, 10),
    ]])
    assert all(b.kind == "paragraph" for b in blocks)
    assert containing(blocks, "4 for").text.startswith("We compare")
    assert containing(blocks, "Metrics.").paragraph_start is True


def test_heading_before_page_margin_still_starts_next_body_line():
    blocks = derive([
        [("1 Introduction", 60, 100, 12), ("1", 270, 25, 9)],
        [("Journal 2024", 60, 770, 9), ("The first line after the heading.", 60, 700, 10)],
        [("Journal 2025", 60, 770, 9), ("continued on another page.", 60, 700, 10)],
    ])
    assert containing(blocks, "first line after").paragraph_start is True
    assert containing(blocks, "continued on another").paragraph_start is False


def test_v4_is_opt_in(tmp_path):
    from tests.test_source_reparse import _service

    _, service, _ = _service(tmp_path)
    assert service.parser_version == "pypdf-v3"
    service.settings = service.settings.model_copy(update={"pdf_parser_version": "pypdf-v4"})
    assert service.parser_version == "pypdf-v4"


def test_v4_opt_in_import_and_frozen_v3_reparse(tmp_path, monkeypatch):
    from tests.test_source_reparse import _setup

    repository, service, versions, old, parsed, _, worker = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("app.knowledge.service.PARSER_VERSION", "pypdf-v3")
    queued_v3 = service.reparse(old["document_id"])
    service.settings = service.settings.model_copy(update={"pdf_parser_version": "pypdf-v4"})
    assert worker.run_once()
    v3 = next(d for d in versions.documents() if d["parser_version"] == "pypdf-v3")
    assert v3["version"].endswith(":parser:pypdf-v3")
    frozen = repository.reparse_payload(queued_v3["ingestion_id"])
    assert frozen["parser_version"] == "pypdf-v3"
    queued_v4 = service.reparse(v3["document_id"])
    assert queued_v4["ingestion_id"] != queued_v3["ingestion_id"]
    assert worker.run_once()
    v4 = next(d for d in versions.documents() if d["parser_version"] == "pypdf-v4")
    assert v4["version"].endswith(":parser:pypdf-v4")
    assert service.reparse(v4["document_id"])["status"] == "unchanged"
    assert service.reparse(v3["document_id"])["ingestion_id"] == queued_v4["ingestion_id"]
    assert any(d["document_id"] == old["document_id"] for d in versions.documents())
    with repository.database.connect() as connection:
        for version, document in [("pypdf-v3", v3), ("pypdf-v4", v4)]:
            rows = connection.execute("SELECT metadata_json FROM chunks WHERE document_id = ?",
                                      (document["document_id"],)).fetchall()
            assert rows and all(json.loads(r[0])["parser_version"] == version for r in rows)
    paper = service._paper_from_document(parsed, "new.pdf")
    assert paper.metadata["parser_version"] == "pypdf-v4"
    service.settings = SimpleNamespace(chunk_size=4, chunk_overlap=1)
    assert service._page_chunks(paper, parsed)[0].metadata["parser_version"] == "pypdf-v4"
