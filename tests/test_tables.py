"""Synthetic-only D040 tests. No PDF parsers, inference subprocesses or network."""

from __future__ import annotations

import copy
import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tables.chars import align_chars, layout_records, reading_order
from app.tables.detect import detect, layout_fallback
from app.tables.engines import html_grid, mineru, mineru_html, ppstructure, read_mineru
from app.tables.geometry import crop_box_to_points, pixel_box
from app.tables.io import read, write
from app.tables.packets import build, validate
from app.tables.perturb import audit
from app.tables.pilot import page_numbers, run_pilot, select_dev
from app.tables.resources import Resources
from app.tables.score import proportion, score
from app.tables.verify import evidence_cells, grid_html, verify


@pytest.fixture(autouse=True)
def forbid_external_calls(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("External process/network/PDF extraction forbidden in table tests")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    import pdfminer.high_level
    import pypdf

    monkeypatch.setattr(pdfminer.high_level, "extract_pages", denied)
    monkeypatch.setattr(pypdf, "PdfReader", denied)


def fixture_table():
    cells, chars, raw = [], [], ""
    for r, row in enumerate([["模型", "精度", "F1"], ["甲", "91.2", "88%"], ["乙", "92.4", "89%"]]):
        for c, text in enumerate(row):
            box = [10 + c * 80, 20 + r * 30, 90 + c * 80, 50 + r * 30]
            cells.append(
                {"row": r, "col": c, "rowspan": 1, "colspan": 1, "bbox_pt": box, "model_text": text}
            )
            for i, character in enumerate(text):
                chars.append(
                    {
                        "id": len(chars),
                        "text": character,
                        "offset": len(raw),
                        "bbox_pt": [
                            box[0] + 6 + i * 7,
                            box[1] + 8,
                            box[0] + 12 + i * 7,
                            box[1] + 18,
                        ],
                        "font_size_pt": 10,
                        "rotation": 0,
                    }
                )
                raw += character
            raw += " "
        raw += "\n"
    page = {
        "page": 1,
        "chars": chars,
        "raw_text": raw,
        "garbled": False,
        "width_pt": 300,
        "height_pt": 200,
        "rules": [],
        "cropbox_matches_mediabox": True,
    }
    candidate = {
        "id": "p001-t001",
        "page": 1,
        "bbox_pt": [10, 20, 250, 110],
        "caption": "",
        "sources": ["test"],
    }
    return page, candidate, {"engine": "synthetic", "cells": cells, "errors": []}


def test_offsets_chinese_whitespace_superscript_and_unmatched():
    chars = [{"text": c} for c in "中文x²=3"]
    aligned, garbled = align_chars(chars, "中 文\nx² = 3")
    assert not garbled
    assert [c["offset"] for c in aligned] == [0, 2, 4, 5, 7, 9]
    aligned, _ = align_chars([{"text": c} for c in "甲X乙"], "甲Y乙")
    assert [c["offset"] for c in aligned] == [0, None, 2]
    aligned, _ = align_chars([{"text": c} for c in "111"], "1111")
    assert all(c["offset"] is None for c in aligned)  # competing repeated-text anchors


def test_ccl_and_duplicate_glyphs_are_unmappable():
    text = "".join(chr(0x5B20 + i) for i in range(10))
    chars, garbled = align_chars([{"text": c} for c in text], text)
    assert garbled and all(c["offset"] is None for c in chars)
    page, candidate, grid = fixture_table()
    page["garbled"] = True
    assert not verify(page, candidate, grid)["verified"]
    page["garbled"] = False
    page["chars"].append(copy.deepcopy(page["chars"][0]))
    assert not verify(page, candidate, grid)["verified"]


def test_layout_coordinate_conversion_without_parser(monkeypatch):
    import pdfminer.layout as layout

    class Char:
        bbox = (12, 35, 18, 45)
        matrix = (0, 1, -1, 0, 0, 0)
        size = 10

        def get_text(self):
            return "中"

    class Line:
        bbox = (10, 20, 80, 20)

    class Page(list):
        bbox = (10, 20, 110, 220)
        height = 200

    monkeypatch.setattr(layout, "LTChar", Char)
    monkeypatch.setattr(layout, "LTLine", Line)
    chars, rules = layout_records(Page([Char(), Line()]))
    assert chars[0]["bbox_pt"] == [2, 175, 8, 185]
    assert chars[0]["rotation"] == 90
    assert rules[0]["bbox_pt"] == [0, 200, 70, 200]


def test_reading_order_keeps_script_with_baseline():
    chars = [
        {"id": 0, "bbox_pt": [10, 20, 16, 30]},
        {"id": 1, "bbox_pt": [17, 16, 21, 24]},
        {"id": 2, "bbox_pt": [22, 20, 28, 30]},
        {"id": 3, "bbox_pt": [10, 40, 16, 50]},
    ]
    assert [c["id"] for c in reading_order(chars)] == [0, 1, 2, 3]


def test_candidates_combine_caption_rules_columns_without_false_fragments():
    page, _, _ = fixture_table()
    cap = [
        {"id": 100 + i, "text": c, "bbox_pt": [10 + i * 7, 2, 16 + i * 7, 12]}
        for i, c in enumerate("Table 1: Results")
    ]
    page["chars"].extend(cap)
    page["rules"] = [{"bbox_pt": [10, y, 250, y], "kind": "LTLine"} for y in (20, 50, 80, 110)]
    found = list(detect(page))
    assert len(found) == 1
    assert set(found[0]["sources"]) == {"caption", "rulings", "numeric_columns"}
    assert not found[0]["fragment"] and found[0]["page"] == 1
    assert found[0]["caption"].startswith("Table 1")
    assert layout_fallback(page) == []


@pytest.mark.parametrize("signal", ["rulings", "numeric_columns"])
def test_independent_detection_signals(signal):
    page, _, _ = fixture_table()
    if signal == "rulings":
        page["chars"] = []
        page["rules"] = [{"bbox_pt": [10, y, 250, y], "kind": "LTLine"} for y in (20, 50, 110)]
    found = list(detect(page))
    assert found and signal in found[0]["sources"]


def test_pixel_origin_and_nonuniform_scaling():
    crop = pixel_box([67.68, 227.52, 167.68, 277.52], [600, 800], [1250, 1600])
    assert crop[:2] == [141, 455]
    box = crop_box_to_points([0, 0, 20, 0, 20, 30, 0, 30], crop, [600, 800], [1250, 1600])
    assert box == pytest.approx([67.68, 227.5, 77.28, 242.5])
    with pytest.raises(ValueError):
        crop_box_to_points([0, 0, 500, 500], crop, [600, 800], [1250, 1600])


def test_html_spans_and_source_only_fill():
    cells = html_grid(
        '<table><tr><th rowspan="2">A</th><th colspan="2">B</th></tr>'
        "<tr><td>C</td><td>D</td></tr></table>"
    )
    assert [(c["row"], c["col"]) for c in cells] == [(0, 0), (0, 1), (1, 1), (1, 2)]
    page, candidate, grid = fixture_table()
    grid["cells"][4]["model_text"] = "<script>fabrication</script>"
    result = verify(page, candidate, grid)
    assert not result["verified"] and result["cells"][4]["text"] == "91.2"
    assert "fabrication" not in grid_html(result)
    result["cells"][0]["text"] = "<script>&"
    assert "&lt;script&gt;&amp;" in grid_html(result)


def test_verified_fill_references_and_header_association():
    page, candidate, grid = fixture_table()
    result = verify(page, candidate, grid)
    assert result["verified"], result["verification"]
    for cell in result["cells"]:
        assert (
            "".join(page["raw_text"][ref["start"] : ref["end"]] for ref in cell["char_refs"])
            == cell["text"]
        )
    associations = result["verification"]["checks"]["header_value_associations"]["value"]
    assert associations["associations"][0] == {"cell": 4, "row_headers": [3], "column_headers": [1]}
    assert result["evidence_eligible"] is False


@pytest.mark.parametrize(
    "corruption",
    [
        "digit",
        "symbol",
        "letter",
        "ambiguous",
        "offset",
        "yband",
        "xband",
        "bad_span",
        "rotation",
        "header",
    ],
)
def test_fail_closed_criteria(corruption):
    page, candidate, grid = fixture_table()
    if corruption in {"digit", "symbol", "letter"}:
        desired = {"digit": "9", "symbol": "%", "letter": "甲"}[corruption]
        char = next(c for c in page["chars"] if c["text"] == desired)
        char["bbox_pt"] = [251, 30, 267, 40]
        candidate["bbox_pt"][2] = 260
    elif corruption == "ambiguous":
        grid["cells"][0]["bbox_pt"][2] = 200
    elif corruption == "offset":
        page["chars"][0]["offset"] = None
    elif corruption == "yband":
        grid["cells"][4]["bbox_pt"][1] += 25
    elif corruption == "xband":
        grid["cells"][4]["bbox_pt"][0] += 65
    elif corruption == "bad_span":
        grid["cells"][0]["colspan"] = 2
    elif corruption == "rotation":
        page["chars"][0]["rotation"] = 90
    else:
        grid["cells"][0]["rowspan"] = 3
    result = verify(page, candidate, grid)
    assert not result["verified"]
    with pytest.raises(ValueError, match="Unverified"):
        evidence_cells(result)


def test_caption_exclusion_and_coverage_threshold():
    page, candidate, grid = fixture_table()
    page["chars"].append({"id": 999, "text": "9", "offset": None, "bbox_pt": [12, 112, 18, 120]})
    candidate["bbox_pt"][3] = 122
    candidate["caption_bbox_pt"] = [10, 111, 100, 122]
    assert verify(page, candidate, grid)["verified"]
    del candidate["caption_bbox_pt"]
    assert not verify(page, candidate, grid)["verified"]


def test_grouped_header_span_and_wrong_4_2_split():
    page, candidate, grid = fixture_table()
    # Move the existing 3-column table down to make room for a spanning title row.
    for cell in grid["cells"]:
        cell["row"] += 1
        cell["bbox_pt"][1] += 30
        cell["bbox_pt"][3] += 30
    for char in page["chars"]:
        char["bbox_pt"][1] += 30
        char["bbox_pt"][3] += 30
    start = len(page["raw_text"])
    page["raw_text"] += "模型组"
    for i, c in enumerate("模型组"):
        page["chars"].append(
            {
                "id": 100 + i,
                "text": c,
                "offset": start + i,
                "bbox_pt": [20 + i * 7, 28, 26 + i * 7, 38],
            }
        )
    grid["cells"].insert(
        0,
        {
            "row": 0,
            "col": 0,
            "rowspan": 1,
            "colspan": 3,
            "bbox_pt": [10, 20, 250, 50],
            "model_text": "模型组",
        },
    )
    candidate["bbox_pt"][3] += 30
    assert verify(page, candidate, grid, header_rows=2)["verified"]
    grid["cells"][0]["colspan"] = 2
    assert not verify(page, candidate, grid, header_rows=2)["verified"]


def test_every_perturbation_is_rejected():
    page, candidate, grid = fixture_table()
    verified = verify(page, candidate, grid)
    results = audit(page, candidate, verified)
    assert set(results) == {"swap_columns", "move_row", "merge_cells", "split_cell", "drop_cell"}
    assert all(r["rejected"] for r in results.values())


def source_html(grid):
    return (
        "<table>"
        + "".join(
            "<tr>"
            + "".join(f"<td>{c['model_text']}</td>" for c in grid["cells"] if c["row"] == r)
            + "</tr>"
            for r in range(3)
        )
        + "</table>"
    )


def test_mineru_exact_alignment_and_unaligned_rejection():
    page, candidate, grid = fixture_table()
    result = mineru(source_html(grid), page, candidate)
    assert not result["errors"]
    assert verify(page, candidate, result)["verified"]
    result = mineru(source_html(grid).replace("91.2", "99.2"), page, candidate)
    assert result["errors"] and not verify(page, candidate, result)["verified"]
    structured = {
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "table",
                        "bbox": [10 / 300, 0.1, 250 / 300, 0.55],
                        "content": source_html(grid),
                    }
                ],
            }
        ]
    }
    assert mineru_html(structured, page, candidate) == source_html(grid)


