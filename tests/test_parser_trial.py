"""Deterministic tests: real stored JSON excerpts, synthetic lines, no live services."""

import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.benchmarking.parser_trial.__main__ import align, validate_pages
from app.benchmarking.parser_trial.align import align_page
from app.benchmarking.parser_trial.common import read, write
from app.benchmarking.parser_trial.judge import judge_deepseek
from app.benchmarking.parser_trial.normalize import (
    MINERU_TYPES,
    PADDLE_TYPES,
    TYPES,
    bbox_points,
    mineru_pages,
    paddle_page,
)
from app.benchmarking.parser_trial.packets import (
    blind_page,
    expand_line_answer,
    line_packet,
    packets,
    validate_answer,
)
from app.benchmarking.parser_trial.score import (
    THRESHOLDS,
    agree,
    complete_timing,
    f1,
    gates,
    line_metrics,
    percentile95,
    score,
    visual_metrics,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network calls forbidden in parser-trial tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def line(index, text, x=60, y=100, page=1):
    return {
        "id": f"l{index}",
        "page": page,
        "text": text,
        "anchor_pt": [x, y],
        "font_size_pt": 10,
        "start": index * 10,
        "end": index * 10 + len(text),
    }


def block(index, text, kind="paragraph", box=None):
    return {
        "id": f"b{index}",
        "order": index,
        "type": kind,
        "text": text,
        "bbox_pt": box or [50, 80, 270, 130],
    }


def page(blocks, parser="mineru", number=1):
    return {
        "parser": parser,
        "paper": "p",
        "page": number,
        "page_width_pt": 600,
        "page_height_pt": 800,
        "blocks": blocks,
    }


def answer(packet, label="body"):
    return {
        "packet_id": packet["packet_id"],
        "lines": [
            {"id": r["id"], "label": label, "paragraph_start": label == "body"}
            for r in packet["lines"]
        ],
    }


def image_answer(packet):
    result = {"packet_id": packet["packet_id"], "systems": {}}
    for alias, blocks in packet["systems"].items():
        result["systems"][alias] = {
            "tables": [
                {
                    "id": b["id"],
                    "total_cells": 20,
                    "error_cells": 1,
                    "examples": ["row 2 col 3 wrong"],
                }
                for b in blocks
                if b["type"] == "table"
            ],
            "formulas": [
                {"id": b["id"], "equivalent": True} for b in blocks if b["type"] == "formula"
            ],
            "missed_tables": [],
            "missed_formulas": [],
        }
    return result


def test_real_mineru_excerpt_normalization_geometry_and_nested_captions():
    raw = read(Path(__file__).parent / "fixtures/parser_trial/mineru_excerpt.json")
    pages = mineru_pages(raw, raw, "2025.ccl-1.12")
    assert [p["page"] for p in pages] == [7, 8]
    assert pages[0]["parser_version"] == "4.0.12"
    header = pages[0]["blocks"][0]
    assert header["bbox_pt"] == pytest.approx(
        [
            0.118 * 595.2760009765625,
            0.047 * 841.8900146484375,
            0.273 * 595.2760009765625,
            0.062 * 841.8900146484375,
        ]
    )
    blocks = [b for p in pages for b in p["blocks"]]
    assert {b["type"] for b in blocks} >= {
        "paragraph",
        "heading",
        "formula",
        "table",
        "caption",
        "footnote",
        "page_number",
    }
    assert next(b for b in blocks if b["type"] == "table")["html"].startswith("<table>")
    assert "\\tag" in next(b for b in blocks if b["type"] == "formula")["latex"]
    assert "image_source" not in json.dumps(pages)


@pytest.mark.parametrize("mapping", [MINERU_TYPES, PADDLE_TYPES])
def test_type_mapping_tables_cover_enum(mapping):
    assert set(mapping.values()) <= set(TYPES)
    assert all(mapping[k] == k for k in TYPES)


@pytest.mark.parametrize(
    "source,target",
    [
        ("text", "paragraph"),
        ("doc_title", "title"),
        ("paragraph_title", "heading"),
        ("display_formula", "formula"),
        ("figure_title", "caption"),
        ("table_title", "caption"),
        ("number", "page_number"),
        ("reference_content", "references"),
        ("new_label", "other"),
    ],
)
def test_paddle_synthetic_labels_and_pixel_coordinates(source, target):
    raw = {
        "res": {
            "parsing_res_list": [
                {"block_label": source, "block_bbox": [100, 200, 500, 400], "block_content": "x"}
            ]
        }
    }
    result = paddle_page(raw, "p", 2, 600, 800, 1200, 1600)
    assert result["blocks"][0]["type"] == target
    assert result["blocks"][0]["bbox_pt"] == [50, 100, 250, 200]
    assert result["page"] == 2


@pytest.mark.parametrize("box", [[0, 0, 2, 1], [1, 0, 0, 1], [0, float("nan"), 1, 1]])
def test_invalid_geometry_fails_closed(box):
    with pytest.raises(ValueError):
        bbox_points(box, 600, 800)


def test_alignment_column_disambiguation_unaligned_and_starts():
    lines = [
        line(0, "中 文测试"),
        line(1, "continued", y=120),
        line(2, "中 文测试", x=320),
        line(3, "missing", y=200),
    ]
    blocks = [block(0, "**中文测试**\ncontinued"), block(1, "中文测试", box=[300, 80, 500, 130])]
    result = align_page(lines, page(blocks))
    assert [r["block_id"] for r in result["lines"]] == ["b0", "b0", "b1", None]
    assert [r["paragraph_start"] for r in result["lines"]] == [True, False, True, False]
    assert result["unaligned"] == 1
    with pytest.raises(ValueError, match="physical page"):
        align_page([line(1, "x", page=2)], page(blocks))


def test_ambiguous_text_without_coordinates_is_unaligned():
    row = {**line(0, "repeat"), "anchor_pt": None}
    assert align_page([row], page([block(0, "repeat"), block(1, "repeat")]))["unaligned"] == 1


def test_baseline_spans_disambiguate_and_cross_page_continuation():
    b = {**block(0, "repeat"), "spans": [{"start": 10, "end": 16}], "continuation": True}
    rows = [line(0, "repeat"), line(1, "repeat")]
    aligned = align_page(rows, page([b], parser="pypdf-v3"))
    assert aligned["lines"][0]["label"] == "unaligned"
    assert aligned["lines"][1]["label"] == "body"
    assert aligned["lines"][1]["paragraph_start"] is False


def test_blinding_is_deterministic_and_roundtrips_identifiers():
    outputs = {
        "mineru": page([block(99, "a", "formula")]),
        "paddle": page([block(22, "b", "table")]),
    }
    packet, mapping = blind_page("p", 1, "page.png", outputs, 39)
    assert (packet, mapping) == blind_page("p", 1, "page.png", outputs, 39)
    serialized = json.dumps(packet)
    assert "mineru" not in serialized and "paddle" not in serialized
    for alias, private in mapping.items():
        original = outputs[private["parser"]]["blocks"][0]
        assert private["blocks"][packet["systems"][alias][0]["id"]] == original["id"]
    validate_answer(packet, image_answer(packet))


def test_packet_selection_table_priority_cap_and_call_budget(tmp_path):
    write(tmp_path / "manifest.json", {"papers": [{"paper": "p", "language": "zh"}]})
    prepared = [
        {"page": n, "png": f"{n}.png", "lines": [line(n, "text", page=n)]} for n in range(1, 61)
    ]
    write(tmp_path / "prepared/p.json", prepared)
    for parser in ("mineru", "paddle"):
        write(
            tmp_path / f"normalized/{parser}/p.json",
            [
                page([block(0, "x", "formula" if n <= 30 else "table")], parser, n)
                for n in range(1, 61)
            ],
        )
    result = packets(tmp_path)
    assert result == {"line_packets": 1, "image_packets": 40}
    index = read(tmp_path / "packets/index.json")
    assert index["planned_annotation_calls"] == 85
    assert all(int(key[-3:]) > 30 for key in index["image_packets"][:30])
    assert packets(tmp_path) == result
    (tmp_path / "judge-first").mkdir()
    with pytest.raises(ValueError, match="frozen"):
        packets(tmp_path)


def test_strict_answers_missing_extra_duplicate_and_invalid_bool():
    packet = line_packet("p", [{"lines": [line(0, "text")]}])
    valid = answer(packet)
    for invalid in [
        {**valid, "extra": 1},
        {**valid, "lines": []},
        {**valid, "lines": valid["lines"] * 2},
        {**valid, "lines": [{"id": "l0", "label": "body", "paragraph_start": 1}]},
    ]:
        with pytest.raises(ValueError):
            validate_answer(packet, invalid)


def test_compact_line_answers_expand_to_one_row_per_line():
    packet = line_packet("p", [{"lines": [line(i, f"t{i}") for i in range(5)]}])
    compact = {
        "packet_id": "lines-p",
        "spans": [{"from": "l0", "to": "l1", "label": "front_matter"},
                  {"from": "l4", "to": "l4", "label": "footer"}],
        "paragraph_starts": ["l2"],
    }
    full = validate_answer(packet, expand_line_answer(packet, compact))
    assert [(r["label"], r["paragraph_start"]) for r in full["lines"]] == [
        ("front_matter", False), ("front_matter", False), ("body", True),
        ("body", False), ("footer", False),
    ]
    for bad in [
        {**compact, "spans": [{"from": "l1", "to": "l0", "label": "header"}]},
        {**compact, "spans": [{"from": "l0", "to": "l2", "label": "header"},
                              {"from": "l2", "to": "l3", "label": "footer"}]},
        {**compact, "spans": [{"from": "l0", "to": "l0", "label": "body"}]},
        {**compact, "paragraph_starts": ["l2", "l2"]},
        {**compact, "spans": [{"from": "x", "to": "l0", "label": "header"}]},
        {**compact, "extra": 1},
    ]:
        with pytest.raises(ValueError):
            expand_line_answer(packet, bad)
    # Start flags inside non-body spans are dropped; the span label wins.
    dropped = expand_line_answer(packet, {**compact, "paragraph_starts": ["l0", "l2"]})
    assert [r["paragraph_start"] for r in dropped["lines"]] == [False, False, True, False, False]


def test_agreement_missing_disagreement_arbitration():
    packet = line_packet("p", [{"lines": [line(0, "text")]}])
    left, right = answer(packet), answer(packet, "header")
    consensus, conflicts = agree(packet, left, left)
    assert consensus == left and conflicts == []
    consensus, conflicts = agree(packet, left, right)
    assert not consensus["lines"] and conflicts[0]["page"] == 1
    assert agree(packet, left, None)[0]["lines"] == []
    resolved, rows = agree(packet, left, right, {conflicts[0]["id"]: left["lines"][0]})
    assert resolved == left and rows[0]["resolved"]
    with pytest.raises(ValueError):
        agree(packet, left, right, {conflicts[0]["id"]: {**left["lines"][0], "id": "wrong"}})


def test_visual_disagreements_and_missing_objects():
    packet, _ = blind_page(
        "p", 1, "p.png", {"mineru": page([block(0, "x", "table")]), "paddle": page([])}, 39
    )
    a = image_answer(packet)
    b = copy.deepcopy(a)
    alias = next(k for k, v in b["systems"].items() if v["tables"])
    b["systems"][alias]["tables"][0]["error_cells"] = 2
    b["systems"][alias]["missed_formulas"] = [{"id": "mf001", "description": "bottom"}]
    _, conflicts = agree(packet, a, b)
    assert len(conflicts) == 2
    resolved, conflicts = agree(packet, a, b, {c["id"]: c["right"] for c in conflicts})
    assert resolved == b and all(c["resolved"] for c in conflicts)
    metrics = visual_metrics(
        [
            {
                "tables": [{"total_cells": 20, "error_cells": 1}],
                "missed_tables": [{"total_cells": 10}],
                "formulas": [{"equivalent": True}],
                "missed_formulas": [{"id": "mf001"}],
            }
        ]
    )
    assert metrics["table_cell_accuracy"] == pytest.approx(19 / 30)
    assert metrics["formula_equivalence"] == 0.5


def test_metric_formulas_zero_denominators_missing_predictions_and_gates():
    assert f1(8, 1, 3) == 0.8
    assert f1(0, 0, 0) is None
    assert percentile95(list(range(1, 101))) == 95
    assert visual_metrics([])["table_cell_accuracy"] is None
    assert visual_metrics([])["formula_equivalence"] is None
    labels = ["front_matter", "header", "footer", "page_number", "references", "body", "body"]
    gold = [
        {"id": str(i), "page": 1, "label": label, "paragraph_start": label == "body"}
        for i, label in enumerate(labels)
    ]
    predicted = {g["id"]: dict(g) for g in gold}
    predicted.pop("6")
    metrics = line_metrics(gold, predicted)
    assert metrics["front_matter_f1"] == 1
    assert metrics["margin_exclusion_recall"] == 1
    assert metrics["references_exclusion_recall"] == 1
    assert metrics["body_false_exclusion_rate"] == 0.5
    assert metrics["paragraph_boundary_f1"] == pytest.approx(2 / 3)
    passing = {k: threshold for k, (operator, threshold) in THRESHOLDS.items()}
    assert gates(passing)["status"] == "pass"
    assert gates(passing)["adopt"] is False
    assert gates({**passing, "table_cell_accuracy": None})["status"] == "incomplete"
    assert gates({**passing, "body_false_exclusion_rate": 0.005001})["status"] == "fail"
    assert gates(passing, complete=False)["status"] == "incomplete"


def test_judge_hard_cap_survives_failures_and_reinvocation(tmp_path):
    ids = []
    for name in ("p", "q", "r"):
        packet = line_packet(name, [{"lines": [line(0, "text")]}])
        ids.append(packet["packet_id"])
        write(tmp_path / "packets/lines" / f"{packet['packet_id']}.json", packet)
    write(tmp_path / "packets/index.json", {"line_packets": ids})
    attempts = []

    def invoke(prompt, system):
        attempts.append(prompt)
        if len(attempts) == 1:
            raise RuntimeError("fake failure")
        packet = json.loads(prompt)
        return json.dumps({"packet_id": packet["packet_id"], "spans": [], "paragraph_starts": []})

    client = SimpleNamespace(invoke=invoke)
    assert judge_deepseek(tmp_path, 2, client) == {"calls": 2, "failures": 1, "cumulative_calls": 2}
    assert judge_deepseek(tmp_path, 2, client)["calls"] == 0
    assert len(attempts) == 2
    assert read(tmp_path / "attempts/judge-deepseek.json")[ids[0]]["status"] == "failed"
    with pytest.raises(ValueError):
        judge_deepseek(tmp_path, 13, client)


def test_offline_pipeline_packets_to_arbitrated_stratified_score(tmp_path):
    papers = [
        {"paper": "p", "language": "zh", "page_count": 1},
        {"paper": "q", "language": "en", "page_count": 1},
    ]
    write(tmp_path / "manifest.json", {"papers": papers})
    for paper in papers:
        name = paper["paper"]
        prepared = [
            {
                "page": 1,
                "width_pt": 600,
                "height_pt": 800,
                "lines": [line(0, "body")],
                "png": "1.png",
            }
        ]
        write(tmp_path / "prepared" / f"{name}.json", prepared)
        for parser in ("mineru", "paddle", "pypdf-v3"):
            normalized = page([block(0, "body")], parser)
            normalized["paper"] = name
            write(tmp_path / "normalized" / parser / f"{name}.json", [normalized])
    align(tmp_path)
    packets(tmp_path)
    for name in ("p", "q"):
        packet = read(tmp_path / f"packets/lines/lines-{name}.json")
        write(tmp_path / f"judge-left/lines-{name}.json", answer(packet))
        write(tmp_path / f"judge-right/lines-{name}.json", answer(packet, "header"))
    first = score(tmp_path, ["left", "right"], ["image-a", "image-b"])
    assert first["arms"]["mineru"]["zh"]["metrics"]["unresolved_items"] == 1
    assert "第 1 页" in (tmp_path / "arbitration.md").read_text(encoding="utf-8")
    resolutions = {c["id"]: c["left"] for c in read(tmp_path / "disagreements.json")}
    write(tmp_path / "arbitration.json", {"resolutions": resolutions})
    result = score(
        tmp_path, ["left", "right"], ["image-a", "image-b"], tmp_path / "arbitration.json"
    )
    for parser in ("mineru", "paddle", "pypdf-v3"):
        for language in ("zh", "en"):
            arm = result["arms"][parser][language]
            assert arm["metrics"]["paragraph_boundary_f1"] == 1
            assert arm["metrics"]["unresolved_items"] == 0
            assert arm["gate"]["status"] == "incomplete"  # no tables/formulas/performance
    assert result["default_parser"] == "pypdf-v3" and result["adopt"] is False


def test_physical_page_validation_rejects_missing_duplicate_or_wrong_geometry():
    expected = [{"page": 1, "width_pt": 600, "height_pt": 800}]
    with pytest.raises(ValueError):
        validate_pages(expected, [page([]), page([])])
    with pytest.raises(ValueError):
        validate_pages(expected, [page([], number=2)])
    with pytest.raises(ValueError):
        validate_pages(expected, [{**page([]), "page_width_pt": 100}])


def test_incomplete_cached_and_invalid_timings_cannot_pass():
    complete = {
        "page_timings": [
            {"page": 1, "seconds": 2, "cold": True},
            {"page": 2, "seconds": 1, "cold": False},
        ]
    }
    assert complete_timing(complete, 2)
    assert not complete_timing(complete, 3)
    assert not complete_timing({"warm_page_seconds": [1]}, 2)
    for value in (None, float("nan"), -1):
        invalid = copy.deepcopy(complete)
        invalid["page_timings"][1]["seconds"] = value
        assert not complete_timing(invalid, 2)
    cached = copy.deepcopy(complete)
    cached["page_timings"][1]["cache_hit"] = True
    assert not complete_timing(cached, 2)


def test_score_deblinds_visual_judgments_and_counts_missed_tables(tmp_path):
    write(
        tmp_path / "manifest.json", {"papers": [{"paper": "p", "language": "zh", "page_count": 1}]}
    )
    prepared = [
        {"page": 1, "width_pt": 600, "height_pt": 800, "lines": [line(0, "body")], "png": "1.png"}
    ]
    write(tmp_path / "prepared/p.json", prepared)
    for parser in ("mineru", "paddle", "pypdf-v3"):
        blocks = [block(0, "body")]
        if parser != "pypdf-v3":
            blocks += [
                block(1, "<table></table>", "table", [50, 200, 500, 300]),
                block(2, "x = y", "formula", [50, 350, 500, 370]),
            ]
        write(tmp_path / f"normalized/{parser}/p.json", [page(blocks, parser)])
    align(tmp_path)
    packets(tmp_path)
    packet = read(tmp_path / "packets/lines/lines-p.json")
    for judge in ("a", "b"):
        write(tmp_path / f"judge-{judge}/lines-p.json", answer(packet))
    visual = read(tmp_path / "packets/images/image-p-p001.json")
    private = read(tmp_path / "private/blind-map.json")[visual["packet_id"]]
    response = image_answer(visual)
    for alias, entry in private.items():
        if entry["parser"] == "paddle":
            response["systems"][alias]["missed_tables"] = [
                {"id": "mt001", "total_cells": 10, "description": "bottom table"}
            ]
            response["systems"][alias]["formulas"][0]["equivalent"] = False
    for judge in ("c", "d"):
        write(tmp_path / f"judge-{judge}/{visual['packet_id']}.json", response)
    result = score(tmp_path, ["a", "b"], ["c", "d"])
    mineru = result["arms"]["mineru"]["zh"]["metrics"]
    paddle = result["arms"]["paddle"]["zh"]["metrics"]
    assert mineru["table_cell_accuracy"] == 0.95
    assert paddle["table_cell_accuracy"] == pytest.approx(19 / 30)
    assert mineru["formula_equivalence"] == 1
    assert paddle["formula_equivalence"] == 0
