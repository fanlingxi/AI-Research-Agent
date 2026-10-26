"""Explicit offline CLI: select-dev, run, packets, score, perturb. Imports do no work."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import shutil
import subprocess
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .chars import extract_pages
from .detect import detect
from .engines import PaddleSession, mineru, mineru_html, ppstructure, read_mineru, region_chars
from .geometry import pixel_box, union
from .io import read, write
from .packets import build
from .perturb import audit
from .resources import Resources
from .score import score
from .verify import grid_html, verify

PROJECT = Path("D:/AI-Research-Agent")
DEFAULT_ROOT = PROJECT / "data/evaluation/table-pilot"


def page_numbers(value):
    pages = set()
    for part in value.split(","):
        ends = part.split("-")
        if len(ends) == 1:
            pages.add(int(ends[0]))
        elif len(ends) == 2:
            start, end = map(int, ends)
            if start > end:
                raise ValueError("Backwards page range")
            pages.update(range(start, end + 1))
        else:
            raise ValueError("Invalid page range")
    if not pages or min(pages) < 1:
        raise ValueError("Pages are 1-based")
    return sorted(pages)


def select_dev(dev_root, output):
    """Selection reads existing JSON only; does not run a PDF parser or model."""
    manifest = read(dev_root / "manifest.json")
    selections = []
    for paper in manifest["papers"]:
        pages, packet_pages = set(), set()
        for packet_path in (dev_root / "packets/images").glob("*.json"):
            packet = read(packet_path)
            if packet.get("paper") == paper["paper"] and any(
                b["type"] == "table"
                for blocks in packet.get("systems", {}).values()
                for b in blocks
            ):
                packet_pages.add(packet["page"])
        for engine in ("mineru", "paddle"):
            path = dev_root / "normalized" / engine / f"{paper['paper']}.json"
            if path.exists():
                for page in read(path):
                    if any(b["type"] == "table" for b in page["blocks"]):
                        pages.add(page["page"])
        pages |= packet_pages
        pages = {p for p in pages if 1 <= p <= paper["page_count"]}
        if not pages:
            continue
        raw = sorted((dev_root / "raw/mineru" / paper["paper"]).rglob("*structured*.json"))
        selections.append(
            {
                "paper": paper["paper"],
                "language": paper["language"],
                "pdf": paper["pdf"],
                "pages": sorted(pages),
                "packet_pages": sorted(packet_pages),
                "page_png_dir": str(dev_root / "pages" / paper["paper"]),
                "mineru_json": str(dev_root / "raw/mineru" / paper["paper"]) if raw else None,
                "mineru_json_candidates": [str(p) for p in raw],
            }
        )
    result = {
        "split": "development",
        "selection_bias": "existing parser table signals",
        "papers": selections,
    }
    write(output, result)
    return result


def report(root, manifest):
    sections = [
        '<!doctype html><meta charset="utf-8"><title>表格离线试点</title>',
        "<style>body{font:16px sans-serif;margin:2em}table{border-collapse:collapse}",
        "td,th{border:1px solid #999;padding:5px}img{max-width:100%}",
        "pre{white-space:pre-wrap}section{margin:3em 0}</style>",
        "<h1>表格离线试点（开发集）</h1><p>未核验表格不得用于证据。</p>",
    ]
    if manifest.get("fatal_error"):
        sections.append("<p>运行失败：" + html.escape(manifest["fatal_error"]) + "</p>")
    for page in manifest["pages"]:
        sections.append(f"<h2>第 {page['page']} 页</h2>")
        for table in page["tables"]:
            sections.append("<section><h3>" + html.escape(table["candidate"]["id"]) + "</h3>")
            sections.append(f'<img src="{html.escape(table["crop"], quote=True)}" alt="原页裁剪">')
            for engine, path in table["results"].items():
                record = read(root / path)
                grid = record["grid"]
                sections.append(f"<h4>{html.escape(engine)}：{grid['status']}</h4>")
                sections.append(grid_html(grid))
                sections.append(
                    "<details><summary>核验与耗时</summary><pre>"
                    + html.escape(
                        json.dumps(
                            {"verification": grid["verification"], "timings": record["timings"]},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    + "</pre></details>"
                )
            sections.append("</section>")
        if page.get("error"):
            sections.append("<p>" + html.escape(page["error"]) + "</p>")
    (root / "report.html").write_text("\n".join(sections), encoding="utf-8")


@contextmanager
def failure_journal(root, manifest, started, measured):
    write(root / "run.json", manifest)
    try:
        yield
    except Exception as exc:
        manifest.update(
            status="failed",
            fatal_error=f"{type(exc).__name__}: {exc}",
            elapsed_seconds=time.perf_counter() - started,
            resources=measured.result,
        )
        write(root / "run.json", manifest)
        report(root, manifest)
        raise


def run_pilot(
    args,
    *,
    extract=extract_pages,
    detector=detect,
    paddle=ppstructure,
    resources=Resources,
    runner=subprocess.run,
):
    if "ppstructure" in args.engines and not args.enable_model:
        raise ValueError("ppstructure requires explicit --enable-model (integrator only)")
    if not args.pdf.is_file():
        raise ValueError("PDF must be a local file")
    if not args.page_png_dir and not args.render_python:
        raise ValueError("Supply --page-png-dir or --render-python")
    if "mineru" in args.engines and not args.mineru_json:
        raise ValueError("mineru requires --mineru-json with existing output")
    if args.root.exists() and any(args.root.iterdir()):
        raise ValueError("Use a fresh output directory; existing runs are immutable")
    args.root.mkdir(parents=True, exist_ok=True)
    root = args.root
    manifest = {
        "schema_version": 1,
        "status": "running",
        "paper": args.paper or args.pdf.stem,
        "language": args.language,
        "pdf": str(args.pdf.resolve()),
        "source_sha256": hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
        "engines": args.engines,
        "config": {
            "header_rows": args.header_rows,
            "row_header_cols": args.row_header_cols,
            "timeout_seconds": args.timeout,
            "paddle_python": str(args.paddle_python),
            "paddle_cache": str(args.paddle_cache),
            "mineru_json": str(args.mineru_json) if args.mineru_json else None,
        },
        "pages": [],
        "rejected": [],
        "split": getattr(args, "split", "development"),
        "timing_notes": "One persistent Paddle worker per paper; "
        "cold start included once and amortized. "
        "Shared detection/render costs allocated per table and engine. "
        "Cached PNG generation time is unknown, not zero-cost rendering.",
    }
    started = time.perf_counter()
    render_seconds = 0.0
    with (
        resources() as measured,
        failure_journal(root, manifest, started, measured),
        ExitStack() as stack,
    ):
        worker = None
        if "ppstructure" in args.engines and paddle is ppstructure:
            worker = stack.enter_context(
                PaddleSession(args.paddle_python, args.paddle_cache, root / "paddle-worker.log")
            )
        structured = read_mineru(args.mineru_json) if args.mineru_json else None
        page_dir = args.page_png_dir
        if not page_dir:
            page_dir = root / "rendered"
            begin = time.perf_counter()
            runner(
                [
                    str(args.render_python),
                    str(Path(__file__).with_name("render_worker.py")),
                    str(args.pdf.resolve()),
                    str(page_dir.resolve()),
                    ",".join(map(str, args.pages)),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=600,
            )
            render_seconds = time.perf_counter() - begin
        iterator = iter(extract(args.pdf, args.pages))
        for number in args.pages:
            begin = time.perf_counter()
            page = next(iterator)
            if page["page"] != number:
                raise ValueError("Extractor page mismatch")
            page["font_aware"] = True
            candidates = list(detector(page))
            manifest["rejected"].extend({"page": number, **r} for r in page.get("rejected", []))
            detection_seconds = time.perf_counter() - begin
            write(root / "pages" / f"{number:03d}.json", page)
            png_relative = f"pages/{number:03d}.png"
            shutil.copyfile(page_dir / f"{number:03d}.png", root / png_relative)
            page_result = {
                "page": number,
                "png": png_relative,
                "tables": [],
                "rejected": page.get("rejected", []),
                "detection_seconds": detection_seconds,
                "crop_seconds": 0.0,
            }
            page_result["render_seconds"] = render_seconds / len(args.pages)
            manifest["pages"].append(page_result)
            from PIL import Image

            with Image.open(root / png_relative) as image:
                size_pt = [page["width_pt"], page["height_pt"]]
                aspect_error = abs(image.width / image.height / (size_pt[0] / size_pt[1]) - 1)
                coordinate_error = (
                    not page.get("cropbox_matches_mediabox", True) or aspect_error > 0.01
                )
                for candidate in candidates:
                    identifier = candidate["id"]
                    crop_relative = f"crops/{identifier}.png"
                    (root / "crops").mkdir(exist_ok=True)
                    begin = time.perf_counter()
                    review_box = union(
                        [
                            candidate["bbox_pt"],
                            candidate.get("caption_bbox_pt", candidate["bbox_pt"]),
                        ]
                    )
                    image.crop(pixel_box(review_box, size_pt, image.size)).save(
                        root / crop_relative
                    )
                    crop_seconds = time.perf_counter() - begin
                    page_result["crop_seconds"] += crop_seconds
                    table = {"candidate": candidate, "crop": crop_relative, "results": {}}
                    page_result["tables"].append(table)
                    final_paths = {}
                    for engine in args.engines:
                        begin = time.perf_counter()
                        try:
                            if coordinate_error:
                                raise ValueError(
                                    "PNG/MediaBox coordinate mismatch; review manually"
                                )
                            if engine == "mineru":
                                grid = mineru(
                                    mineru_html(structured, page, candidate), page, candidate
                                )
                            else:
                                grid = paddle(
                                    root / png_relative,
                                    candidate,
                                    size_pt,
                                    python=args.paddle_python,
                                    cache_home=args.paddle_cache,
                                    output=root / "raw" / f"{identifier}.json",
                                    timeout=args.timeout,
                                    chars=region_chars(page, candidate),
                                    **({"worker": worker} if worker is not None else {}),
                                )
                        except Exception as exc:
                            grid = {
                                "engine": engine,
                                "cells": [],
                                "errors": [f"{type(exc).__name__}: {exc}"],
                            }
                        final_paths[f"old/{engine}"] = grid
                        structure_seconds = time.perf_counter() - begin
                        begin = time.perf_counter()
                        checked = verify(
                            page,
                            candidate,
                            grid,
                            header_rows=args.header_rows,
                            row_header_cols=args.row_header_cols,
                        )
                        verify_seconds = time.perf_counter() - begin
                        timings = {
                            "detection_seconds": detection_seconds / max(1, len(candidates)),
                            "render_seconds": render_seconds
                            / len(args.pages)
                            / max(1, len(candidates))
                            + crop_seconds,
                            "structure_seconds": structure_seconds,
                            "verification_seconds": verify_seconds,
                            "cached_page_render_time_unknown": bool(args.page_png_dir),
                            "cold_start_included": engine == "ppstructure",
                            "cold_start_seconds": grid.get("cold_start_seconds", 0),
                        }
                        timings["total_seconds"] = sum(
                            timings[k]
                            for k in (
                                "detection_seconds",
                                "render_seconds",
                                "structure_seconds",
                                "verification_seconds",
                            )
                        )
                        path = f"tables/{identifier}-{engine}.json"
                        write(
                            root / path,
                            {
                                "paper": manifest["paper"],
                                "page": number,
                                "candidate": candidate,
                                "grid": checked,
                                "timings": timings,
                            },
                        )
                        table["results"][engine] = path
                    from .engines import html_grid
                    from .final import final_status
                    from .glyphgrid import build as glyph_build
                    from .vote import vote

                    paths = dict(final_paths)
                    for engine in table["results"]:
                        try:
                            paths[f"glyph/{engine}"] = vote(
                                glyph_build(page, candidate),
                                html_grid(mineru_html(structured, page, candidate))
                                if engine == "mineru"
                                else html_grid(
                                    read(root / "raw" / f"{identifier}.json")["pred_html"]
                                ),
                                engine,
                            )
                        except (ValueError, KeyError, TypeError, IndexError, OSError) as exc:
                            paths[f"glyph/{engine}"] = {"cells": [], "errors": [str(exc)]}
                    final = final_status(
                        page,
                        candidate,
                        paths,
                        header_rows=args.header_rows,
                        row_header_cols=args.row_header_cols,
                    )
                    final_path = f"tables/{identifier}-final.json"
                    write(root / final_path, {"candidate": candidate, "page": number, **final})
                    table["final"] = final_path
            write(root / "run.json", manifest)  # Recoverable incremental outputs.
    manifest["resources"] = measured.result
    manifest["status"] = "completed"
    manifest["elapsed_seconds"] = time.perf_counter() - started
    reconcile_timings(root, manifest, render_seconds)
    manifest["performance"] = performance(root, manifest, render_seconds)
    write(root / "run.json", manifest)
    report(root, manifest)
    return {
        "root": str(root),
        "pages": len(manifest["pages"]),
        "tables": sum(len(page["tables"]) for page in manifest["pages"]),
    }


def reconcile_timings(root, manifest, render_seconds):
    """Amortize orchestration and pages without detections; include failures in wall time."""
    shared = render_seconds + sum(
        p["detection_seconds"] + p["crop_seconds"] for p in manifest["pages"]
    )
    records = [
        (path, read(root / path))
        for page in manifest["pages"]
        for table in page["tables"]
        for path in table["results"].values()
    ]
    for engine in manifest["engines"]:
        matching = [r for _, r in records if r["grid"].get("engine") == engine]
        cold = sum(r["timings"].get("cold_start_seconds", 0) for r in matching)
        for record in matching:
            timing = record["timings"]
            delta = cold / len(matching) - timing.get("cold_start_seconds", 0)
            timing["measured_structure_seconds"] = timing["structure_seconds"]
            timing["structure_seconds"] += delta
            timing["total_seconds"] += delta
            timing["amortized_cold_start_seconds"] = cold / len(matching)
    engine_time = sum(
        r["timings"]["structure_seconds"] + r["timings"]["verification_seconds"] for _, r in records
    )
    overhead = max(0, manifest["elapsed_seconds"] - shared - engine_time)
    undetected = sum(
        p["detection_seconds"] + p["render_seconds"] for p in manifest["pages"] if not p["tables"]
    )
    manifest["orchestration_seconds"] = overhead
    count = sum(len(p["tables"]) for p in manifest["pages"])
    for path, record in records:
        allocated = (overhead + undetected) / count
        record["timings"]["amortized_overhead_seconds"] = allocated
        record["timings"]["total_seconds"] += allocated
        write(root / path, record)


def performance(root, manifest, render_seconds):
    result = {}
    for engine in manifest["engines"]:
        records = [
            read(root / table["results"][engine])
            for page in manifest["pages"]
            for table in page["tables"]
        ]
        times = sorted(r["timings"]["total_seconds"] for r in records)
        paper_total = sum(p["detection_seconds"] for p in manifest["pages"]) + render_seconds
        paper_total += sum(
            sum(r["timings"][k] for k in ("structure_seconds", "verification_seconds"))
            for r in records
        )
        # Include all per-crop image preparation costs in the paper estimate.
        allocated_render = sum(p["render_seconds"] for p in manifest["pages"] if p["tables"])
        paper_total += sum(r["timings"]["render_seconds"] for r in records) - allocated_render
        paper_total += manifest["orchestration_seconds"]
        cached = any(r["timings"]["cached_page_render_time_unknown"] for r in records)
        result[engine] = {
            "tables": len(records),
            "failures": sum(bool(r["grid"].get("errors")) for r in records),
            "p95_seconds": times[math.ceil(len(times) * 0.95) - 1] if times else None,
            "paper_pipeline_seconds": paper_total,
            "upstream_inference_cost_unknown": engine == "mineru",
            "end_to_end_complete": not cached and engine != "mineru",
            "speed_gate_eligible": bool(times) and not cached and engine != "mineru",
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select-dev", help="Read existing development JSON; no parser calls")
    select.add_argument("--dev-root", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)
    offline = sub.add_parser(
        "detect-report", help="Saved-dev detection and MinerU re-verification; no inference"
    )
    offline.add_argument("--selection", type=Path, required=True)
    offline.add_argument("--saved", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    offline.add_argument("--review-labels", type=Path)
    run = sub.add_parser("run", help="Integrator entry point; extracts local PDFs")
    run.add_argument("--pdf", type=Path, required=True)
    run.add_argument("--paper")
    run.add_argument("--pages", type=page_numbers, required=True)
    run.add_argument("--language", choices=("zh", "en"), required=True)
    run.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    run.add_argument(
        "--engines", nargs="+", choices=("mineru", "ppstructure"), default=["mineru", "ppstructure"]
    )
    run.add_argument("--mineru-json", type=Path)
    images = run.add_mutually_exclusive_group(required=True)
    images.add_argument("--page-png-dir", type=Path)
    images.add_argument("--render-python", type=Path)
    run.add_argument("--enable-model", action="store_true")
    run.add_argument(
        "--paddle-python",
        type=Path,
        default=PROJECT / "data/tools/parsers/paddleocr-vl/.venv/Scripts/python.exe",
    )
    run.add_argument(
        "--paddle-cache", type=Path, default=PROJECT / "data/tools/parsers/paddleocr-vl/home"
    )
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("--header-rows", type=int, default=None)
    run.add_argument("--row-header-cols", type=int, default=None)
    packets = sub.add_parser("packets")
    packets.add_argument("--root", type=Path, required=True)
    packets.add_argument("--seed", type=int, default=40)
    scoring = sub.add_parser("score")
    scoring.add_argument("--roots", type=Path, nargs="+", required=True)
    scoring.add_argument("--judge", required=True)
    scoring.add_argument("--provenance", required=True, help="e.g. AI共识+人工仲裁，非人工金标")
    scoring.add_argument("--output", type=Path, required=True)
    perturb = sub.add_parser("perturb")
    perturb.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "select-dev":
        result = select_dev(args.dev_root, args.output)
    elif args.command == "detect-report":
        from .offline import run as offline_run

        result = offline_run(args.selection, args.saved, args.output, args.review_labels)
    elif args.command == "run":
        if len(set(args.engines)) != len(args.engines):
            parser.error("Duplicate engines")
        result = run_pilot(args)
    elif args.command == "packets":
        result = build(args.root, args.seed)
    elif args.command == "score":
        result = score(args.roots, args.judge, args.provenance)
        write(args.output, result)
    else:
        result = {}
        for path in sorted((args.root / "tables").glob("*.json")):
            record = read(path)
            if record["grid"]["verified"]:
                page = read(args.root / "pages" / f"{record['page']:03d}.json")
                result[path.stem] = audit(page, record["candidate"], record["grid"])
        write(args.root / "perturbations.json", result)
        if not result:
            raise SystemExit("No verified baseline tables; perturbation gate not evaluated")
        if any(not v["rejected"] for checks in result.values() for v in checks.values()):
            raise SystemExit("Perturbation gate failed")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
