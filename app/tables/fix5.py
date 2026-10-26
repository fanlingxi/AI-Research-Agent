"""Offline saved-dev replay and blinded image judge packets. No inference."""

import shutil
from pathlib import Path

from .engines import mineru, mineru_html, pp_grid, read_mineru, region_chars
from .final import final_status
from .io import read, write
from .perturb import perturbations
from .verify import grid_html

OUTPUT = Path(".pytest-tmp/f0-fix5")


def run():
    records, audits, packets, mapping = [], [], [], {}
    selection = read("data/evaluation/table-pilot/dev-f0/selection.json")["papers"]
    structured = {p["paper"]: read_mineru(p["mineru_json"]) for p in selection}
    for file in sorted(Path(".pytest-tmp/f0-fix4").glob("*/tables/*.json")):
        record = read(file)
        paper, identifier = record["id"].split("/")
        candidate = record["candidate"]
        page = read(Path(".pytest-tmp/f0-fix3/pages") / paper / f"{int(identifier[1:4]):03d}.json")
        page["font_aware"] = True
        paths = {f"glyph/{k}": v for k, v in record["votes"].items()}
        raw_file = (
            Path("data/evaluation/table-pilot/dev-f0d") / paper / "raw" / f"{identifier}.json"
        )
        raw = read(raw_file) if raw_file.exists() else None
        for engine in ("mineru", "ppstructure"):
            try:
                paths[f"old/{engine}"] = (
                    mineru(mineru_html(structured[paper], page, candidate), page, candidate)
                    if engine == "mineru"
                    else pp_grid(
                        raw,
                        [page["width_pt"], page["height_pt"]],
                        candidate,
                        region_chars(page, candidate),
                    )
                )
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                paths[f"old/{engine}"] = {"cells": [], "errors": [str(exc)]}
        final = final_status(page, candidate, paths)
        records.append({"id": record["id"], **final})
        write(OUTPUT / paper / "tables" / f"{identifier}.json", records[-1])
        if not final["grid"]["verified"]:
            continue
        for name, changed in perturbations(final["grid"]):
            checked = final_status(page, candidate, {final["selected_path"]: changed})
            audits.append(
                {
                    "id": record["id"],
                    "kind": name,
                    "rejected": not checked["grid"]["verified"],
                    "failures": checked["grid"]["verification"]["failures"],
                }
            )
        key = f"t{len(packets) + 1:03d}"
        image = OUTPUT / "packets" / f"{key}.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            Path("data/evaluation/table-pilot/dev-f0d") / paper / "crops" / f"{identifier}.png",
            image,
        )
        grid = final["grid"]
        body = grid_html(grid)
        (image.with_suffix(".html")).write_text(body, encoding="utf-8")
        packets.append(
            {
                "packet_id": key,
                "image_path": image.name,
                "html": body,
                "cell_count": len(grid["cells"]),
                "cells": [{"row": c["row"], "col": c["col"]} for c in grid["cells"]],
            }
        )
        mapping[key] = {"id": record["id"], "selected_path": final["selected_path"]}
    write(OUTPUT / "packets/manifest.json", {"packets": packets})
    write(OUTPUT / "private/blind-map.json", mapping)
    prompt = (
        "Compare the HTML table to the image cell by cell. The HTML is the claim; "
        'the image is the truth. Return JSON: {"packet_id": "t001", '
        '"cell_errors": [{"row": 0, "col": 0, "kind": '
        '"text|position|span|missing|extra", "note": "..."}], '
        '"table_correct": false}. Use zero-based row/col coordinates. Whitespace, '
        "typographic quote/dash variants, and sub/superscript flattening "
        "(E5mistral-7b) are not errors."
    )
    (OUTPUT / "packets/prompt.txt").write_text(prompt, encoding="utf-8")
    report = {
        "tables": len(records),
        "verified": len(packets),
        "per_path": {
            k: sum(r["paths"][k]["verified"] for r in records) for k in records[0]["paths"]
        },
        "perturbations": audits,
        "perturbation_summary": {
            "total": len(audits),
            "rejected": sum(a["rejected"] for a in audits),
        },
        "disagreements": [
            {"id": r["id"], "differences": r["content_differences"]}
            for r in records
            if r["engine_disagreement"]
        ],
    }
    requested = {"2025.ccl-1.12/p008-t001", "2025.ccl-1.12/p009-t001", "e5-mistral/p006-t001"}
    report["round4_disagreement_tables"] = [
        {
            "id": r["id"],
            "verified": r["grid"]["verified"],
            "content_differences": r["content_differences"],
            "interpretation": "nonempty glyph ownership or header paths differ"
            if r["engine_disagreement"]
            else "passing paths agree modulo empty cells",
            "passing_paths": {
                name: {
                    "header_rows": g["header_rows"],
                    "row_header_cols": g["row_header_cols"],
                    "cells": [
                        {k: c[k] for k in ("row", "col", "rowspan", "colspan", "text", "char_refs")}
                        for c in g["cells"]
                    ],
                }
                for name, g in r["paths"].items()
                if g["verified"]
            },
        }
        for r in records
        if r["id"] in requested
    ]
    write(OUTPUT / "report.json", report)
    print({k: report[k] for k in ("tables", "verified", "per_path", "perturbation_summary")})
    if any(not a["rejected"] for a in audits):
        raise RuntimeError("Perturbation gate failed")


if __name__ == "__main__":
    run()
