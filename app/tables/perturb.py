"""Reproducible corruptions applied to a verified grid; always re-run all checks."""

from copy import deepcopy

from .geometry import union
from .verify import verify


def perturbations(grid):
    if not grid.get("verified"):
        raise ValueError("Perturbations require a verified baseline")
    for name in ("swap_columns", "move_row", "merge_cells", "split_cell", "drop_cell"):
        changed = deepcopy(grid)
        cells = changed["cells"]
        if name == "swap_columns":
            for cell in cells:
                if cell["col"] in (0, 1):
                    cell["col"] = 1 - cell["col"]
        elif name == "move_row":
            cells[-1]["row"] += 1
        elif name == "merge_cells":
            pair = next(
                (
                    (i, j)
                    for i, a in enumerate(cells)
                    for j, b in enumerate(cells)
                    if a["row"] >= grid.get("header_rows", 1)
                    and a["row"] == b["row"]
                    and a["rowspan"] == b["rowspan"]
                    and a["col"] + a["colspan"] == b["col"]
                ),
                None,
            )
            if pair is None:
                # Single-column tables still admit a vertical merge corruption.
                pair = next(
                    (
                        (i, j)
                        for i, a in enumerate(cells)
                        for j, b in enumerate(cells)
                        if a["col"] == b["col"]
                        and a["colspan"] == b["colspan"]
                        and a["row"] + a["rowspan"] == b["row"]
                    ),
                    None,
                )
                if pair is None:
                    raise ValueError("No adjacent cells to merge")
                i, j = pair
                cells[i]["rowspan"] += cells[j]["rowspan"]
                cells[i]["bbox_pt"] = union([cells[i]["bbox_pt"], cells[j]["bbox_pt"]])
                cells[i].pop("model_text", None)
                cells.pop(j)
                yield name, changed
                continue
            i, j = pair
            cells[i]["bbox_pt"] = union([cells[i]["bbox_pt"], cells[j]["bbox_pt"]])
            cells[i]["colspan"] += cells[j]["colspan"]
            cells[i].pop("model_text", None)
            cells.pop(j)
        elif name == "split_cell":
            extra = deepcopy(cells[-1])
            box = cells[-1]["bbox_pt"]
            midpoint = (box[0] + box[2]) / 2
            cells[-1]["bbox_pt"] = [box[0], box[1], midpoint, box[3]]
            extra["bbox_pt"] = [midpoint, box[1], box[2], box[3]]
            extra["col"] += 1
            cells[-1].pop("model_text", None)
            extra.pop("model_text", None)
            cells.append(extra)
        else:
            cells.pop()
        yield name, changed


def audit(page, candidate, grid):
    results = {}
    for name, changed in perturbations(grid):
        result = verify(
            page,
            candidate,
            changed,
            header_rows=grid.get("header_rows", 1),
            row_header_cols=grid.get("row_header_cols", 1),
        )
        results[name] = {
            "rejected": not result["verified"],
            "failures": result["verification"]["failures"],
        }
    return results