def test_paddle_subprocess_adapter_uses_stub(tmp_path):
    calls = []

    def stub(command, **kwargs):
        calls.append((command, kwargs))
        request = read(command[command.index("--request") + 1])
        assert request["bbox_pt"] == [10, 20, 250, 110]
        write(
            command[-1],
            {
                "pred_html": "<table><tr><td>OCR</td></tr></table>",
                "cell_box_list": [[0, 0, 480, 180]],
                "crop_px": [20, 40, 500, 220],
                "image_size_px": [600, 400],
                "table_ocr_pred": "diagnostic",
            },
        )

    _, candidate, _ = fixture_table()
    result = ppstructure(
        tmp_path / "page.png",
        candidate,
        [300, 200],
        python="paddle-python",
        cache_home="paddle-cache",
        output=tmp_path / "raw.json",
        run=stub,
    )
    assert result["cells"][0]["bbox_pt"] == [10, 20, 250, 110]
    assert result["cells"][0]["model_text"] == "OCR"
    assert calls[0][0][0] == "paddle-python"
    assert calls[0][1]["env"]["PADDLE_PDX_CACHE_HOME"] == "paddle-cache"


def test_resource_sampling_unknown_values_are_not_zero():
    with Resources(
        sampler=lambda pid: {"process_tree_rss_bytes": 123, "process_vram_bytes": None}, interval=10
    ) as resources:
        pass
    assert resources.result["process_tree_rss_bytes"] == 123
    assert resources.result["process_vram_bytes"] is None


