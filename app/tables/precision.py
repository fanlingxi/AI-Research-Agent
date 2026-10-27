"""Conservative source geometry checks for hidden columns and prose rows."""

import re
from statistics import median

from .chars import text_lines

BASELINE_TOLERANCE = 0.35
MIN_GUTTER_PT = 3.0
NUMERIC = re.compile(r"[-+−–]?[\d.,]+%?")


def precision_checks(cells, owners):
    hidden, wrapped, misaligned = [], [], []
    for col in sorted({c["col"] for c in cells}):
        glyphs = [
            (g, c["row"])
            for c, gs in zip(cells, owners, strict=True)
            if c["col"] == col and c["colspan"] == 1 and c["rowspan"] == 1
            for g in gs
        ]
        intervals = sorted((g["bbox_pt"][0], g["bbox_pt"][2]) for g, _ in glyphs)
        end = None
        for left, right in intervals:
            if end is not None and left - end >= MIN_GUTTER_PT:
                before = {r for g, r in glyphs if g["bbox_pt"][2] <= end}
                after = {r for g, r in glyphs if g["bbox_pt"][0] >= left}
                if len(before) >= 2 and len(after) >= 2:
                    hidden.append(
                        {
                            "col": col,
                            "gap": [end, left],
                            "left_rows": sorted(before),
                            "right_rows": sorted(after),
                        }
                    )
            end = max(end if end is not None else right, right)
    lines = [text_lines(gs, segments=False, cell=True) for gs in owners]
    for row in sorted({c["row"] for c in cells}):
        singles = [
            (
                i,
                median(g["bbox_pt"][3] for g in ls[0]),
                median(g["bbox_pt"][3] - g["bbox_pt"][1] for g in ls[0]),
            )
            for i, (c, ls) in enumerate(zip(cells, lines, strict=True))
            if c["row"] == row and c["rowspan"] == 1 and len(ls) == 1
        ]
        for a, b in zip(singles, singles[1:], strict=False):
            if abs(a[1] - b[1]) > BASELINE_TOLERANCE * max(a[2], b[2]):
                misaligned.append({"row": row, "cells": [a[0], b[0]], "delta": abs(a[1] - b[1])})
    lookup = {
        (c["row"], c["col"]): i for i, c in enumerate(cells) if c["rowspan"] == c["colspan"] == 1
    }
    for (row, col), i in lookup.items():
        j = lookup.get((row + 1, col))
        if j is None or not lines[i] or not lines[j]:
            continue
        previous, following = cells[i]["text"].rstrip(), cells[j]["text"].lstrip()
        if not previous or not following:
            continue
        last = lines[i][-1]
        size = median(g.get("font_size_pt", g["bbox_pt"][3] - g["bbox_pt"][1]) for g in last)
        reaches = cells[i]["bbox_pt"][2] - max(g["bbox_pt"][2] for g in last) <= 1.5 * size
        prose = len(previous.split()) >= 4
        continuation = following[0].islower() or "\u4e00" <= following[0] <= "\u9fff"
        if (
            previous.endswith("-") or (reaches and prose and previous[-1] not in ".!?;:。！？；：")
        ) and continuation:
            wrapped.append({"cells": [i, j], "previous": previous, "next": following})
    return {
        "column_hides_a_gutter": hidden,
        "wrapped_text_split_across_rows": wrapped,
        "row_baselines_misaligned": misaligned,
        "rowspan_cell_hides_row_boundary": hidden_rows(cells, lines),
    }


def hidden_rows(cells, lines):
    """A multi-line row-spanning cell whose lines sit on different value rows is really
    several cells (`GPT-3.5` / `+本文方法` merged over two result rows). Only rows that carry
    numeric single-line values are considered, so multi-line header labels are exempt."""

    def baseline(line):
        return median(g["bbox_pt"][3] for g in line), median(
            g["bbox_pt"][3] - g["bbox_pt"][1] for g in line
        )

    found = []
    for i, cell in enumerate(cells):
        if cell["rowspan"] < 2 or len(lines[i]) < 2:
            continue
        rows = {}
        for row in range(cell["row"], cell["row"] + cell["rowspan"]):
            values = [
                baseline(lines[j][0])
                for j, other in enumerate(cells)
                if other["row"] == row
                and other["rowspan"] == 1
                and len(lines[j]) == 1
                and NUMERIC.fullmatch(other["text"].strip())
            ]
            if not values:
                break
            rows[row] = (median(v[0] for v in values), median(v[1] for v in values))
        else:
            matched = set()
            for line in lines[i]:
                bottom, height = baseline(line)
                matched |= {
                    row
                    for row, (value, size) in rows.items()
                    if abs(bottom - value) <= BASELINE_TOLERANCE * max(height, size)
                }
            if len(matched) >= 2:
                found.append({"cell": i, "rows": sorted(matched)})
    return found
