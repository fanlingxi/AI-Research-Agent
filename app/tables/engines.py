"""Structure-only adapters. Engine text is diagnostic, never source text."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from html.parser import HTMLParser
from pathlib import Path
from statistics import median

from .chars import compact, deduplicate_glyphs, reading_order, text_lines, watermark_glyph
from .geometry import contains, crop_box_to_points, overlap, union
from .io import read


def read_mineru(path):
    """Accept one structured file or the existing per-paper/per-page output directory."""
    path = Path(path)
    files = sorted(path.rglob("structured_content.json")) if path.is_dir() else [path]
    if not files:
        raise ValueError("No MinerU structured_content.json files")
    pages = {}
    for file in files:
        bodies = middle_table_bodies(file.parent / "middle_json.json")
        for page in read(file)["pages"]:
            index = page["page_idx"]
            if index in pages:
                raise ValueError(f"Duplicate MinerU page_idx {index}; select a single run")
            for block in page["blocks"]:
                # structured_content renders span-free tables as Markdown; the HTML is in middle.
                key = (index, tuple(block.get("bbox") or ()))
                if block["type"] == "table" and key in bodies:
                    block["content"] = bodies[key]
            pages[index] = page
    return {"pages": [pages[k] for k in sorted(pages)]}


def middle_table_bodies(path):
    """table_body HTML keyed by (page_idx, table bbox) from a sibling middle_json.json."""
    if not path.exists():
        return {}
    bodies = {}
    for page in read(path)["pages"]:
        for block in page.get("blocks", []):
            if block.get("type") != "table":
                continue
            html = [
                part["content"]
                for part in block.get("content", [])
                if isinstance(part, dict)
                and part.get("type") == "table_body"
                and re.search(r"<table\b", str(part.get("content", "")), re.I)
            ]
            if len(html) == 1:
                bodies[page["page_idx"], tuple(block.get("bbox") or ())] = html[0]
    return bodies


class GridHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells, self.occupied = [], set()
        self.row, self.col, self.current, self.depth = -1, 0, None, 0
        self.in_head = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            if self.depth > 1 or self.cells:
                raise ValueError("Expected exactly one non-nested table")
        elif tag == "thead":
            self.in_head = True
        elif tag == "tr":
            if self.current is not None:
                raise ValueError("Unclosed cell")
            self.row += 1
            self.col = 0
        elif tag in {"td", "th"}:
            if self.depth != 1 or self.row < 0 or self.current is not None:
                raise ValueError("Invalid HTML cell placement")
            attrs = dict(attrs)
            rs, cs = int(attrs.get("rowspan", "1")), int(attrs.get("colspan", "1"))
            if not 1 <= rs <= 1000 or not 1 <= cs <= 1000:
                raise ValueError("Invalid HTML span")
            while (self.row, self.col) in self.occupied:
                self.col += 1
            slots = {
                (r, c)
                for r in range(self.row, self.row + rs)
                for c in range(self.col, self.col + cs)
            }
            if slots & self.occupied:
                raise ValueError("Overlapping HTML spans")
            self.occupied |= slots
            self.current = {
                "row": self.row,
                "col": self.col,
                "rowspan": rs,
                "colspan": cs,
                "model_text": "",
                "header_tag": tag == "th" or self.in_head,
            }
            self.col += cs
        elif tag == "br" and self.current is not None:
            self.current["model_text"] += "\n"

    def handle_data(self, data):
        if self.current is not None:
            self.current["model_text"] += data

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            if self.current is None:
                raise ValueError("Unexpected cell end")
            self.cells.append(self.current)
            self.current = None
        elif tag == "thead":
            self.in_head = False
        elif tag == "table":
            self.depth -= 1


def html_grid(html):
    parser = GridHTML()
    parser.feed(html)
    parser.close()
    if parser.current is not None or parser.depth != 0 or not parser.cells:
        raise ValueError("Incomplete or empty table HTML")
    return parser.cells


def ppstructure(
    page_png,
    candidate,
    page_size_pt,
    *,
    python,
    cache_home,
    output,
    timeout=300,
    run=subprocess.run,
    worker=None,
    chars=None,
):
    if worker is not None:
        raw = worker.request(
            {
                "page_png": str(Path(page_png).resolve()),
                "bbox_pt": candidate["bbox_pt"],
                "page_size_pt": page_size_pt,
            },
            timeout,
        )
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return pp_grid(raw, page_size_pt, candidate, chars)
    output = Path(output)
    request = output.with_suffix(".request.json")
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text(
        json.dumps(
            {
                "page_png": str(Path(page_png).resolve()),
                "bbox_pt": candidate["bbox_pt"],
                "page_size_pt": page_size_pt,
            }
        ),
        encoding="utf-8",
    )
    env = dict(os.environ, PADDLE_PDX_CACHE_HOME=str(cache_home))
    run(
        [
            str(python),
            str(Path(__file__).with_name("paddle_worker.py")),
            "--request",
            str(request.resolve()),
            "--output",
            str(output.resolve()),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
    )
    raw = json.loads(output.read_text(encoding="utf-8"))
    return pp_grid(raw, page_size_pt, candidate, chars)


def pp_grid(raw, page_size_pt, candidate, chars=None):
    cells = html_grid(raw["pred_html"])
    boxes = [
        crop_box_to_points(b, raw["crop_px"], page_size_pt, raw["image_size_px"])
        for b in raw["cell_box_list"]
    ]
    if len(boxes) == len(cells) and rank_consistent(cells, boxes):
        for cell, box in zip(cells, boxes, strict=True):
            cell["bbox_pt"] = box
    else:
        # Infer logical bands from detector extents, then anchor them to source gutters.
        nr = max(c["row"] + c["rowspan"] for c in cells)
        nc = max(c["col"] + c["colspan"] for c in cells)
        edges = []
        for axis, count in ((0, nc), (1, nr)):
            values = sorted(v for box in boxes for v in (box[axis], box[axis + 2]))
            bands = sorted(set(values))
            if len(bands) != count + 1:
                if not chars:
                    raise ValueError("Engine alignment failure: source gutters required")
                # Fit edge ranks, then select actual whitespace gaps from source glyphs.
                centres = [values[round(i * (len(values) - 1) / count)] for i in range(count + 1)]
                for _ in range(20):
                    groups = [[] for _ in centres]
                    for value in values:
                        groups[
                            min(range(len(centres)), key=lambda j: abs(value - centres[j]))
                        ].append(value)
                    centres = [median(g) if g else c for g, c in zip(groups, centres, strict=True)]
                bands = [candidate["bbox_pt"][axis]]
                intervals = sorted((g["bbox_pt"][axis], g["bbox_pt"][axis + 2]) for g in chars)
                gaps, end = [], intervals[0][1]
                for low, high in intervals[1:]:
                    if low > end:
                        gaps.append((end, low))
                    end = max(end, high)
                for centre in centres[1:-1]:
                    if not gaps:
                        raise ValueError("Engine alignment failure: no glyph gutters")
                    low, high = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - centre))
                    bands.append(min(max(centre, low + 0.001), high - 0.001))
                bands.append(candidate["bbox_pt"][axis + 2])
                if any(a >= b for a, b in zip(bands, bands[1:], strict=False)):
                    raise ValueError("Engine alignment failure: ambiguous glyph gutters")
            edges.append(bands)
        xs, ys = edges
        unused = set(range(len(boxes)))
        for cell in cells:
            r, c = cell["row"], cell["col"]
            expected = [xs[c], ys[r], xs[c + cell["colspan"]], ys[r + cell["rowspan"]]]
            matches = [
                i
                for i in unused
                if contains(
                    expected,
                    [
                        (boxes[i][0] + boxes[i][2]) / 2 - 0.01,
                        (boxes[i][1] + boxes[i][3]) / 2 - 0.01,
                        (boxes[i][0] + boxes[i][2]) / 2 + 0.01,
                        (boxes[i][1] + boxes[i][3]) / 2 + 0.01,
                    ],
                )
            ]
            if len(matches) == 1:
                cell["bbox_pt"] = boxes[matches[0]]
                unused.remove(matches[0])
            elif not matches and not compact(cell["model_text"]):
                cell["bbox_pt"] = expected
            else:
                raise ValueError(f"Engine alignment failure at r{r}c{c}: {len(matches)} boxes")
        if unused:
            raise ValueError("Engine alignment failure: unmatched boxes")
    return {
        "engine": "ppstructure",
        "cells": cells,
        "errors": [],
        "crosscheck": raw.get("table_ocr_pred"),
        "worker_resources": raw.get("resources"),
        "cold_start_seconds": raw.get("cold_start_seconds", 0),
    }


def rank_consistent(cells, boxes):
    for i, (a, ab) in enumerate(zip(cells, boxes, strict=True)):
        for b, bb in zip(cells[i + 1 :], boxes[i + 1 :], strict=True):
            if a["row"] == b["row"] and a["col"] < b["col"]:
                if ab[0] + ab[2] >= bb[0] + bb[2]:
                    return False
            if a["row"] + a["rowspan"] <= b["row"]:
                if ab[1] + ab[3] >= bb[1] + bb[3]:
                    return False
            for axis, key, span in ((0, "col", "colspan"), (1, "row", "rowspan")):
                if a[span] > 1 and a[key] <= b[key] and b[key] + b[span] <= a[key] + a[span]:
                    centre = (bb[axis] + bb[axis + 2]) / 2
                    if not ab[axis] <= centre <= ab[axis + 2]:
                        return False
    return True


def region_chars(page, candidate):
    return reading_order(
        [
            c
            for c in deduplicate_glyphs(page["chars"])
            if "duplicate_of" not in c
            and not c["text"].isspace()
            and not watermark_glyph(c)
            and not any(contains(b, c["bbox_pt"]) for b in candidate.get("note_bboxes_pt", []))
            and contains(candidate["bbox_pt"], c["bbox_pt"])
            and not (
                candidate.get("caption_bbox_pt")
                and contains(candidate["caption_bbox_pt"], c["bbox_pt"])
            )
        ]
    )


def infer_boxes(cells, region):
    """Infer separators only in observed whitespace between matched logical cells."""
    boundaries = []
    for axis, key, span in ((0, "col", "colspan"), (1, "row", "rowspan")):
        count = max(c[key] + c[span] for c in cells)
        edges = [region[axis]]
        for index in range(1, count):
            before = [
                c["text_bbox_pt"][axis + 2]
                for c in cells
                if c[key] + c[span] == index and "text_bbox_pt" in c
            ]
            after = [
                c["text_bbox_pt"][axis] for c in cells if c[key] == index and "text_bbox_pt" in c
            ]
            if not before or not after or max(before) >= min(after):
                raise ValueError(f"No grounded {key} separator at {index}")
            edges.append((max(before) + min(after)) / 2)
        edges.append(region[axis + 2])
        boundaries.append(edges)
    xs, ys = boundaries
    for cell in cells:
        r, c = cell["row"], cell["col"]
        cell["bbox_pt"] = [xs[c], ys[r], xs[c + cell["colspan"]], ys[r + cell["rowspan"]]]
        if "text_bbox_pt" in cell and not contains(cell["bbox_pt"], cell["text_bbox_pt"]):
            raise ValueError("Inferred cell does not contain matched characters")


def mineru(html, page, candidate):
    if not html or not re.search(r"<table\b", html, re.I):
        raise ValueError("engine produced no table structure")
    cells = html_grid(html)
    chars = region_chars(page, candidate)
    errors = []
    remaining = list(chars)
    # Search exact contiguous line runs; a wrapped cell continues only in its
    # own horizontal segment on subsequent lines. Never substitute source text.
    previous_row_bottom = candidate["bbox_pt"][1]
    source_lines = text_lines(chars, segments=False)
    for row in sorted({c["row"] for c in cells}):
        row_cells = [c for c in cells if c["row"] == row]
        matched_row = []
        for cell in row_cells:
            target = compact(cell["model_text"])
            if not target:
                continue
            options = []
            remaining_ids = {g["id"] for g in remaining}
            lines = [[g for g in line if g["id"] in remaining_ids] for line in source_lines]
            lines = [line for line in lines if line]
            for li, line in enumerate(lines):
                for start in range(len(line)):
                    if line[start]["text"] != target[0]:
                        continue
                    run, pending = [], target
                    x0 = line[start]["bbox_pt"][0]
                    for next_line in lines[li:]:
                        segment = [g for g in next_line if g["bbox_pt"][0] >= x0 - 2]
                        if next_line is line:
                            segment = line[start:]
                        for g in segment:
                            if not pending or g["text"] != pending[0]:
                                break
                            run.append(g)
                            pending = pending[1:]
                        if not pending:
                            break
                    if not pending and min(g["bbox_pt"][1] for g in run) >= previous_row_bottom - 2:
                        options.append(run)
            unique = {tuple(g["id"] for g in run): run for run in options}
            if not unique:
                return {
                    "engine": "mineru",
                    "cells": cells,
                    "errors": [
                        f"Unaligned cell r{cell['row']}c{cell['col']}",
                        "Region contains characters absent from HTML",
                    ],
                }
            run = min(unique.values(), key=lambda run: (run[0]["bbox_pt"][1], run[0]["bbox_pt"][0]))
            cell["text_bbox_pt"] = union(g["bbox_pt"] for g in run)
            cell["aligned_char_ids"] = [g["id"] for g in run]
            consumed = set(cell["aligned_char_ids"])
            remaining = [g for g in remaining if g["id"] not in consumed]
            if cell["rowspan"] == 1:
                matched_row.extend(run)
        if matched_row:
            previous_row_bottom = max(g["bbox_pt"][3] for g in matched_row)
    if remaining:
        errors.append("Region contains characters absent from HTML")
    if not errors:
        try:
            infer_boxes(cells, candidate["bbox_pt"])
        except ValueError as exc:
            errors.append(str(exc))
    return {"engine": "mineru", "cells": cells, "errors": errors}


def mineru_html(structured, page, candidate):
    """Read MinerU 4 structured_content; bbox units are normalized [0, 1]."""
    matches, overlaps = [], []
    for source in structured["pages"]:
        if source["page_idx"] + 1 != page["page"]:
            continue
        for block in source["blocks"]:
            if block["type"] != "table":
                continue
            b = block["bbox"]
            if max(b) <= 1.01:
                sx, sy = page["width_pt"], page["height_pt"]
            elif source.get("width") and source.get("height"):
                sx, sy = page["width_pt"] / source["width"], page["height_pt"] / source["height"]
            elif max(b) <= 1000 and (b[2] > page["width_pt"] or b[3] > page["height_pt"]):
                sx, sy = page["width_pt"] / 1000, page["height_pt"] / 1000
            else:
                sx = sy = 1
            box = [b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy]
            score = overlap(box, candidate["bbox_pt"])
            overlaps.append(round(score, 4))
            if score >= 0.5:
                content = block.get("content", "")
                if not isinstance(content, str):
                    raise ValueError("Expected MinerU table HTML string")
                matches.append(content)
    if len(matches) != 1:
        raise ValueError(
            f"Expected one matching MinerU table, got {len(matches)}; overlaps={overlaps}"
        )
    if not re.search(r"<table\b", matches[0], re.I):
        raise ValueError("engine produced no table structure")
    return matches[0]


class PaddleSession:
    """Lazy one-paper JSON-lines subprocess, with bounded waits and explicit cleanup."""

    def __init__(self, python, cache_home, log):
        self.python, self.cache_home, self.log = python, cache_home, Path(log)
        self.process = None
        self.messages = queue.Queue()
        self.log_file = None

    def __enter__(self):
        return self

    def request(self, request, timeout):
        if self.process is None:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            self.log_file = self.log.open("w", encoding="utf-8")
            self.process = subprocess.Popen(
                [str(self.python), str(Path(__file__).with_name("paddle_worker.py")), "--stream"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.log_file,
                text=True,
                encoding="utf-8",
                env=dict(os.environ, PADDLE_PDX_CACHE_HOME=str(self.cache_home)),
            )

            def receive():
                for line in self.process.stdout:
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        self.log_file.write(line)
                        continue
                    if isinstance(payload, dict) and ("pred_html" in payload or "error" in payload):
                        self.messages.put(line)
                    else:
                        self.log_file.write(line)
                self.messages.put(None)

            threading.Thread(target=receive, daemon=True).start()
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        try:
            line = self.messages.get(timeout=timeout)
        except queue.Empty:
            self.__exit__(None, None, None)
            raise TimeoutError("Paddle paper worker timed out") from None
        if line is None:
            raise RuntimeError("Paddle paper worker exited; inspect paddle-worker.log")
        result = json.loads(line)
        if result.get("error"):
            raise ValueError(result["error"])
        return result

    def __exit__(self, *_):
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
        if self.log_file:
            self.log_file.close()