def make_run(tmp_path, *, paddle_failure=False):
    from PIL import Image

    page, candidate, grid = fixture_table()
    png_dir = tmp_path / "png"
    png_dir.mkdir()
    Image.new("RGB", (600, 400), "white").save(png_dir / "001.png")
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(b"not a PDF; tests must never parse this")
    structured = tmp_path / "mineru.json"
    write(
        structured,
        {
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "table",
                            "bbox": [10 / 300, 0.1, 250 / 300, 0.55],
                            "content": source_html(grid),
                        }
                    ],
                }
            ]
        },
    )
    args = SimpleNamespace(
        pdf=pdf,
        root=tmp_path / "run",
        paper="synthetic",
        pages=[1],
        language="zh",
        engines=["mineru", "ppstructure"],
        enable_model=True,
        page_png_dir=png_dir,
        render_python=None,
        mineru_json=structured,
        paddle_python="unused",
        paddle_cache="unused",
        timeout=5,
        header_rows=1,
        row_header_cols=1,
    )

    def paddle(*args, **kwargs):
        if paddle_failure:
            raise TimeoutError("synthetic timeout")
        return copy.deepcopy(grid)

    result = run_pilot(
        args,
        extract=lambda *_: iter([page]),
        detector=lambda _: [candidate],
        paddle=paddle,
        resources=lambda: Resources(sampler=lambda _: {}, interval=10),
    )
    assert result["tables"] == 1
    return args.root


