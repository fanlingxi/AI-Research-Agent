"""Offline fix-4 dev replay; no inference, model loading, or network access."""

from collections import Counter
from pathlib import Path

from .engines import html_grid, mineru, mineru_html, pp_grid, read_mineru, region_chars
from .glyphgrid import build
from .io import read, write
from .perturb import audit
from .verify import verify
from .vote import consensus


def run():
    saved = Path("data/evaluation/table-pilot/dev-f0d")
    output = Path(".pytest-tmp/f0-fix4")
    records = []
    perturb = []
    for paper in read("data/evaluation/table-pilot/dev-f0/selection.json")["papers"]:
        name = paper["paper"]
        enriched = {}
        for number in paper["pages"]:
            page = read(Path(".pytest-tmp/f0-fix3/pages") / name / f"{number:03d}.json")
            page["font_aware"] = True
            enriched[number] = page
        structured = read_mineru(paper["mineru_json"])
        for file in sorted((saved / name / "tables").glob("*-mineru.json")):
            old = read(file)
            candidate = old["candidate"]
            number = old["page"]
            page = enriched[number]
            before = read(saved / name / "pages" / f"{number:03d}.json")
            raw_path = saved / name / "raw" / f"{candidate['id']}.json"
            raw = read(raw_path) if raw_path.exists() else None
            try:
                body = mineru_html(structured, page, candidate)
            except ValueError:
                body = "<table></table>"
            logical = {}
            for engine, text in (
                ("mineru", body),
                ("ppstructure", raw["pred_html"] if raw else ""),
            ):
                try:
                    logical[engine] = html_grid(text)
                except ValueError:
                    logical[engine] = []
            diagnostics = {}
            for engine in logical:
                try:
                    grid = (
                        mineru(body, page, candidate)
                        if engine == "mineru"
                        else pp_grid(
                            raw,
                            [page["width_pt"], page["height_pt"]],
                            candidate,
                            region_chars(page, candidate),
                        )
                    )
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    grid = {"cells": [], "errors": [str(exc)], "engine": engine}
                diagnostics[engine] = verify(page, candidate, grid)
            try:
                physical = build(page, candidate)
                voted = consensus(page, candidate, physical, logical)
                metadata = {k: v for k, v in physical.items() if k not in ("lines", "chars")}
            except ValueError as exc:
                metadata = {"error": str(exc)}
                voted = {
                    "grid": verify(page, candidate, {"cells": [], "errors": [str(exc)]}),
                    "votes": {
                        engine: {
                            "engine": engine,
                            "cells": [],
                            "errors": [str(exc)],
                            "vote_passed": False,
                            "agreement_score": 0.0,
                        }
                        for engine in logical
                    },
                }
            record = {
                "id": f"{name}/{candidate['id']}",
                "candidate": candidate,
                "glyph_grid": metadata,
                **voted,
                "old_paths": diagnostics,
                "mappable_before": verify(before, candidate, {"cells": []})["verification"][
                    "checks"
                ]["page_mappable"]["passed"],
            }
            records.append(record)
            write(output / name / "tables" / f"{candidate['id']}.json", record)
            if record["grid"]["verified"]:
                perturb.append({"id": record["id"], **audit(page, candidate, record["grid"])})
        print(name, len(records), flush=True)
    failures = Counter(reason for r in records for reason in r["grid"]["verification"]["failures"])
    report = {
        "tables": len(records),
        "verified": sum(r["grid"]["verified"] for r in records),
        "new_vote_per_engine": {
            e: sum(
                r["votes"].get(e, {}).get("verification_result", {}).get("verified", False)
                for r in records
            )
            for e in ("mineru", "ppstructure")
        },
        "old_paths": {
            e: sum(r["old_paths"][e]["verified"] for r in records)
            for e in ("mineru", "ppstructure")
        },
        "old_union": sum(any(v["verified"] for v in r["old_paths"].values()) for r in records),
        "union": sum(
            r["grid"]["verified"] or any(v["verified"] for v in r["old_paths"].values())
            for r in records
        ),
        "mappable_before": sum(r["mappable_before"] for r in records),
        "mappable_after": sum(
            r["grid"]["verification"]["checks"]["page_mappable"]["passed"] for r in records
        ),
        "newly_mappable_verified": sum(
            not r["mappable_before"] and r["grid"]["verified"] for r in records
        ),
        "failure_counts": dict(failures),
        "perturbations": perturb,
        "thresholds": {"minimum_gutter_pt": 3, "gutter_support": 0.9, "similarity": 0.65},
        "per_table": [
            {
                "id": r["id"],
                "shape": r["glyph_grid"].get("shape"),
                "verified": r["grid"]["verified"],
                "votes": {
                    k: {f: v[f] for f in ("vote_passed", "agreement_score", "errors")}
                    for k, v in r["votes"].items()
                },
                "failures": r["grid"]["verification"]["failures"],
            }
            for r in records
        ],
    }
    categories = {}
    for record in records:
        if record["grid"]["verified"]:
            continue
        if record.get("engine_disagreement"):
            category = "engine disagreement"
        elif "error" in record["glyph_grid"]:
            category = record["glyph_grid"]["error"]
        elif not any(v["vote_passed"] for v in record["votes"].values()):
            category = "No complete monotone glyph agreement"
        elif any(
            not v.get("verification_result", {})
            .get("verification", {})
            .get("checks", {})
            .get("physical_column_sanity", {"passed": True})["passed"]
            for v in record["votes"].values()
            if v["vote_passed"]
        ):
            category = "Physical column/header association"
        else:
            category = "Header/value association"
        categories.setdefault(category, []).append(record["id"])
    report["failure_classes"] = {
        k: {"count": len(v), "examples": v[:3]} for k, v in categories.items()
    }
    report["perturbation_summary"] = {
        "total": sum(len(r) - 1 for r in perturb),
        "rejected": sum(v["rejected"] for r in perturb for k, v in r.items() if k != "id"),
    }
    report["newly_mappable"] = sum(
        not r["mappable_before"] and r["grid"]["verification"]["checks"]["page_mappable"]["passed"]
        for r in records
    )
    report["newly_mappable_old_union_verified"] = sum(
        not r["mappable_before"] and any(g["verified"] for g in r["old_paths"].values())
        for r in records
    )
    report["old_path_failure_counts"] = {
        e: dict(
            Counter(
                reason for r in records for reason in r["old_paths"][e]["verification"]["failures"]
            )
        )
        for e in ("mineru", "ppstructure")
    }
    report["calibration_file"] = "../f0-fix3/calibration.json"
    report["threshold_rationale"] = (
        "Round-3 glyph grid retained for diagnostics only; voting uses row-group whitespace. "
        "Every cell requires casefolded whitespace-free similarity >=0.65 and exact "
        "empty/nonempty agreement, unique glyph ownership, and downstream checks. Dev only."
    )
    write(output / "report.json", report)
    print(
        {
            k: report[k]
            for k in (
                "tables",
                "verified",
                "new_vote_per_engine",
                "old_paths",
                "old_union",
                "union",
                "failure_classes",
                "perturbation_summary",
            )
        },
        flush=True,
    )


if __name__ == "__main__":
    run()
