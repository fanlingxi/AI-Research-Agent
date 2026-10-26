"""Synthetic glyph-grid/vote regressions. No parsers, engines, or network."""

from copy import deepcopy

import pytest

from app.tables.chars import align_chars, decode_fonts
from app.tables.engines import html_grid
from app.tables.glyphgrid import build
from app.tables.vote import consensus, vote


def source(rows, xs=None, rules=()):
    xs = xs or [0, 40, 80]
    chars = []
    raw = ""
    for r, row in enumerate(rows):
        for col, text in enumerate(row):
            for i, t in enumerate(text):
                chars.append(
                    {
                        "id": len(chars),
                        "text": t,
                        "offset": len(raw),
                        "bbox_pt": [
                            xs[col] + 2 + i * 3,
                            r * 15 + 3,
                            xs[col] + 4 + i * 3,
                            r * 15 + 11,
                        ],
                    }
                )
                raw += t
    page = {
        "page": 1,
        "chars": chars,
        "raw_text": raw,
        "rules": [{"bbox_pt": [a, y, b, y + 0.1]} for a, y, b in rules],
    }
    candidate = {"bbox_pt": [0, 0, xs[-1], len(rows) * 15], "rule_ys": []}
    return page, candidate


def test_wrapped_first_column():
    page, candidate = source([["Method", "Score"], ["Long", "91"], ["Label", ""], ["Other", "92"]])
    physical = build(page, candidate)
    logical = html_grid(
        "<table><tr><td>Method</td><td>Score</td></tr>"
        "<tr><td>Long Label</td><td>91</td></tr>"
        "<tr><td>Other</td><td>92</td></tr></table>"
    )
    result = vote(physical, logical, "test")
    assert result["vote_passed"]
    assert result["cells"][2]["text"] == "Long\nLabel"


def test_spanning_header_cmidrule_and_dense_gutters():
    page, candidate = source(
        [["", "ABC"], ["Name", "X", "Y", "Z"], ["a", "1", "2", "3"], ["b", "4", "5", "6"]],
        [0, 30, 37, 44, 51],
        [(0, 1, 51), (30, 13, 51), (0, 28, 51), (0, 59, 51)],
    )
    physical = build(page, candidate, min_width=3, support=0.9)
    assert physical["shape"] == [4, 4]
    logical = html_grid(
        '<table><tr><td rowspan="2"></td><td colspan="3">ABC</td></tr>'
        "<tr><td>X</td><td>Y</td><td>Z</td></tr>"
        "<tr><td>a</td><td>1</td><td>2</td><td>3</td></tr>"
        "<tr><td>b</td><td>4</td><td>5</td><td>6</td></tr></table>"
    )
    # Remove the extra fixture stub string from row 1.
    page["chars"] = [
        g for g in page["chars"] if not (15 <= g["bbox_pt"][1] < 30 and g["bbox_pt"][0] < 30)
    ]
    physical = build(page, candidate)
    assert vote(physical, logical, "test")["vote_passed"]
    assert any(s["colspan"] == 3 and s["cmidrules"] for s in physical["header_segments"])


def test_numeric_row_merge_rejected():
    page, candidate = source([["Name", "Score"], ["a", "91"], ["b", "92"]])
    logical = html_grid(
        "<table><tr><td>Name</td><td>Score</td></tr><tr><td>a b</td><td>91 92</td></tr></table>"
    )
    assert not vote(build(page, candidate), logical, "ppstructure")["vote_passed"]


def test_two_passing_engines_disagree():
    page, candidate = source([["Name", "Score"], ["Long", "91"], ["Label", ""], ["Other", "92"]])
    a = html_grid(
        "<table><tr><td>Name</td><td>Score</td></tr>"
        "<tr><td>Long Label</td><td>91</td></tr>"
        "<tr><td>Other</td><td>92</td></tr></table>"
    )
    b = html_grid(
        "<table><tr><td>Name</td><td>Score</td></tr>"
        "<tr><td>Long</td><td>91</td></tr><tr><td>Label</td><td></td></tr>"
        "<tr><td>Other</td><td>92</td></tr></table>"
    )
    result = consensus(page, candidate, build(page, candidate), {"a": a, "b": b})
    assert all(v["vote_passed"] for v in result["votes"].values())
    assert result["engine_disagreement"] and not result["grid"]["verified"]
    assert "engine disagreement" in result["grid"]["errors"]