def judge_answer(packet):
    return {
        "packet_id": packet["packet_id"],
        "systems": {
            alias: [
                {
                    "id": table["id"],
                    "is_table": True,
                    "captioned": True,
                    "cells": [
                        {
                            "row": r,
                            "col": c,
                            "rowspan": 1,
                            "colspan": 1,
                            "content_correct": True,
                            "placement_correct": True,
                        }
                        for r in range(3)
                        for c in range(3)
                    ],
                    "extra_cells": 0,
                    "notes": "",
                }
                for table in tables
            ]
            for alias, tables in packet["systems"].items()
        },
        "missed_tables": [
            {"id": "mt001", "captioned": True, "total_cells": 4, "description": "页底漏表"}
        ],
    }


def test_offline_pipeline_report_blinding_strict_judging_and_scoring(tmp_path):
    root = make_run(tmp_path)
    manifest = read(root / "run.json")
    assert not manifest["performance"]["ppstructure"]["speed_gate_eligible"]
    assert (root / "report.html").is_file()
    assert build(root)["tables"] == 1
    packet = read(root / "packets/synthetic-p001.json")
    assert "mineru" not in json.dumps(packet) and "ppstructure" not in json.dumps(packet)
    answer = judge_answer(packet)
    validate(packet, answer)
    write(root / "judge-test/synthetic-p001.json", answer)
    result = score([root], "test", "synthetic labels")
    for engine in result["engines"].values():
        assert engine["verified_fraction"]["rate"] == 0.5  # missed table in denominator
        assert engine["verified_cell_accuracy"]["rate"] == 1
        assert engine["captioned_detection_recall"]["rate"] == 0.5
        assert engine["verified_fraction"]["ci95"][0] < 0.5
    assert result["languages"]["zh"]
    with pytest.raises(ValueError, match="frozen"):
        build(root)


@pytest.mark.parametrize(
    "bad",
    [
        "extra_field",
        "bool_count",
        "duplicate_cell",
        "missing_table",
        "string_boolean",
        "different_truth",
        "missing_cell",
    ],
)
def test_strict_judge_rejects_invalid_answers(tmp_path, bad):
    root = make_run(tmp_path)
    build(root)
    packet = read(root / "packets/synthetic-p001.json")
    answer = judge_answer(packet)
    table = answer["systems"]["X"][0]
    if bad == "extra_field":
        table["extra"] = 1
    elif bad == "bool_count":
        table["extra_cells"] = True
    elif bad == "duplicate_cell":
        table["cells"].append(copy.deepcopy(table["cells"][0]))
    elif bad == "missing_table":
        answer["systems"]["X"] = []
    elif bad == "string_boolean":
        table["cells"][0]["content_correct"] = "true"
    elif bad == "different_truth":
        table["captioned"] = False
    else:
        table["cells"].pop(4)
    with pytest.raises(ValueError):
        validate(packet, answer)


