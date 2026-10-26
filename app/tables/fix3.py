"""Offline fix-3 dev replay; no inference, model loading, or network access."""

from collections import Counter
from pathlib import Path

from .chars import extract_pages
from .engines import html_grid, mineru, mineru_html, pp_grid, read_mineru, region_chars
from .glyphgrid import build
from .io import read, write
from .perturb import audit
from .verify import verify
from .vote import consensus


def allowed_pdf(path, paper):
    path = Path(path)
    if "holdout-d040" in str(path).lower():
        raise ValueError("Forbidden holdout path")
    if "holdout" in str(path).lower() and not (
        paper in {"attrscore", "cove", "csqe", "e5-mistral"}
        and path.parent.name == "papers"
        and path.parent.parent.name == "holdout-v4"
        and path.stem == paper
    ):
        raise ValueError("Not an authorized dev PDF")
    return path


def calibrate():
    """Compare predeclared gutter settings on dev only; retain all outcomes."""
    output = Path(".pytest-tmp/f0-fix3")
    saved = Path("data/evaluation/table-pilot/dev-f0d")
    settings = [(w, s) for w in (2.0, 3.0, 4.0) for s in (0.85, 0.90, 1.0)]
    samples = []
    for paper in read("data/evaluation/table-pilot/dev-f0/selection.json")["papers"]:
        name = paper["paper"]
        structured = read_mineru(paper["mineru_json"])
        for file in sorted((saved / name / "tables").glob("*-mineru.json")):
            old = read(file)
            candidate = old["candidate"]
            page = read(output / "pages" / name / f"{old['page']:03d}.json")
            logical = {}
            for engine in ("mineru", "ppstructure"):
                try:
                    body = (
                        mineru_html(structured, page, candidate)
                        if engine == "mineru"
                        else read(saved / name / "raw" / f"{candidate['id']}.json")["pred_html"]
                    )
                    logical[engine] = html_grid(body)
                except (ValueError, FileNotFoundError):
                    logical[engine] = []
            samples.append((page, candidate, logical))
    results = []
    for width, support in settings:
        verified = votes = 0
        for page, candidate, logical in samples:
            try:
                physical = build(page, candidate, min_width=width, support=support)
                result = consensus(page, candidate, physical, logical)
                verified += result["grid"]["verified"]
                votes += any(v["vote_passed"] for v in result["votes"].values())
            except ValueError:
                pass
        results.append(
            {
                "minimum_gutter_pt": width,
                "gutter_support": support,
                "verified": verified,
                "tables_with_vote": votes,
            }
        )
        print(results[-1], flush=True)
    write(output / "calibration.json", results)


def run(*, reextract=True):
    saved = Path("data/evaluation/table-pilot/dev-f0d")
    output = Path(".pytest-tmp/f0-fix3")
    records = []
    perturb = []
    for paper in read("data/evaluation/table-pilot/dev-f0/selection.json")["papers"]:
        name = paper["paper"]
        enriched = {}
        pages = (
            extract_pages(allowed_pdf(paper["pdf"], name), paper["pages"])
            if reextract
            else (read(output / "pages" / name / f"{n:03d}.json") for n in paper["pages"])
        )
        for page in pages:
            page["font_aware"] = True
            enriched[page["page"]] = page
            if reextract:
                write(output / "pages" / name / f"{page['page']:03d}.json", page)
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
                    "votes": {},
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
    report["calibration_file"] = "calibration.json"
    report["threshold_rationale"] = (
        "Widths 2/3/4 pt and support 0.90/1.00 share a 17-table dev plateau; "
        "support 0.85 yields 15. Choose 3 pt to preserve narrow numeric gutters and "
        "0.90 for nearly all body lines. Retain preregistered text ratio 0.65. "
        "Dev calibration is not holdout evidence."
    )
    write(output / "report.json", report)
    print({k: v for k, v in report.items() if k not in ("per_table", "perturbations")})


if __name__ == "__main__":
    import sys

    if "--calibrate" in sys.argv:
        calibrate()
    else:
        run(reextract="--reuse-pages" not in sys.argv)
