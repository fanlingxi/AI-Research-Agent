"""Offline preparation/scoring and explicitly invoked parser/judge runners for D039-B."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .align import align_page
from .common import DEFAULT_ROOT, MINERU_PYTHON, PADDLE_PYTHON, PARSERS, read, write
from .normalize import mineru_pages, paddle_page


def normalize(root):
    for paper in read(root / "manifest.json")["papers"]:
        name = paper["paper"]
        expected = read(root / "prepared" / f"{name}.json")
        mineru_folder = root / "raw/mineru" / name
        if (mineru_folder / "structured_content.json").exists():
            # Also accept the documented full-range job outputs imported by the integrator.
            normalized = mineru_pages(
                read(mineru_folder / "structured_content.json"),
                read(mineru_folder / "middle_json.json"),
                name,
            )
        else:
            normalized = []
            for page in expected:
                folder = mineru_folder / f"{page['page']:03d}"
                normalized.extend(
                    mineru_pages(
                        read(folder / "structured_content.json"),
                        read(folder / "middle_json.json"),
                        name,
                    )
                )
        validate_pages(expected, normalized)
        write(root / "normalized/mineru" / f"{name}.json", normalized)
        normalized = []
        for page in expected:
            raw = read(root / "raw/paddle" / name / f"{page['page']:03d}.json")
            normalized.append(
                paddle_page(
                    raw["raw"],
                    name,
                    page["page"],
                    page["width_pt"],
                    page["height_pt"],
                    raw["image_width"],
                    raw["image_height"],
                    raw["parser_version"],
                )
            )
        validate_pages(expected, normalized)
        write(root / "normalized/paddle" / f"{name}.json", normalized)


def validate_pages(expected, actual):
    if len(actual) != len(expected) or {p["page"] for p in actual} != {p["page"] for p in expected}:
        raise ValueError("Parser output pages do not match the fixed physical page slice")
    for page in actual:
        reference = next(p for p in expected if p["page"] == page["page"])
        if any(
            abs(page[f"page_{key}_pt"] - reference[f"{key}_pt"]) > 1 for key in ("width", "height")
        ):
            raise ValueError("Parser output dimensions differ from prepared PDF")


def align(root):
    for paper in read(root / "manifest.json")["papers"]:
        name = paper["paper"]
        prepared = read(root / "prepared" / f"{name}.json")
        lines = {p["page"]: p["lines"] for p in prepared}
        for parser in PARSERS:
            normalized = read(root / "normalized" / parser / f"{name}.json")
            validate_pages(prepared, normalized)
            write(
                root / "aligned" / parser / f"{name}.json",
                [align_page(lines[p["page"]], p) for p in normalized],
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    # --root follows the subcommand consistently across all examples.
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for name in (
        "manifest",
        "prepare",
        "run-mineru",
        "run-paddle",
        "normalize",
        "align",
        "packets",
        "judge-deepseek",
        "score",
        "zoning-score",
    ):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=DEFAULT_ROOT)
        commands[name] = command
    for name in ("manifest", "prepare"):
        commands[name].add_argument("--zh-dir", type=Path)
        commands[name].add_argument("--en-dir", type=Path)
    commands["prepare"].add_argument("--papers", type=Path)
    commands["prepare"].add_argument("--render-python", type=Path, default=MINERU_PYTHON)
    commands["run-mineru"].add_argument("--base-url", default="http://127.0.0.1:16580")
    commands["run-mineru"].add_argument("--service-pid", type=int, required=True)
    commands["run-mineru"].add_argument("--cold-start-seconds", type=float)
    commands["run-paddle"].add_argument("--python", type=Path, default=PADDLE_PYTHON)
    commands["run-paddle"].add_argument("--cpu", action="store_true")
    for name in ("run-mineru", "run-paddle"):
        commands[name].add_argument("--max-pages", type=int, default=150)
    commands["packets"].add_argument("--lines-only", action="store_true")
    commands["packets"].add_argument("--seed", type=int, default=39)
    commands["packets"].add_argument("--max-images", type=int, default=40)
    commands["judge-deepseek"].add_argument("--max-calls", type=int, default=12)
    commands["score"].add_argument("--line-judges", nargs=2, default=["deepseek", "qwen-text"])
    commands["score"].add_argument(
        "--image-judges", nargs=2, default=["doubao-image", "qwen-image"]
    )
    commands["score"].add_argument("--arbitration", type=Path)
    commands["zoning-score"].add_argument("--versions", nargs="+",
                                         choices=("pypdf-v3", "pypdf-v4"),
                                         default=["pypdf-v3", "pypdf-v4"])
    commands["zoning-score"].add_argument("--out", type=Path,
                                         default=Path(".pytest-tmp/zoning-dev"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    result = None
    if args.command in {"manifest", "prepare"}:
        from .prepare import corpus, manifest, prepare

        entries = corpus(args.zh_dir, args.en_dir)
        split = "development"
        if args.command == "prepare" and args.papers:
            from .prepare import paper_list

            if args.zh_dir or args.en_dir:
                parser.error("--papers cannot be combined with --zh-dir/--en-dir")
            explicit = paper_list(args.papers)
            entries, split = explicit["papers"], explicit["split"]
        result = (
            manifest(root, entries)
            if args.command == "manifest"
            else prepare(root, args.render_python, entries, split=split)
        )
    elif args.command == "run-mineru":
        from .runners import run_mineru

        result = run_mineru(
            root, args.service_pid, args.base_url, args.max_pages, args.cold_start_seconds
        )
    elif args.command == "run-paddle":
        from .runners import run_paddle

        result = run_paddle(root, args.python, args.max_pages, cpu=args.cpu)
    elif args.command == "normalize":
        normalize(root)
    elif args.command == "align":
        align(root)
    elif args.command == "packets":
        from .packets import packets

        result = packets(root, args.seed, args.max_images, lines_only=args.lines_only)
    elif args.command == "judge-deepseek":
        from .judge import judge_deepseek

        result = judge_deepseek(root, args.max_calls)
    elif args.command == "zoning-score":
        from .zoning import zoning_score

        result = zoning_score(root, args.out, args.versions)
    elif args.command == "score":
        from .score import score

        result = score(root, args.line_judges, args.image_judges, args.arbitration)
    print(
        json.dumps(result or {"status": "completed", "command": args.command}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