def test_failed_engine_is_preserved_in_timing_and_review(tmp_path):
    root = make_run(tmp_path, paddle_failure=True)
    manifest = read(root / "run.json")
    assert manifest["performance"]["ppstructure"]["failures"] == 1
    assert manifest["performance"]["ppstructure"]["tables"] == 1
    result = read(root / "tables/p001-t001-ppstructure.json")
    assert not result["grid"]["verified"]
    assert "synthetic timeout" in result["grid"]["errors"][0]
    build(root)
    packet = read(root / "packets/synthetic-p001.json")
    assert all(len(v) == 1 for v in packet["systems"].values())


def test_selection_only_reads_existing_json(tmp_path):
    write(
        tmp_path / "manifest.json",
        {"papers": [{"paper": "paper", "language": "zh", "pdf": "unused.pdf", "page_count": 12}]},
    )
    write(
        tmp_path / "normalized/mineru/paper.json",
        [{"page": 3, "blocks": [{"type": "table"}]}, {"page": 4, "blocks": [{"type": "text"}]}],
    )
    result = select_dev(tmp_path, tmp_path / "selection.json")
    assert result["papers"][0]["pages"] == [3]


def test_strict_json_and_empty_denominator(tmp_path):
    path = tmp_path / "bad.json"
    for value in ('{"x":1,"x":2}', '{"x":NaN}'):
        path.write_text(value, encoding="utf-8")
        with pytest.raises(ValueError):
            read(path)
    assert proportion(0, 0)["rate"] is None
    assert page_numbers("1,3-5") == [1, 3, 4, 5]
    with pytest.raises(ValueError):
        page_numbers("5-3")


def test_model_guard_precedes_io():
    args = SimpleNamespace(engines=["ppstructure"], enable_model=False, pdf=Path("missing"))
    with pytest.raises(ValueError, match="enable-model"):
        run_pilot(args)


def test_exact_995_percent_boundary_and_critical_character_override():
    page, candidate, grid = fixture_table()
    for cell in grid["cells"]:
        cell.pop("model_text")
        cell["bbox_pt"][0] *= 20
        cell["bbox_pt"][2] *= 20
    for char in page["chars"]:
        char["bbox_pt"][0] *= 20
        char["bbox_pt"][2] *= 20
    candidate["bbox_pt"] = [200, 20, 5020, 110]
    page["width_pt"] = 6000
    needed = 200 - len(page["chars"])
    for i in range(needed):
        box = [700 + i * 5, 28, 704 + i * 5, 38]
        if i == needed - 1:
            box = [5018, 28, 5023, 38]  # one source letter straddles the region boundary
        page["chars"].append(
            {"id": len(page["chars"]), "text": "a", "offset": len(page["raw_text"]), "bbox_pt": box}
        )
        page["raw_text"] += "a"
    result = verify(page, candidate, grid)
    # Synthetic widened columns now intentionally fail the independent hidden-gutter gate.
    assert result["verification"]["checks"]["character_coverage"]["passed"]
    assert result["verification"]["checks"]["character_coverage"]["value"]["ratio"] == 0.995
    page["chars"][-2]["bbox_pt"] = [5018, 40, 5023, 48]
    assert not verify(page, candidate, grid)["verified"]
    page["chars"][-2]["bbox_pt"] = [700, 28, 704, 38]
    page["chars"][-1]["text"] = "%"
    page["raw_text"] = page["raw_text"][:-1] + "%"
    result = verify(page, candidate, grid)
    assert not result["verified"]
    assert not result["verification"]["checks"]["numeric_symbol_coverage"]["passed"]


def test_multiline_cell_and_text_refill():
    page, candidate, grid = fixture_table()
    target = grid["cells"][3]
    target["model_text"] = "甲组"
    page["chars"].append(
        {"id": 100, "text": "组", "offset": len(page["raw_text"]), "bbox_pt": [16, 70, 22, 78]}
    )
    page["raw_text"] += "组"
    target["text"] = "fabricated"
    target["char_refs"] = [{"start": 999999}]
    result = verify(page, candidate, grid)
    assert result["verified"]
    assert result["cells"][3]["text"] == "甲\n组"


def test_geometrically_legal_merge_crossing_value_partition_is_rejected():
    page, candidate, grid = fixture_table()
    grid["cells"][3]["colspan"] = 2
    grid["cells"][3]["bbox_pt"][2] = grid["cells"][4]["bbox_pt"][2]
    grid["cells"][3].pop("model_text")
    grid["cells"].pop(4)
    result = verify(page, candidate, grid, row_header_cols=1)
    assert result["verification"]["checks"]["geometry"]["passed"]
    assert not result["verification"]["checks"]["header_value_associations"]["passed"]


