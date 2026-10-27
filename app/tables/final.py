"""Shared final rule for independently verified candidate paths."""

from copy import deepcopy

from .chars import compact
from .verify import verify


def content(grid):
    cells = grid["cells"]
    associations = grid["verification"]["checks"]["header_value_associations"]["value"][
        "associations"
    ]
    paths = {a["cell"]: a for a in associations}
    result = set()
    for i, cell in enumerate(cells):
        if not compact(cell.get("text", "")):
            continue
        association = paths.get(i, {})
        result.add(
            (
                tuple(sorted(r["char_id"] for r in cell["char_refs"])),
                tuple(compact(cells[h]["text"]) for h in association.get("column_headers", [])),
                tuple(compact(cells[h]["text"]) for h in association.get("row_headers", [])),
            )
        )
    return result


def final_status(page, candidate, paths, **options):
    checked = {
        name: verify(
            page,
            candidate,
            grid,
            **{
                "header_rows": grid.get("header_rows"),
                "row_header_cols": grid.get("row_header_cols"),
                **options,
            },
        )
        for name, grid in paths.items()
    }
    passing = {name: grid for name, grid in checked.items() if grid["verified"]}
    signatures = {name: content(grid) for name, grid in passing.items()}
    disagreement = any(s != next(iter(signatures.values())) for s in signatures.values())
    selected = (
        max(
            passing or checked,
            key=lambda n: (
                (passing or checked)[n].get("agreement_score", 0),
                (passing or checked)[n].get("vote_passed", False),
            ),
        )
        if checked
        else None
    )
    grid = deepcopy(checked[selected]) if selected else verify(page, candidate, {"cells": []})
    if disagreement:
        grid.setdefault("errors", []).append("engine disagreement")
        grid = verify(page, candidate, grid, **options)
    if grid["verified"]:
        grid.setdefault(
            "vote_topology",
            sorted((c["row"], c["col"], c["rowspan"], c["colspan"]) for c in grid["cells"]),
        )
    return {
        "grid": grid,
        "paths": checked,
        "selected_path": selected,
        "agreeing_paths": list(passing) if not disagreement else [],
        "engine_disagreement": disagreement,
        "content_differences": {
            name: {"only_here": sorted(signature - set.intersection(*signatures.values()))}
            for name, signature in signatures.items()
        }
        if disagreement
        else {},
    }
