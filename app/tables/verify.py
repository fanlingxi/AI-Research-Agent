"""Fail-closed D040 checks; HTML and cell text are always derived from source glyphs."""

from __future__ import annotations

import copy
import html
import unicodedata
from difflib import SequenceMatcher

from .chars import (
    compact,
    decode_fonts,
    deduplicate_glyphs,
    display_text,
    expansion_mappable,
    garbled_layer,
    text_lines,
    watermark_glyph,
)
from .geometry import contains, intersects, valid_box
from .snap import snap_cells

VERSION = "d040-f0-frozen-fix6b"


def geometry_checks(cells, tolerance=2.0, *, glyph_level=False):
    errors, slots, edges = [], {}, {"x": {}, "y": {}}
    if not cells:
        return ["Empty grid"], {}
    for index, cell in enumerate(cells):
        if any(type(cell.get(k)) is not int for k in ("row", "col", "rowspan", "colspan")):
            errors.append(f"Cell {index}: noninteger grid coordinates")
            continue
        r, c, rs, cs = [cell[k] for k in ("row", "col", "rowspan", "colspan")]
        b = cell.get("bbox_pt")
        if min(r, c) < 0 or min(rs, cs) < 1 or max(r + rs, c + cs) > 1000 or not valid_box(b):
            errors.append(f"Cell {index}: invalid span or bbox")
            continue
        for row in range(r, r + rs):
            for col in range(c, c + cs):
                if (row, col) in slots:
                    errors.append(f"Overlapping logical slot {row},{col}")
                slots[row, col] = index
        for axis, start, end, low, high in (
            ("x", c, c + cs, b[0], b[2]),
            ("y", r, r + rs, b[1], b[3]),
        ):
            edges[axis].setdefault(start, []).append(low)
            edges[axis].setdefault(end, []).append(high)
    if errors or not slots:
        return errors or ["Empty grid"], slots
    nr, nc = max(r for r, _ in slots) + 1, max(c for _, c in slots) + 1
    if len(slots) != nr * nc:
        errors.append("Missing logical slots")
    for axis, count in (("x", nc), ("y", nr)):
        if glyph_level and axis == "x":
            continue
        previous = None
        for edge in range(count + 1):
            values = edges[axis].get(edge)
            if not values:
                errors.append(f"Ungrounded {axis} boundary {edge}")
                continue
            if max(values) - min(values) > tolerance:
                errors.append(f"Inconsistent {axis} band/span at {edge}")
            if previous is not None and min(values) <= previous:
                errors.append(f"Reversed {axis} geometry at {edge}")
            previous = max(values)
    for i, a in enumerate(cells):
        for b in cells[i + 1 :]:
            if intersects(a["bbox_pt"], b["bbox_pt"]):
                errors.append("Overlapping physical cells")
    return errors, slots


