from copy import deepcopy

from test_tables_fix3 import source

from app.tables.chars import display_text
from app.tables.engines import html_grid
from app.tables.final import content, final_status
from app.tables.glyphgrid import build
from app.tables.vote import consensus


def baseline():
    page, candidate = source([["Name", "Score"], ["a", "1"], ["b", "2"]])
    logical = html_grid(
        "<table><tr><td>Name</td><td>Score</td></tr>"
        "<tr><td>a</td><td>1</td></tr>"
        "<tr><td>b</td><td>2</td></tr></table>"
    )
    grid = consensus(page, candidate, build(page, candidate), {"engine": logical})["grid"]
    return page, candidate, grid


def test_content_ignores_empty_cells():
    _, _, grid = baseline()
    other = deepcopy(grid)
    other["cells"].append({"text": "", "row": 8, "col": 9})
    assert content(grid) == content(other)
    other["cells"][3]["char_refs"][0]["char_id"] = 999
    assert content(grid) != content(other)


def test_final_checks_every_path_and_selects_best():
    page, candidate, grid = baseline()
    other = deepcopy(grid)
    other["agreement_score"] = 2
    result = final_status(page, candidate, {"low": grid, "high": other})
    assert result["grid"]["verified"]
    assert result["selected_path"] == "high"
    assert result["agreeing_paths"] == ["low", "high"]
    other["errors"] = ["failed"]
    assert final_status(page, candidate, {"bad": other})["agreeing_paths"] == []


def test_spacing_raw_and_fallback_decoded():
    page, _ = source([["AB", ""]])
    chars = page["chars"]
    chars[1]["offset"] = 2
    chars[0]["text"] = "decoded"
    assert display_text(chars, "A B") == ("decoded B", False)
    chars[1]["offset"] = None
    chars[1]["bbox_pt"] = [20, 3, 22, 11]
    assert display_text(chars, "A B") == ("decoded B", True)


def test_final_content_disagreement():
    page, candidate, grid = baseline()
    other = deepcopy(grid)
    other["header_rows"] = 2
    result = final_status(page, candidate, {"first": grid, "second": other})
    assert all(g["verified"] for g in result["paths"].values())
    assert result["engine_disagreement"]
    assert not result["grid"]["verified"]
    assert result["content_differences"]


def test_empty_span_agreement_in_final_rule(monkeypatch):
    import app.tables.final as rule

    page, candidate, grid = baseline()
    other = deepcopy(grid)
    other["cells"].extend(
        [
            {"text": "", "row": 6, "col": 0, "colspan": 1, "rowspan": 1},
            {"text": "", "row": 6, "col": 1, "colspan": 1, "rowspan": 1},
        ]
    )
    grid["cells"].append({"text": "", "row": 6, "col": 0, "colspan": 2, "rowspan": 1})
    # Isolate final comparison from geometry: both verifier results are passing.
    monkeypatch.setattr(rule, "verify", lambda page, candidate, grid, **kw: deepcopy(grid))
    result = rule.final_status(page, candidate, {"span": grid, "split": other})
    assert result["grid"]["verified"]
    assert result["agreeing_paths"] == ["span", "split"]


def test_two_judges_conservative_union(tmp_path):
    from app.tables.io import write
    from app.tables.judge_score import score

    manifest = tmp_path / "manifest.json"
    write(
        manifest,
        {
            "packets": [
                {
                    "packet_id": "t001",
                    "cell_count": 2,
                    "cells": [{"row": 0, "col": 0}, {"row": 0, "col": 1}],
                }
            ]
        },
    )
    directories = [tmp_path / "a", tmp_path / "b"]
    for directory, col in zip(directories, (0, 1), strict=True):
        write(
            directory / "t001.json",
            {
                "packet_id": "t001",
                "table_correct": False,
                "cell_errors": [{"row": 0, "col": col, "kind": "text", "note": "wrong"}],
            },
        )
    result = score(manifest, directories)
    assert result["cell_accuracy"]["success"] == 0
    assert result["cell_accuracy"]["total"] == 2
    assert result["whole_table_correct_rate"]["rate"] == 0
    assert result["cell_accuracy"]["ci95"] is not None