def test_mineru_per_page_directory_and_duplicate_detection(tmp_path):
    write(tmp_path / "001/structured_content.json", {"pages": [{"page_idx": 0, "blocks": []}]})
    write(tmp_path / "008/structured_content.json", {"pages": [{"page_idx": 7, "blocks": []}]})
    assert [p["page_idx"] for p in read_mineru(tmp_path)["pages"]] == [0, 7]
    write(tmp_path / "duplicate/structured_content.json", {"pages": [{"page_idx": 7}]})
    with pytest.raises(ValueError, match="Duplicate"):
        read_mineru(tmp_path)


def test_false_verified_detection_is_penalized_and_missing_judgement_fails(tmp_path):
    root = make_run(tmp_path)
    build(root)
    packet = read(root / "packets/synthetic-p001.json")
    answer = judge_answer(packet)
    for rows in answer["systems"].values():
        rows[0].update(is_table=False, captioned=False, cells=[])
    write(root / "judge-test/synthetic-p001.json", answer)
    result = score([root], "test", "synthetic")
    for engine in result["engines"].values():
        assert engine["verified_whole_table_accuracy"]["rate"] == 0
        assert engine["verified_cell_accuracy"]["rate"] == 0
        assert engine["verified_fraction"]["rate"] == 0
    with pytest.raises(ValueError, match="exactly"):
        score([root], "missing", "synthetic")


def test_timing_reconciliation_and_deterministic_blinding(tmp_path):
    root = make_run(tmp_path)
    manifest = read(root / "run.json")
    for engine in manifest["engines"]:
        record = read(root / f"tables/p001-t001-{engine}.json")
        assert record["timings"]["total_seconds"] == pytest.approx(
            manifest["performance"][engine]["paper_pipeline_seconds"]
        )
    build(root)
    first = (root / "packets/synthetic-p001.json").read_bytes()
    build(root)
    assert (root / "packets/synthetic-p001.json").read_bytes() == first


def test_failed_pdf_stage_is_journaled_and_cannot_drop_pages_from_review(tmp_path):
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(b"Never parsed")
    args = SimpleNamespace(
        pdf=pdf,
        root=tmp_path / "run",
        paper="synthetic",
        pages=[1],
        language="zh",
        engines=["ppstructure"],
        enable_model=True,
        page_png_dir=tmp_path,
        render_python=None,
        mineru_json=None,
        paddle_python="unused",
        paddle_cache="unused",
        timeout=5,
        header_rows=1,
        row_header_cols=1,
    )

    def broken_extract(*_):
        raise ValueError("synthetic page extraction failure")

    with pytest.raises(ValueError, match="synthetic page"):
        run_pilot(
            args,
            extract=broken_extract,
            resources=lambda: Resources(sampler=lambda _: {}, interval=10),
        )
    manifest = read(args.root / "run.json")
    assert manifest["status"] == "failed" and manifest["elapsed_seconds"] >= 0
    with pytest.raises(ValueError, match="completed"):
        build(args.root)


def test_zero_area_source_glyph_is_not_silently_removed_from_coverage():
    page, candidate, grid = fixture_table()
    page["chars"][0]["bbox_pt"][2] = page["chars"][0]["bbox_pt"][0]
    result = verify(page, candidate, grid)
    assert not result["verified"]
    assert not result["verification"]["checks"]["character_geometry"]["passed"]


def add_line(page, text, x, y):
    for i, char in enumerate(text):
        page["chars"].append(
            {
                "id": len(page["chars"]),
                "text": char,
                "bbox_pt": [x + i * 5, y, x + i * 5 + 4, y + 8],
            }
        )


def test_tall_booktabs_and_column_caption():
    page, _, _ = fixture_table()
    page["chars"] = []
    page["width_pt"] = 600
    page["height_pt"] = 600
    add_line(page, "left column prose", 10, 10)
    add_line(page, "Table1:Results", 320, 10)
    page["rules"] = [{"bbox_pt": [320, y, 580, y], "kind": "LTLine"} for y in (30, 60, 400)]
    found = list(detect(page))
    assert len(found) == 1 and found[0]["caption"] == "Table1:Results"
    assert found[0]["bbox_pt"][3] == 402
    assert found[0]["captioned"] and not found[0]["fragment"]


def test_figure_caption_rejects_rect_content():
    page, _, _ = fixture_table()
    page["chars"] = []
    add_line(page, "Figure1:Diagram", 10, 115)
    page["rules"] = [{"bbox_pt": [10, 20, 250, 110], "kind": "LTRect"}]
    assert list(detect(page)) == []
    assert page["rejected"][0]["reason"] == "Non-grid graphic content"


