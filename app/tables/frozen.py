"""Frozen integrator entry point. Only explicit run invokes the persistent local model."""

import argparse
from pathlib import Path
from types import SimpleNamespace

from .io import read, write
from .pilot import PROJECT, run_pilot
from .verify import VERSION


def run(args, pipeline=run_pilot):
    selection = read(args.selection)
    papers = [p for p in selection["papers"] if p["paper"] == args.paper]
    if len(papers) != 1:
        raise ValueError("Select exactly one paper from the selection file")
    paper = papers[0]
    if not args.enable_model:
        raise ValueError("Frozen run requires explicit --enable-model (integrator only)")
    result = pipeline(
        SimpleNamespace(
            pdf=Path(paper["pdf"]),
            paper=args.paper,
            pages=paper["pages"],
            language=paper["language"],
            root=args.output,
            engines=["mineru", "ppstructure"],
            mineru_json=args.mineru_dir,
            page_png_dir=None,
            render_python=args.render_python,
            enable_model=True,
            paddle_python=args.paddle_python,
            paddle_cache=args.paddle_cache,
            timeout=args.timeout,
            header_rows=None,
            row_header_cols=None,
            split=selection.get("split", "unspecified"),
        )
    )
    manifest_path = args.output / "run.json"
    manifest = read(manifest_path)
    manifest.update(
        code_version=VERSION,
        selection=str(args.selection),
        split=selection.get("split", "unspecified"),
    )
    tables = [table for page in manifest["pages"] for table in page["tables"]]
    # Actual paper wall time includes both structure paths, failures, and final verification.
    for table in tables:
        record = read(args.output / table["final"])
        record["frozen_timing"] = {
            "paper_wall_seconds": manifest["elapsed_seconds"],
            "end_to_end_amortized_seconds": manifest["elapsed_seconds"] / len(tables),
            "allocation": (
                "equal share of measured paper wall time, including failures and cold start"
            ),
            "resources_scope": "paper process tree sampled peak; not a per-table peak",
            "resources": manifest["resources"],
            "saved_mineru_upstream_inference_seconds": None,
        }
        write(args.output / table["final"], record)
    write(manifest_path, manifest)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--paper", required=True)
    parser.add_argument("--mineru-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--render-python", type=Path, required=True)
    parser.add_argument("--paddle-python", type=Path, required=True)
    parser.add_argument(
        "--paddle-cache", type=Path, default=PROJECT / "data/tools/parsers/paddleocr-vl/home"
    )
    parser.add_argument("--enable-model", action="store_true")
    parser.add_argument("--timeout", type=float, default=300)
    print(run(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
