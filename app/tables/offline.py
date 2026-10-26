"""Offline dev-only detection and saved-MinerU re-verification; never runs engines."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from .chars import text_lines
from .detect import CAPTION, detect
from .engines import mineru, mineru_html, read_mineru
from .io import read, write
from .verify import verify


def dev_path(path):
    path = Path(path)
    if any("holdout" in part.lower() for part in path.parts):
        raise ValueError(f"Holdout access forbidden: {path}")
    return path


def caption_key(text):
    # Decode only the two known offset ASCII families for caption scoring.
    text = "".join(
        chr(ord(c) - offset) if offset else c
        for c in text
        for offset in [
            0x5B00 if 0x5B00 <= ord(c) <= 0x5B7F else 0x5700 if 0x5700 <= ord(c) <= 0x577F else 0
        ]
    )
    match = re.match(r"\s*(?:Table|表)\s*([A-Z]?\d+|[一二三四五六七八九十]+)", text, re.I)
    return match.group(1) if match else None


# Caption ground truth supplied in the fix brief; keys are table numbers, not detections.
TRUTH = {
    "2024.ccl-1.2": {5: ["1"], 6: ["2"], 7: ["3"], 8: ["4"], 9: ["5"]},
    "2024.ccl-2.9": {9: ["1"]},
    "2025.ccl-1.12": {2: ["1"], 8: ["2"], 9: ["3", "4"], 10: ["5"], 11: ["6"]},
    "2025.ccl-1.13": {6: ["1", "2"], 7: ["3", "4"], 8: ["5"], 10: ["6", "7"], 11: ["8"]},
    "2025.ccl-1.31": {6: ["2", "3"], 7: ["4"], 8: ["5"], 9: ["6"], 12: ["7"]},
    "attrscore": {5: ["1"], 6: ["2"], 7: ["3"], 8: ["4"]},
    "cove": {6: ["1", "2"], 7: ["3"], 12: ["4", "5"]},
    "csqe": {4: ["3", "4", "5"], 5: ["6", "7", "8"], 9: ["9", "10"]},
    "e5-mistral": {5: ["1"], 6: ["2", "3"], 7: ["4"], 8: ["5"]},
}


def run(selection, saved, output, review=None):
    selection, saved, output = map(dev_path, (selection, saved, output))
    output.mkdir(parents=True, exist_ok=True)
    pages, tables, failures = [], [], Counter()
    expected_total = found_total = 0
    for paper in read(selection)["papers"]:
        name = paper["paper"]
        structured = read_mineru(dev_path(paper["mineru_json"]))
        for number in paper["pages"]:
            # Use saved dev extraction; never open the PDF paths in selection.
            page = read(dev_path(saved / name / "pages" / f"{number:03d}.json"))
            candidates = list(detect(page))
            expected = TRUTH.get(name, {}).get(number)
            if expected is None:
                # Brief leaves the two garbled papers unenumerated: transcribe their
                # offset captions from the saved source layer, never engine output.
                expected = [
                    caption_key("".join(c["text"] for c in line))
                    for line in text_lines(page["chars"])
                    if CAPTION.match("".join(c["text"] for c in line))
                ]
                expected = [k for k in expected if k]
            detected = {caption_key(c["caption"]) for c in candidates if c["captioned"]}
            found = set(expected) & detected
            expected_total += len(expected)
            found_total += len(found)
            pages.append(
                {
                    "paper": name,
                    "page": number,
                    "ground_truth": expected,
                    "found": sorted(found),
                    "missed": sorted(set(expected) - found),
                    "candidates": candidates,
                    "rejected": page.get("rejected", []),
                }
            )
            for candidate in candidates:
                identifier = f"{name}/{candidate['id']}"
                try:
                    grid = mineru(mineru_html(structured, page, candidate), page, candidate)
                except Exception as exc:
                    grid = {"engine": "mineru", "cells": [], "errors": [str(exc)]}
                checked = verify(page, candidate, grid)
                for reason in checked["verification"]["failures"]:
                    failures[reason] += 1
                tables.append({"id": identifier, "candidate": candidate, "grid": checked})
                write(output / name / "tables" / f"{candidate['id']}-mineru.json", tables[-1])
            write(
                output / name / "run.json",
                {
                    "paper": name,
                    "pages": [p for p in pages if p["paper"] == name],
                    "rejected": [
                        dict(r, page=p["page"])
                        for p in pages
                        if p["paper"] == name
                        for r in p["rejected"]
                    ],
                    "engines": ["mineru"],
                    "mode": "saved-dev-offline",
                },
            )
    report = {
        "pages": pages,
        "captioned_recall": {
            "found": found_total,
            "total": expected_total,
            "rate": found_total / expected_total if expected_total else None,
        },
        "kept_candidates": len(tables),
        "non_table_share": {
            "rate": None,
            "reason": "Requires explicit review labels; uncaptioned does not mean non-table.",
        },
    }
    if review is not None:
        labels = read(dev_path(review))
        identifiers = {t["id"] for t in tables}
        if set(labels["tables"]) != identifiers:
            raise ValueError("Detection review must label exactly the current kept candidates")
        if any(type(v.get("is_table")) is not bool for v in labels["tables"].values()):
            raise ValueError("Detection review needs boolean is_table labels")
        non_tables = sum(not v["is_table"] for v in labels["tables"].values())
        report["non_table_share"] = {
            "non_tables": non_tables,
            "total": len(tables),
            "rate": non_tables / len(tables) if tables else None,
            "provenance": labels["provenance"],
        }
    write(output / "detect-report.json", report)
    engine_failures = Counter()
    for table in tables:
        for error in table["grid"].get("errors", []):
            category = re.sub(r"r\d+c\d+", "rNcN", error)
            if "overlaps=" in category:
                category = category.split("; overlaps=")[0]
            engine_failures[category] += 1
    summary = {
        "tables": len(tables),
        "verified": sum(t["grid"]["verified"] for t in tables),
        "failure_counts": dict(failures.most_common()),
        "engine_failure_counts": dict(engine_failures.most_common()),
        "failed_check_counts": dict(
            Counter(
                name
                for t in tables
                for name, check in t["grid"]["verification"]["checks"].items()
                if not check["passed"]
            ).most_common()
        ),
        "per_table": [
            {
                "id": t["id"],
                "verified": t["grid"]["verified"],
                "engine_errors": t["grid"].get("errors", []),
                "failures": t["grid"]["verification"]["failures"],
            }
            for t in tables
        ],
    }
    write(output / "mineru-offline-report.json", summary)
    return {
        "recall": report["captioned_recall"],
        **{k: v for k, v in summary.items() if k != "per_table"},
    }


def rerun_saved(saved, output):
    """Recompute both adapters from saved dev artifacts without starting any engine."""
    import html

    from .engines import pp_grid, region_chars

    saved, output = map(dev_path, (saved, output))
    reports = {}
    for engine in ("mineru", "ppstructure"):
        records, similarities, ious = [], [], []
        for file in sorted(saved.glob(f"*/tables/*-{engine}.json")):
            old = read(file)
            candidate = old["candidate"]
            page = read(file.parent.parent / "pages" / f"{old['page']:03d}.json")
            try:
                if engine == "ppstructure":
                    raw = read(file.parent.parent / "raw" / f"{candidate['id']}.json")
                    grid = pp_grid(
                        raw,
                        [page["width_pt"], page["height_pt"]],
                        candidate,
                        region_chars(page, candidate),
                    )
                else:
                    cells = old["grid"]["cells"]
                    body = (
                        "<table>"
                        + "".join(
                            "<tr>"
                            + "".join(
                                f'<td rowspan="{c["rowspan"]}" colspan="{c["colspan"]}">'
                                + html.escape(c.get("model_text", ""))
                                + "</td>"
                                for c in cells
                                if c["row"] == row
                            )
                            + "</tr>"
                            for row in sorted({c["row"] for c in cells})
                        )
                        + "</table>"
                    )
                    grid = mineru(body, page, candidate)
                    for cell, original in zip(grid["cells"], cells, strict=True):
                        cell["header_tag"] = original.get("header_tag", False)
                checked = verify(page, candidate, grid)
            except (ValueError, KeyError, IndexError) as exc:
                checked = verify(
                    page, candidate, {"engine": engine, "cells": [], "errors": [str(exc)]}
                )
            record = {
                "id": f"{file.parent.parent.name}/{candidate['id']}",
                "candidate": candidate,
                "grid": checked,
            }
            write(output / file.parent.parent.name / "tables" / file.name, record)
            records.append(record)
            similarities.extend(
                c["crosscheck_similarity"] for c in checked["cells"] if "crosscheck_similarity" in c
            )
            ious.extend(c["snap_iou"] for c in checked["cells"] if "snap_iou" in c)
        failures = Counter(
            reason for r in records for reason in r["grid"]["verification"]["failures"]
        )
        mappable = [
            r for r in records if r["grid"]["verification"]["checks"]["page_mappable"]["passed"]
        ]
        reports[engine] = {
            "tables": len(records),
            "verified": sum(r["grid"]["verified"] for r in records),
            "mappable_tables": len(mappable),
            "verified_mappable": sum(r["grid"]["verified"] for r in mappable),
            "failure_counts": dict(failures.most_common()),
            "crosscheck_similarity_distribution": sorted(similarities),
            "box_snap_iou_distribution": sorted(ious),
            "per_table": [
                {
                    "id": r["id"],
                    "verified": r["grid"]["verified"],
                    "engine_errors": r["grid"].get("errors", []),
                    "failures": r["grid"]["verification"]["failures"],
                }
                for r in records
            ],
        }
    write(
        output / "report.json",
        {
            "mode": "saved-dev-offline",
            "similarity_threshold": 0.65,
            "minimum_iou": 0.5,
            "engines": reports,
        },
    )
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--saved", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-labels", type=Path)
    args = parser.parse_args()
    import json

    print(
        json.dumps(
            run(args.selection, args.saved, args.output, args.review_labels),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
