"""Offline precision and integrator contract tests; no inference."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_tables_fix5 import baseline

from app.tables.chars import deduplicate_glyphs, display_text
from app.tables.frozen import run
from app.tables.frozen_packets import build as packets
from app.tables.io import read, write
from app.tables.precision import precision_checks
from app.tables.verify import verify


def glyph(i, text, x, y=0, width=5, height=10):
    return {
        "id": i,
        "text": text,
        "offset": i,
        "font_size_pt": 10,
        "bbox_pt": [x, y, x + width, y + height],
    }


@pytest.mark.parametrize("text", ["000", "ll", "……"])
def test_fake_bold_keeps_real_repeats(text):
    chars = [glyph(i, t, i * 5) for i, t in enumerate(text)]
    chars.insert(1, glyph(99, text[0], 0.3, 0.1))
    result = deduplicate_glyphs(chars)
    assert result[1]["duplicate_of"] == 0
    assert "".join(g["text"] for g in result if "duplicate_of" not in g) == text
    assert deduplicate_glyphs(result) == result


def test_fake_bold_excluded_from_ownership_and_coverage():
    page, candidate, grid = baseline()
    char = deepcopy(page["chars"][0])
    char.update(id=999, offset=len(page["raw_text"]))
    page["raw_text"] += char["text"]
    char["bbox_pt"] = [v + 0.2 for v in char["bbox_pt"]]
    page["chars"].append(char)
    result = verify(page, candidate, grid)
    assert result["verified"]
    assert result["duplicate_glyph_count"] == 1
    assert all(r["char_id"] != 999 for c in result["cells"] for r in c["char_refs"])


def test_tall_arrow_in_parentheses():
    chars = [glyph(0, "(", 0), glyph(1, ")", 10), glyph(2, "↑", 5, -7, height=27)]
    assert display_text(chars, "()↑")[0] == "(↑)"


def cell(row, col, text="", right=80):
    return {
        "row": row,
        "col": col,
        "rowspan": 1,
        "colspan": 1,
        "text": text,
        "bbox_pt": [0, row * 15, right, row * 15 + 15],
    }


def test_hidden_gutter_needs_two_rows_each_side_and_ignores_spans():
    cells = [cell(0, 0), cell(1, 0)]
    owners = [[glyph(0, "a", 0), glyph(1, "b", 20)], [glyph(2, "a", 0, 15), glyph(3, "b", 20, 15)]]
    assert precision_checks(cells, owners)["column_hides_a_gutter"]
    cells[0]["colspan"] = 2
    assert not precision_checks(cells, owners)["column_hides_a_gutter"]


def test_wrapped_prose_and_baselines():
    cells = [cell(0, 0, "this is a long unfinished", right=26), cell(1, 0, "sentence continues")]
    owners = [[glyph(0, "d", 20)], [glyph(1, "s", 0, 15)]]
    assert precision_checks(cells, owners)["wrapped_text_split_across_rows"]
    cells[0]["text"] += "."
    assert not precision_checks(cells, owners)["wrapped_text_split_across_rows"]
    cells = [cell(0, 0), cell(0, 1)]
    assert precision_checks(cells, [[glyph(0, "a", 0)], [glyph(1, "b", 20, 5)]])[
        "row_baselines_misaligned"
    ]
    assert not precision_checks(cells, [[glyph(0, "a", 0)], [glyph(1, "b", 20, 3)]])[
        "row_baselines_misaligned"
    ]


def test_rowspan_label_lines_on_separate_value_rows():
    label = dict(cell(0, 0), rowspan=2)
    cells = [label, cell(0, 1, "58.26"), cell(1, 1, "67.93")]
    owners = [
        [glyph(0, "G", 0), glyph(1, "+", 0, 15)],
        [glyph(2, "5", 40)],
        [glyph(3, "6", 40, 15)],
    ]
    assert precision_checks(cells, owners)["rowspan_cell_hides_row_boundary"]
    # A two-line header label over non-numeric header rows is exempt.
    cells[1]["text"], cells[2]["text"] = "Zero-shot", "Sim."
    assert not precision_checks(cells, owners)["rowspan_cell_hides_row_boundary"]
    # A single centred label over two value rows is fine.
    cells[1]["text"], cells[2]["text"] = "58.26", "67.93"
    owners[0] = [glyph(0, "G", 0, 7)]
    assert not precision_checks(cells, owners)["rowspan_cell_hides_row_boundary"]


def test_frozen_selection_not_dev_guarded(tmp_path):
    selection = tmp_path / "arbitrary-selection.json"
    write(
        selection,
        {
            "split": "synthetic",
            "papers": [{"paper": "paper", "pdf": "fake.pdf", "language": "en", "pages": [1]}],
        },
    )
    output = tmp_path / "run"
    seen = []

    def pipeline(args):
        seen.append(args)
        write(output / "run.json", {"pages": [], "elapsed_seconds": 2, "resources": {}})
        return {"ok": True}

    args = SimpleNamespace(
        selection=selection,
        paper="paper",
        output=output,
        enable_model=True,
        mineru_dir=Path("saved"),
        render_python=Path("render"),
        paddle_python=Path("paddle"),
        paddle_cache=Path("cache"),
        timeout=3,
    )
    assert run(args, pipeline) == {"ok": True}
    assert seen[0].engines == ["mineru", "ppstructure"]
    assert seen[0].page_png_dir is None
    assert seen[0].split == "synthetic"
    args.enable_model = False
    with pytest.raises(ValueError):
        run(args, pipeline)


def test_page_packets_include_caption_without_candidate(tmp_path):
    from PIL import Image

    root = tmp_path / "paper"
    write(
        root / "pages/001.json",
        {
            "width_pt": 100,
            "height_pt": 100,
            "chars": [glyph(i, t, i * 5) for i, t in enumerate("Table 1:")],
        },
    )
    Image.new("RGB", (100, 100), "white").save(root / "pages/001.png")
    write(
        root / "run.json",
        {
            "paper": "secret",
            "status": "completed",
            "pages": [{"page": 1, "png": "pages/001.png", "tables": []}],
        },
    )
    out = tmp_path / "judge"
    assert packets([root], out) == {"tables": 0, "pages": 1}
    assert "secret" not in (out / "packets/manifest.json").read_text()
    assert read(out / "private/blind-map.json")


def test_math_arrow_shifted_equal_height():
    chars = [glyph(0, "(", 0, 0), glyph(1, "?", 5, 6.7), glyph(2, ")", 10, 0)]
    assert display_text(chars, "( ?)")[0] == "(?)"


def test_frozen_complete_pipeline_synthetic(tmp_path):
    from PIL import Image
    from test_tables import fixture_table, source_html

    from app.tables.pilot import run_pilot
    from app.tables.resources import Resources

    page, candidate, grid = fixture_table()
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"synthetic-pdf-extraction-is-injected")
    mineru_dir = tmp_path / "mineru"
    box = [10 / 300, 0.1, 250 / 300, 0.55]
    write(
        mineru_dir / "structured_content.json",
        {
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {"type": "table", "bbox": box, "content": "markdown must be replaced"}
                    ],
                }
            ]
        },
    )
    write(
        mineru_dir / "middle_json.json",
        {
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "table",
                            "bbox": box,
                            "content": [{"type": "table_body", "content": source_html(grid)}],
                        }
                    ],
                }
            ]
        },
    )
    selection = tmp_path / "selection.json"
    write(
        selection,
        {
            "split": "synthetic",
            "papers": [{"paper": "p", "pdf": str(pdf), "language": "en", "pages": [1]}],
        },
    )
    calls = []

    def render(command, **kwargs):
        calls.append("render")
        directory = Path(command[3])
        directory.mkdir(parents=True)
        Image.new("RGB", (600, 400), "white").save(directory / "001.png")

    def paddle(*args, **kwargs):
        calls.append("paddle")
        write(kwargs["output"], {"pred_html": source_html(grid)})
        return deepcopy(grid)

    def pipeline(args):
        return run_pilot(
            args,
            extract=lambda *_: iter([page]),
            detector=lambda _: [candidate],
            paddle=paddle,
            runner=render,
            resources=lambda: Resources(sampler=lambda _: {}, interval=10),
        )

    args = SimpleNamespace(
        selection=selection,
        paper="p",
        output=tmp_path / "out",
        enable_model=True,
        mineru_dir=mineru_dir,
        render_python=Path("fake-render"),
        paddle_python=Path("fake-paddle"),
        paddle_cache=tmp_path / "cache",
        timeout=3,
    )
    assert run(args, pipeline)["tables"] == 1
    assert calls == ["render", "paddle"]
    manifest = read(args.output / "run.json")
    final = read(args.output / manifest["pages"][0]["tables"][0]["final"])
    assert set(final["paths"]) == {
        "old/mineru",
        "old/ppstructure",
        "glyph/mineru",
        "glyph/ppstructure",
    }
    assert final["grid"]["verified"]
    assert final["frozen_timing"]["end_to_end_amortized_seconds"] > 0
    assert packets([args.output], tmp_path / "packets") == {"tables": 1, "pages": 1}


def test_windows_shared_gpu_counter_is_process_scoped(monkeypatch):
    import json
    import os

    from app.tables import resources

    if os.name != "nt":
        pytest.skip("Windows counters")

    def runner(command, **kwargs):
        if command[0] != "powershell":
            raise OSError("no nvidia-smi")
        assert "GPU Process Memory" in command[-1]
        return SimpleNamespace(
            stdout=json.dumps(
                [
                    {"InstanceName": f"pid_{os.getpid()}_luid_0", "CookedValue": 123},
                    {"InstanceName": "pid_99999999_luid_0", "CookedValue": 999},
                ]
            )
        )

    monkeypatch.setattr(resources, "process_tree_bytes", lambda *a, **kw: 123)
    monkeypatch.setattr(resources, "process_ids", lambda pid, run: {pid})
    assert resources.observe(os.getpid(), runner)["shared_gpu_memory_bytes"] == 123