@pytest.mark.parametrize("base,cjk_font", [(0x5700, "gbsnu57"), (0x5B00, "gbsnu5b")])
def test_font_decode_raw_alignment_and_chinese_exclusion(base, cjk_font):
    original = [
        {"id": i, "text": chr(base + code), "fontname": "ABCDEF+CMR10"}
        for i, code in enumerate([65, 11, 66])
    ]
    raw = "".join(c["text"] for c in original)
    aligned, _ = align_chars(original, raw, garbled=False)
    decoded = decode_fonts(aligned)
    assert [c["text"] for c in decoded] == ["A", "ff", "B"]
    assert [c["offset"] for c in decoded] == [0, 1, 2]
    assert all(c["decoded_from"] == raw[i] for i, c in enumerate(decoded))
    chinese = [dict(c, fontname=cjk_font) for c in original]
    assert decode_fonts(chinese) == chinese
    mixed = deepcopy(original)
    mixed.append({"text": "x", "fontname": "ABCDEF+CMR10"})
    assert not any("decoded_from" in c for c in decode_fonts(mixed))


def test_vote_baseline_perturbations_all_rejected():
    from app.tables.perturb import audit

    page, candidate = source([["Name", "Score"], ["Long", "91"], ["Label", ""], ["Other", "92"]])
    logical = html_grid(
        "<table><tr><td>Name</td><td>Score</td></tr>"
        "<tr><td>Long Label</td><td>91</td></tr>"
        "<tr><td>Other</td><td>92</td></tr></table>"
    )
    result = consensus(page, candidate, build(page, candidate), {"engine": logical})
    assert result["grid"]["verified"]
    assert all(v["rejected"] for v in audit(page, candidate, result["grid"]).values())


def test_empty_stub_header_and_missing_value_header():
    page, candidate = source([["", "Score"], ["BM25", "91"], ["T5", "92"]])
    logical = html_grid(
        "<table><tr><td></td><td>Score</td></tr>"
        "<tr><td>BM25</td><td>91</td></tr>"
        "<tr><td>T5</td><td>92</td></tr></table>"
    )
    result = consensus(page, candidate, build(page, candidate), {"engine": logical})
    assert result["grid"]["verified"]
    logical[0]["model_text"] = "Name"
    logical[1]["model_text"] = ""
    page, candidate = source([["Name", ""], ["BM25", "91"], ["T5", "92"]])
    result = consensus(page, candidate, build(page, candidate), {"engine": logical})
    assert not result["grid"]["verification"]["checks"]["header_value_associations"]["passed"]


def test_decoded_text_is_recomputed_for_source_reference():
    from app.tables.verify import verify

    page, candidate = source([["Name", "Score"], ["a", "91"], ["b", "92"]])
    page["font_aware"] = True
    glyph = page["chars"][-1]
    glyph["fontname"] = "TEST+CMR10"
    glyph["decoded_from"] = chr(0x5700 + ord("2"))
    raw = list(page["raw_text"])
    raw[glyph["offset"]] = glyph["decoded_from"]
    page["raw_text"] = "".join(raw)
    logical = html_grid(
        "<table><tr><td>Name</td><td>Score</td></tr>"
        "<tr><td>a</td><td>91</td></tr><tr><td>b</td><td>92</td></tr></table>"
    )
    grid = vote(build(page, candidate), logical, "engine")
    assert verify(page, candidate, grid)["verification"]["checks"]["source_references"]["passed"]
    glyph["text"] = "3"
    assert not verify(page, candidate, grid)["verification"]["checks"]["source_references"][
        "passed"
    ]