def test_one_point_overlap_snaps_and_straddling_glyph_fails():
    page, candidate, grid = fixture_table()
    for cell in grid["cells"]:
        if cell["col"] == 0:
            cell["bbox_pt"][2] += 1
    result = verify(page, candidate, grid)
    assert result["verified"], result["verification"]
    assert result["cells"][0]["bbox_pt"][2] == result["cells"][1]["bbox_pt"][0]
    # This glyph bridges text in the two logical columns, leaving no separator.
    page["chars"][0]["bbox_pt"][2] = 110
    assert not verify(page, candidate, grid)["verification"]["checks"]["geometry"]["passed"]


def test_empty_stub_corner_and_value_are_valid():
    page, candidate, grid = fixture_table()
    remove = set()
    for index in (0, 4):
        cell = grid["cells"][index]
        remove.update(
            c["id"]
            for c in page["chars"]
            if cell["bbox_pt"][0] <= c["bbox_pt"][0] < cell["bbox_pt"][2]
            and cell["bbox_pt"][1] <= c["bbox_pt"][1] < cell["bbox_pt"][3]
        )
        cell["model_text"] = ""
    page["chars"] = [c for c in page["chars"] if c["id"] not in remove]
    result = verify(page, candidate, grid)
    assert result["verified"], result["verification"]
    assert (
        result["verification"]["checks"]["header_value_associations"]["value"]["empty_values"] == 1
    )


def test_pp_geometry_reorders_boxes_and_infers_empty_corner():
    from app.tables.engines import pp_grid

    raw = {
        "pred_html": "<table><tr><td></td><th>B</th></tr><tr><td>C</td><td>D</td></tr></table>",
        "cell_box_list": [[50, 50, 100, 100], [50, 0, 100, 50], [0, 50, 50, 100]],
        "crop_px": [0, 0, 100, 100],
        "image_size_px": [100, 100],
    }
    result = pp_grid(raw, [100, 100], {"bbox_pt": [0, 0, 100, 100]})
    assert result["cells"][0]["bbox_pt"] == [0, 0, 50, 50]
    assert result["cells"][1]["bbox_pt"] == [50, 0, 100, 50]


def test_mineru_empty_corner_and_no_structure():
    page, candidate, grid = fixture_table()
    box = grid["cells"][0]["bbox_pt"]
    page["chars"] = [
        c
        for c in page["chars"]
        if not (box[0] <= c["bbox_pt"][0] < box[2] and box[1] <= c["bbox_pt"][1] < box[3])
    ]
    grid["cells"][0]["model_text"] = ""
    result = mineru(source_html(grid), page, candidate)
    assert not result["errors"]
    assert verify(page, candidate, result)["verified"]
    for empty_structure in ("", "| A | B |\n| --- | --- |"):
        with pytest.raises(ValueError, match="engine produced no table structure"):
            mineru(empty_structure, page, candidate)


def test_midrule_and_html_header_inference():
    page, candidate, grid = fixture_table()
    candidate["rule_ys"] = [20, 50, 110]
    assert verify(page, candidate, grid)["header_rows"] == 1
    for cell in grid["cells"]:
        cell["header_tag"] = cell["row"] < 2
    candidate.pop("rule_ys")
    result = verify(page, candidate, grid)
    assert result["verified"] and result["header_rows"] == 2


def test_offline_rejects_holdout_paths():
    from app.tables.offline import dev_path

    with pytest.raises(ValueError, match="Holdout access forbidden"):
        dev_path("data/evaluation/holdout-d040/never-read.json")


def test_persistent_paddle_session_reuses_one_process(monkeypatch, tmp_path):
    import io

    from app.tables.engines import PaddleSession

    calls = []
    process = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO(
            'native log\n{"pred_html":"first","cold_start_seconds":25}\n'
            '{"pred_html":"second","cold_start_seconds":0}\n'
        ),
        wait=lambda **_: 0,
    )

    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with PaddleSession("python", "cache", tmp_path / "worker.log") as worker:
        assert worker.request({"crop": 1}, 1)["cold_start_seconds"] == 25
        assert worker.request({"crop": 2}, 1)["cold_start_seconds"] == 0
        assert len(process.stdin.getvalue().splitlines()) == 2
    assert len(calls) == 1
    assert calls[0][0][0][-1] == "--stream"


