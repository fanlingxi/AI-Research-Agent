"""Offline glyph-level regression cases from the fix-4 brief."""

from copy import deepcopy

from test_tables_fix3 import source

from app.tables.engines import html_grid
from app.tables.glyphgrid import build
from app.tables.verify import verify
from app.tables.vote import consensus, vote


def test_centred_header_without_rule_and_two_line_stub():
    page, candidate = source(
        [
            ["Long", "", "Group", ""],
            ["Stub", "A", "B", "C"],
            ["one", "1", "2", "3"],
            ["two", "4", "5", "6"],
        ],
        [0, 40, 80, 120, 160],
    )
    logical = html_grid(
        '<table><tr><td rowspan="2">Long Stub</td><td colspan="3">Group</td></tr>'
        "<tr><td>A</td><td>B</td><td>C</td></tr>"
        "<tr><td>one</td><td>1</td><td>2</td><td>3</td></tr>"
        "<tr><td>two</td><td>4</td><td>5</td><td>6</td></tr></table>"
    )
    result = consensus(page, candidate, build(page, candidate), {"engine": logical})
    assert result["grid"]["verified"]
    assert result["grid"]["cells"][0]["text"] == "Long\nStub"


def test_section_labels_become_row_header_path():
    page, candidate = source(
        [["Name", "Score"], ["Group", ""], ["a", "1"], ["Next", ""], ["b", "2"]]
    )
    logical = html_grid(
        "<table><tr><td>Name</td><td>Score</td></tr>"
        '<tr><td colspan="2">Group</td></tr><tr><td>a</td><td>1</td></tr>'
        "<tr><td>Next</td><td></td></tr><tr><td>b</td><td>2</td></tr></table>"
    )
    grid = consensus(page, candidate, build(page, candidate), {"engine": logical})["grid"]
    assert grid["verified"]
    paths = grid["verification"]["checks"]["header_value_associations"]["value"]["associations"]
    assert [p["row_headers"][0] for p in paths] == [2, 5]


def test_partial_engine_row_merge_rejected():
    page, candidate = source([["Name", "Score"], ["a", "91"], ["b", "92"]])
    logical = html_grid(
        "<table><tr><td>Name</td><td>Score</td></tr>"
        '<tr><td>a</td><td rowspan="2">91 92</td></tr>'
        "<tr><td>b</td></tr></table>"
    )
    assert not vote(build(page, candidate), logical, "engine")["vote_passed"]


def test_body_shift_fails_physical_header_sanity():
    page, candidate = source(
        [["Name", "A", "B"], ["one", "1", "2"], ["two", "3", "4"]], [0, 40, 80, 120]
    )
    logical = html_grid(
        "<table><tr><td>Name</td><td>A</td><td>B</td></tr>"
        "<tr><td>one</td><td>1</td><td>2</td></tr>"
        "<tr><td>two</td><td>3</td><td>4</td></tr></table>"
    )
    grid = vote(build(page, candidate), logical, "engine")
    assert grid["vote_passed"]
    # Move the whole body one physical column, preserving its text and topology.
    shifted = deepcopy(page)
    for g in shifted["chars"]:
        if g["bbox_pt"][1] > 15:
            g["bbox_pt"][0] += 40
            g["bbox_pt"][2] += 40
    for cell in grid["cells"]:
        if cell["row"] > 0:
            cell["bbox_pt"][0] += 40
            cell["bbox_pt"][2] += 40
    checked = verify(shifted, dict(candidate, bbox_pt=[0, 0, 160, 45]), grid)
    assert not checked["verification"]["checks"]["physical_column_sanity"]["passed"]
    assert not checked["verified"]
