"""Source-only physical line grid. Engine boxes and reading order are not inputs."""

from .chars import text_lines
from .engines import region_chars
from .geometry import contains

MIN_GUTTER_PT = 3.0
GUTTER_SUPPORT = 0.90


def build(page, candidate, *, min_width=MIN_GUTTER_PT, support=GUTTER_SUPPORT):
    chars = region_chars(page, candidate)
    region = candidate["bbox_pt"]
    rules = sorted(
        {
            (r["bbox_pt"][1] + r["bbox_pt"][3]) / 2
            for r in page.get("rules", [])
            if r["bbox_pt"][2] - r["bbox_pt"][0] >= 0.8 * (region[2] - region[0])
            and r["bbox_pt"][3] - r["bbox_pt"][1] < 2
            and region[1] <= r["bbox_pt"][1] <= region[3]
        }
    )
    # Rules are hard separators, including when adjacent superscripts overlap.
    bands = [[] for _ in range(len(rules) + 1)]
    for c in chars:
        y = (c["bbox_pt"][1] + c["bbox_pt"][3]) / 2
        bands[sum(y > rule for rule in rules)].append(c)
    lines = []
    for band in bands:
        grouped = []
        for line in text_lines(band, segments=False):
            if grouped and min(c["bbox_pt"][1] for c in line) < max(
                c["bbox_pt"][3] for c in grouped[-1]
            ):
                grouped[-1].extend(line)
                grouped[-1].sort(key=lambda c: c["bbox_pt"][0])
            else:
                grouped.append(line)
        lines.extend(grouped)
    if not lines:
        raise ValueError("No source line bands")
    mid = rules[1] if len(rules) >= 3 else None
    body = (
        [line for line in lines if min(c["bbox_pt"][1] for c in line) > mid] if mid else lines[1:]
    )
    if not body:
        raise ValueError("No body lines for gutters")
    endpoints = sorted(
        {v for line in body for c in line for v in (c["bbox_pt"][0], c["bbox_pt"][2])}
    )
    gaps = []
    for a, b in zip(endpoints, endpoints[1:], strict=False):
        x = (a + b) / 2
        fraction = sum(
            not any(c["bbox_pt"][0] < x < c["bbox_pt"][2] for c in line) for line in body
        ) / len(body)
        if fraction >= support:
            if gaps and abs(gaps[-1][1] - a) < 1e-6:
                gaps[-1][1] = b
            else:
                gaps.append([a, b])
    xs = (
        [region[0]]
        + [
            (a + b) / 2
            for a, b in gaps
            if b - a >= min_width
            and any(c["bbox_pt"][2] <= a for line in body for c in line)
            and any(c["bbox_pt"][0] >= b for line in body for c in line)
        ]
        + [region[2]]
    )
    ys = [region[1]]
    hard = []
    for i, (left, right) in enumerate(zip(lines, lines[1:], strict=False), 1):
        a = max(c["bbox_pt"][3] for c in left)
        b = min(c["bbox_pt"][1] for c in right)
        between = [r for r in rules if a <= r <= b]
        if a >= b:
            raise ValueError("Overlapping source line bands")
        ys.append(between[0] if between else (a + b) / 2)
        if between:
            hard.append(i)
    ys.append(region[3])
    # Segments spanning body columns are recorded as header colspans. Ownership
    # remains glyph based: never split a glyph at an internal grid boundary.
    headers = []
    for r, line in enumerate(lines):
        if mid is not None and min(c["bbox_pt"][1] for c in line) >= mid:
            break
        for segment in text_lines(line):
            low = min(c["bbox_pt"][0] for c in segment)
            high = max(c["bbox_pt"][2] for c in segment)
            bottom = max(c["bbox_pt"][3] for c in segment)
            confirms = [
                item["bbox_pt"]
                for item in page.get("rules", [])
                if item["bbox_pt"][3] - item["bbox_pt"][1] < 2
                and 0 <= item["bbox_pt"][1] - bottom <= 5
                and item["bbox_pt"][0] <= low
                and item["bbox_pt"][2] >= high
                and item["bbox_pt"][2] - item["bbox_pt"][0] < 0.8 * (region[2] - region[0])
            ]
            if len(confirms) == 1:
                low, high = confirms[0][0], confirms[0][2]
            first = max(0, sum(x <= low for x in xs) - 1)
            last = min(len(xs) - 2, sum(x < high for x in xs) - 1)
            confirmations = confirms
            headers.append(
                {"row": r, "col": first, "colspan": last - first + 1, "cmidrules": confirmations}
            )
    cells = []
    for r in range(len(lines)):
        spans = [
            (s["col"], s["col"] + s["colspan"])
            for s in headers
            if s["row"] == r and s["colspan"] > 1
        ]
        for glyph in lines[r]:
            low, high = glyph["bbox_pt"][0], glyph["bbox_pt"][2]
            first = max(0, sum(x <= low for x in xs) - 1)
            last = min(len(xs) - 2, sum(x < high for x in xs) - 1)
            if last > first:
                spans.append((first, last + 1))
        merged = []
        for a, b in sorted(spans):
            if merged and a < merged[-1][1]:
                merged[-1][1] = max(b, merged[-1][1])
            else:
                merged.append([a, b])
        col = 0
        while col < len(xs) - 1:
            end = next((b for a, b in merged if a == col), col + 1)
            cells.append(
                {
                    "row": r,
                    "col": col,
                    "rowspan": 1,
                    "colspan": end - col,
                    "bbox_pt": [xs[col], ys[r], xs[end], ys[r + 1]],
                }
            )
            glyphs = [g for g in lines[r] if contains(cells[-1]["bbox_pt"], g["bbox_pt"])]
            cells[-1]["text"] = "".join(
                g["text"] for g in sorted(glyphs, key=lambda g: g["bbox_pt"][0])
            )
            cells[-1]["char_ids"] = [g["id"] for g in glyphs]
            col = end
    return {
        "cells": cells,
        "xs": xs,
        "ys": ys,
        "lines": lines,
        "chars": chars,
        "shape": [len(lines), len(xs) - 1],
        "hard_rows": hard,
        "header_rows": sum(max(c["bbox_pt"][3] for c in line) < mid for line in lines)
        if mid
        else 1,
        "header_segments": headers,
        "minimum_gutter_pt": min_width,
        "gutter_support": support,
    }