def verify(page, candidate, grid, *, header_rows=None, row_header_cols=None):
    page = dict(page, chars=deduplicate_glyphs(page["chars"]))
    result = copy.deepcopy(grid)
    cells = result["cells"]
    checks, reasons = {}, []
    decoded_expected = (
        {
            c["id"]: c["text"]
            for c in decode_fonts(
                [dict(c, text=c.get("decoded_from", c["text"])) for c in page["chars"]]
            )
        }
        if any("decoded_from" in c for c in page["chars"])
        else {}
    )
    if "vote_topology" in result:
        actual = sorted((c["row"], c["col"], c["rowspan"], c["colspan"]) for c in cells)
        expected = [tuple(c) for c in result["vote_topology"]]
        if actual != expected:
            result.setdefault("errors", []).append("Topology differs from accepted engine vote")
    grid = result

    def check(name, value, passed, detail):
        checks[name] = {"value": value, "passed": bool(passed), "reason": "" if passed else detail}
        if not passed:
            reasons.append(detail)

    eligible = [
        c
        for c in page["chars"]
        if not c["text"].isspace()
        and (
            intersects(candidate["bbox_pt"], c["bbox_pt"])
            or contains(candidate["bbox_pt"], c["bbox_pt"])
        )
        and not any(contains(b, c["bbox_pt"]) for b in candidate.get("note_bboxes_pt", []))
        and not (
            candidate.get("caption_bbox_pt")
            and contains(candidate["caption_bbox_pt"], c["bbox_pt"])
        )
    ]
    result["duplicate_glyphs"] = [c for c in eligible if "duplicate_of" in c]
    result["duplicate_glyph_count"] = len(result["duplicate_glyphs"])
    eligible = [c for c in eligible if "duplicate_of" not in c]
    result["excluded_watermark_glyphs"] = [c for c in eligible if watermark_glyph(c)]
    eligible = [c for c in eligible if not watermark_glyph(c)]
    # Validate logical topology before snapping; never repair corrupt row/column indices.
    initial_errors, slots = geometry_checks(
        cells, glyph_level=result.get("glyph_level_vote", False)
    )
    fatal = [
        e
        for e in initial_errors
        if e != "Overlapping physical cells"
        and not e.startswith(("Inconsistent", "Reversed x geometry", "Reversed y geometry"))
    ]
    snap_errors = (
        []
        if fatal or result.get("glyph_level_vote")
        else snap_cells(cells, eligible, candidate["bbox_pt"])
    )
    geometry_errors, slots = geometry_checks(
        cells, glyph_level=result.get("glyph_level_vote", False)
    )
    geometry_errors = list(dict.fromkeys([*fatal, *snap_errors, *geometry_errors]))
    invalid_glyphs = [
        c["id"] for c in page["chars"] if not c["text"].isspace() and not valid_box(c["bbox_pt"])
    ]
    check(
        "character_geometry",
        invalid_glyphs,
        not invalid_glyphs,
        "Invalid or zero-area character bounds",
    )
    check("geometry", geometry_errors, not geometry_errors, "; ".join(geometry_errors))
    outside = [
        i
        for i, c in enumerate(cells)
        if valid_box(c.get("bbox_pt")) and not contains(candidate["bbox_pt"], c["bbox_pt"], 1)
    ]
    check("region_bounds", outside, not outside, "Cell extends outside candidate region")
    check(
        "engine_alignment",
        grid.get("errors", []),
        not grid.get("errors"),
        "Engine alignment failed",
    )
    unmappable = (
        (
            any(c.get("font_decode_unresolved") for c in eligible)
            or garbled_layer("".join(c["text"] for c in eligible))
        )
        if page.get("font_aware")
        else (
            page.get("garbled", False)
            or garbled_layer(page["raw_text"])
            or garbled_layer("".join(c["text"] for c in page["chars"]))
        )
    )
    check(
        "page_mappable",
        not unmappable,
        not unmappable,
        "CCL/font decoding makes this page unmappable",
    )
    char_ids = [c["id"] for c in eligible]
    check(
        "unique_character_ids",
        len(set(char_ids)),
        len(set(char_ids)) == len(char_ids),
        "Duplicate character ids",
    )
    offsets = [c.get("offset") for c in eligible if c.get("offset") is not None]
    check(
        "unique_offsets",
        len(set(offsets)),
        len(set(offsets)) == len(offsets),
        "Source offsets reused by multiple glyphs",
    )
    assigned, ambiguous, mapped, critical_total, critical_ok = 0, 0, 0, 0, 0
    owners = [[] for _ in cells]
    bad_refs, rotated = [], []
    for char in eligible:
        box, text, offset = char["bbox_pt"], char["text"], char.get("offset")
        hits = [
            i
            for i, cell in enumerate(cells)
            if valid_box(cell.get("bbox_pt")) and intersects(cell["bbox_pt"], box)
        ]
        good = len(hits) == 1 and contains(cells[hits[0]]["bbox_pt"], box)
        if len(hits) > 1:
            ambiguous += 1
        source_ok = (
            type(offset) is int
            and 0 <= offset < len(page["raw_text"])
            and page["raw_text"][offset : offset + 1] == char.get("decoded_from", text)
            and expansion_mappable(char, eligible, page["raw_text"])
            and ("decoded_from" not in char or decoded_expected.get(char["id"]) == text)
        )
        if not source_ok:
            bad_refs.append(char["id"])
        if abs(char.get("rotation", 0)) > 0.1:
            rotated.append(char["id"])
        critical = any(unicodedata.category(c)[0] in "NPS" for c in text)
        critical_total += critical
        if good:
            owners[hits[0]].append(char)
            assigned += 1
            mapped += source_ok
            critical_ok += bool(critical and source_ok)
    total = len(eligible)
    coverage = mapped / total if total else 0
    check(
        "character_coverage",
        {"covered": mapped, "assigned": assigned, "total": total, "ratio": coverage},
        coverage >= 0.995,
        "Mapped character coverage below 99.5%",
    )
    check(
        "numeric_symbol_coverage",
        {"covered": critical_ok, "total": critical_total},
        critical_ok == critical_total,
        "Not all numeric/symbol characters have unique source cells",
    )
    check("ambiguous_ownership", ambiguous, ambiguous == 0, "Characters intersect multiple cells")
    check("source_references", bad_refs, not bad_refs, "Unmappable source characters")
    check("rotation", rotated, not rotated, "Rotated cell text needs manual review")
    mismatches, structural = [], []
    for i, (cell, chars) in enumerate(zip(cells, owners, strict=True)):
        ordered = [c for line in text_lines(chars, segments=False, cell=True) for c in line]
        cell["text"], cell["spacing_inferred"] = display_text(chars, page["raw_text"])
        cell["char_refs"] = [
            {
                "char_id": c["id"],
                "page": page["page"],
                "start": c.get("offset"),
                "end": c["offset"] + 1 if c.get("offset") is not None else None,
            }
            for c in ordered
        ]
        if "model_text" in cell:
            cell["crosscheck_similarity"] = SequenceMatcher(
                None,
                compact(cell["model_text"]).casefold(),
                compact(cell["text"]).casefold(),
                autojunk=False,
            ).ratio()
        if "model_text" in cell and cell["model_text"] != cell["text"]:
            left, right = compact(cell["model_text"]).casefold(), compact(cell["text"]).casefold()
            similarity = SequenceMatcher(None, left, right, autojunk=False).ratio()
            failed = bool(left) != bool(right) or similarity < 0.65
            mismatches.append(
                {
                    "cell": i,
                    "engine_text": cell["model_text"],
                    "source_text": cell["text"],
                    "similarity": similarity,
                    "structural_disagreement": failed,
                }
            )
            if failed:
                structural.append(i)
    check(
        "model_text_crosscheck",
        mismatches,
        not structural,
        "Structure text disagrees with source glyphs",
    )
    from .precision import precision_checks

    for name, details in precision_checks(cells, owners).items():
        check(name, details, not details, name.replace("_", " "))
    associations, missing, empty_values = [], [], 0
    if not geometry_errors and slots:
        nr, nc = max(r for r, _ in slots) + 1, max(c for _, c in slots) + 1
        if header_rows is None:
            tagged = [c["row"] + c["rowspan"] for c in cells if c.get("header_tag")]
            rules = candidate.get("rule_ys", [])
            header_rows = max(tagged, default=1)
            if len(rules) >= 3:
                mid = rules[1]
                above = [c["row"] + c["rowspan"] for c in cells if c["bbox_pt"][3] <= mid + 2]
                if above:
                    header_rows = max(above)
        while header_rows <= 3 and any(
            c["row"] < header_rows < c["row"] + c["rowspan"] for c in cells
        ):
            header_rows += 1
        sections = {}
        for row in range(header_rows, nr):
            nonempty = [i for i, c in enumerate(cells) if c["row"] == row and c["text"]]
            if len(nonempty) == 1:
                i = nonempty[0]
                if cells[i]["col"] == 0 and cells[i]["rowspan"] == 1:
                    sections[row] = i
        if row_header_cols is None:
            row_header_cols = 0
            for col in range(min(3, nc - 1)):
                texts = [
                    c["text"]
                    for c in cells
                    if c["row"] >= header_rows
                    and c["row"] not in sections
                    and c["col"] <= col < c["col"] + c["colspan"]
                    and c["text"]
                ]
                if (
                    not texts
                    or sum(
                        any(ch.isalpha() for ch in t)
                        and sum(ch.isalpha() for ch in t) >= sum(ch.isdigit() for ch in t)
                        for t in texts
                    )
                    <= len(texts) / 2
                ):
                    break
                row_header_cols = col + 1
            while row_header_cols < min(3, nc - 1) and any(
                c["row"] >= header_rows
                and c["row"] not in sections
                and c["col"] < row_header_cols < c["col"] + c["colspan"]
                for c in cells
            ):
                row_header_cols += 1
        if not (1 <= header_rows <= 3 and header_rows < nr and 0 <= row_header_cols < nc):
            missing.append("Cannot identify a header/body partition")
        else:
            for i, cell in enumerate(cells):
                r, c = cell["row"], cell["col"]
                if r in sections:
                    continue
                if r < header_rows < r + cell["rowspan"] or (
                    r >= header_rows and c < row_header_cols < c + cell["colspan"]
                ):
                    missing.append(f"Cell {i} crosses the header/value partition")
                if r < header_rows or c < row_header_cols:
                    continue
                if not cell["text"]:
                    empty_values += 1
                    continue
                rh = sorted(
                    {
                        slots[row, h]
                        for row in range(r, r + cell["rowspan"])
                        for h in range(row_header_cols)
                        if cells[slots[row, h]]["text"]
                    }
                )
                ch = sorted(
                    {
                        slots[h, col]
                        for h in range(header_rows)
                        for col in range(c, c + cell["colspan"])
                        if cells[slots[h, col]]["text"]
                    }
                )
                for col in range(c, c + cell["colspan"]):
                    if not any(cells[slots[h, col]]["text"] for h in range(header_rows)):
                        missing.append(f"No nonempty column header path at cell {i}")
                preceding = [row for row in sections if row < r]
                if preceding:
                    rh = [sections[max(preceding)], *rh]
                associations.append({"cell": i, "row_headers": rh, "column_headers": ch})
    else:
        missing.append("Geometry prevents header association")
    check(
        "header_value_associations",
        {"associations": associations, "errors": missing, "empty_values": empty_values},
        bool(associations) and not missing,
        "Row/column header associations are not established",
    )
    physical_errors = []
    if result.get("glyph_level_vote") and not geometry_errors:
        for row in sorted({c["row"] for c in cells}):
            ordered = sorted(
                (i for i, c in enumerate(cells) if c["row"] <= row < c["row"] + c["rowspan"]),
                key=lambda i: cells[i]["col"],
            )
            centres = [
                (
                    i,
                    (
                        min(g["bbox_pt"][0] for g in owners[i])
                        + max(g["bbox_pt"][2] for g in owners[i])
                    )
                    / 2,
                )
                for i in ordered
                if owners[i]
            ]
            if any(a[1] >= b[1] for a, b in zip(centres, centres[1:], strict=False)):
                physical_errors.append(f"Reversed glyph column order in row {row}")
        for association in associations:
            i = association["cell"]
            headers = association["column_headers"]
            if not owners[i] or not headers:
                continue
            lowest = max(cells[h]["row"] for h in headers)
            path = [h for h in headers if cells[h]["row"] == lowest]
            left = min(cells[h]["bbox_pt"][0] for h in path)
            right = max(cells[h]["bbox_pt"][2] for h in path)
            centre = (
                min(g["bbox_pt"][0] for g in owners[i]) + max(g["bbox_pt"][2] for g in owners[i])
            ) / 2
            if not left <= centre <= right:
                physical_errors.append(f"Body cell {i} outside lowest column header cuts")
        check(
            "physical_column_sanity",
            physical_errors,
            not physical_errors,
            "Physical column/header association failed",
        )
    result.update(
        verified=not reasons,
        status="已核验" if not reasons else "未核验",
        verification={"version": VERSION, "checks": checks, "failures": reasons},
        header_rows=header_rows,
        row_header_cols=row_header_cols,
        evidence_eligible=False,
    )  # F0 never supplies production evidence.
    return result


def grid_html(grid):
    """Escapes source strings; engine HTML is never embedded."""
    cells = grid["cells"]
    rows = sorted({c["row"] for c in cells if type(c.get("row")) is int})
    output = ["<table>"]
    for row in rows:
        output.append("<tr>")
        for cell in sorted((c for c in cells if c.get("row") == row), key=lambda c: c["col"]):
            rs, cs = cell.get("rowspan"), cell.get("colspan")
            if type(rs) is not int or type(cs) is not int or min(rs, cs) < 1:
                continue
            output.append(
                f'<td rowspan="{rs}" colspan="{cs}">'
                + html.escape(cell.get("text", "")).replace("\n", "<br>")
                + "</td>"
            )
        output.append("</tr>")
    return "".join(output) + "</table>"


def evidence_cells(grid):
    """Explicit guard for consumers experimenting outside the production evidence path."""
    if not grid.get("verified"):
        raise ValueError("Unverified tables cannot be used as evidence")
    raise ValueError("F0 has no production evidence integration")