def test_second_ccl_offset_family_stays_unmappable():
    text = "".join(chr(0x5700 + ord(c)) for c in "Table2:Results")
    chars, garbled = align_chars([{"text": c} for c in text], text)
    assert garbled and all(c["offset"] is None for c in chars)
    page, candidate, grid = fixture_table()
    page["raw_text"] += text
    assert not verify(page, candidate, grid)["verification"]["checks"]["page_mappable"]["passed"]


@pytest.mark.parametrize(
    "angle,offset,excluded", [(45, None, True), (90, None, False), (45, 0, True), (0, None, False)]
)
def test_watermark_is_any_oblique_glyph(angle, offset, excluded):
    from app.tables.chars import watermark_glyph

    assert watermark_glyph({"rotation": angle, "offset": offset}) == excluded


def test_watermark_excluded_before_ownership():
    page, candidate, grid = fixture_table()
    page["chars"].append(
        {"id": 999, "text": "2", "offset": None, "rotation": 45, "bbox_pt": [20, 25, 120, 70]}
    )
    result = verify(page, candidate, grid)
    assert result["verified"]
    assert result["excluded_watermark_glyphs"][0]["id"] == 999


def test_ligature_requires_exact_consecutive_expansion():
    from app.tables.chars import expansion_mappable

    chars = [
        {"id": i, "text": t, "offset": i, "multi_codepoint_glyph": True, "bbox_pt": [0, 0, 10, 10]}
        for i, t in enumerate("fi")
    ]
    assert all(expansion_mappable(c, chars, "fi") for c in chars)
    chars[1]["offset"] = 2
    assert not expansion_mappable(chars[0], chars, "f i")


def test_crosscheck_alone_rejects_dropped_text():
    page, candidate, grid = fixture_table()
    grid["cells"][4]["model_text"] = ""
    result = verify(page, candidate, grid)
    failed = [
        name for name, check in result["verification"]["checks"].items() if not check["passed"]
    ]
    assert failed == ["model_text_crosscheck"]


def test_crosscheck_tolerates_case_and_ocr_but_rejects_missing_prefix():
    from difflib import SequenceMatcher

    assert SequenceMatcher(None, "size", "batchsize").ratio() < 0.65
    page, candidate, grid = fixture_table()
    grid["cells"][4]["model_text"] = "91,2"
    result = verify(page, candidate, grid)
    assert result["verified"]
    assert result["verification"]["checks"]["model_text_crosscheck"]["value"]


def test_pp_ordered_noisy_edges():
    from app.tables.engines import pp_grid

    raw = {
        "pred_html": "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>",
        "cell_box_list": [[0, 0, 52, 51], [49, 1, 100, 50], [1, 49, 50, 100], [51, 52, 99, 100]],
        "crop_px": [0, 0, 100, 100],
        "image_size_px": [100, 100],
    }
    result = pp_grid(raw, [100, 100], {"bbox_pt": [0, 0, 100, 100]})
    assert result["cells"][0]["bbox_pt"] == [0, 0, 52, 51]


def test_mineru_wraps_within_logical_row():
    page, candidate, grid = fixture_table()
    grid["cells"][0]["model_text"] += "Z"
    page["chars"].append(
        {"id": 999, "text": "Z", "offset": len(page["raw_text"]), "bbox_pt": [16, 39, 22, 47]}
    )
    page["raw_text"] += "Z"
    result = mineru(source_html(grid), page, candidate)
    assert not result["errors"], result["errors"]
    assert verify(page, candidate, result)["verified"]


def test_header_partition_extends_past_rowspan():
    page, candidate, grid = fixture_table()
    grid["cells"][0]["rowspan"] = 2
    grid["cells"][0]["bbox_pt"][3] = 80
    grid["cells"][0]["model_text"] += grid["cells"][3]["model_text"]
    grid["cells"].pop(3)
    result = verify(page, candidate, grid)
    assert result["header_rows"] == 2


def test_pp_missing_empty_cell_uses_glyph_gutters():
    from app.tables.engines import pp_grid

    raw = {
        "pred_html": "<table><tr><td></td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>",
        "cell_box_list": [[50.4, 0, 100, 50.2], [0, 49.8, 49.7, 100], [50.1, 50.4, 100, 100]],
        "crop_px": [0, 0, 100, 100],
        "image_size_px": [100, 100],
    }
    chars = [{"bbox_pt": b} for b in ([70, 20, 80, 30], [20, 70, 30, 80], [70, 70, 80, 80])]
    result = pp_grid(raw, [100, 100], {"bbox_pt": [0, 0, 100, 100]}, chars)
    assert len(result["cells"]) == 4
    assert result["cells"][0]["bbox_pt"][2] < 70
