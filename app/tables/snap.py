"""Ground engine topology in source glyph whitespace, without changing its topology."""

from statistics import median

from .geometry import contains, intersects, valid_box


def snap_cells(cells, chars, region=None):
    if not cells or any(not valid_box(c.get("bbox_pt")) for c in cells):
        return []
    errors, axes = [], []
    for axis, key, span in ((0, "col", "colspan"), (1, "row", "rowspan")):
        count = max(c[key] + c[span] for c in cells)
        edges = []
        for index in range(count + 1):
            values = [c["bbox_pt"][axis] for c in cells if c[key] == index]
            values += [c["bbox_pt"][axis + 2] for c in cells if c[key] + c[span] == index]
            if not values:
                errors.append(f"Ungrounded {key} boundary {index}")
                edges.append(0)
                continue
            edge = median(values)
            if region is not None and index in (0, count):
                edge = region[axis if index == 0 else axis + 2]
            if 0 < index < count:
                # Spanning cells do not constrain the separator they cross.
                crossing = [c for c in cells if c[key] < index < c[key] + c[span]]
                relevant = [
                    g
                    for g in chars
                    if not any(contains(c["bbox_pt"], g["bbox_pt"]) for c in crossing)
                ]
                low = [
                    g["bbox_pt"][axis + 2]
                    for g in relevant
                    if (g["bbox_pt"][axis] + g["bbox_pt"][axis + 2]) / 2 < edge
                ]
                high = [
                    g["bbox_pt"][axis]
                    for g in relevant
                    if (g["bbox_pt"][axis] + g["bbox_pt"][axis + 2]) / 2 >= edge
                ]
                if not low or not high or max(low) >= min(high):
                    errors.append(f"Ungrounded {key} boundary {index}: no glyph gutter")
                else:
                    edge = min(max(edge, max(low) + 0.001), min(high) - 0.001)
            edges.append(edge)
        if any(a >= b for a, b in zip(edges, edges[1:], strict=False)):
            errors.append(f"Reversed {key} boundaries")
        axes.append(edges)
    if errors:
        return errors
    xs, ys = axes
    for cell in cells:
        original = cell["bbox_pt"][:]
        r, c = cell["row"], cell["col"]
        cell["bbox_pt"] = [xs[c], ys[r], xs[c + cell["colspan"]], ys[r + cell["rowspan"]]]
        snapped = cell["bbox_pt"]
        intersection = max(0, min(original[2], snapped[2]) - max(original[0], snapped[0])) * max(
            0, min(original[3], snapped[3]) - max(original[1], snapped[1])
        )
        old_area = (original[2] - original[0]) * (original[3] - original[1])
        new_area = (snapped[2] - snapped[0]) * (snapped[3] - snapped[1])
        iou = intersection / (old_area + new_area - intersection)
        cell["snap_iou"] = iou
        cx, cy = (original[0] + original[2]) / 2, (original[1] + original[3]) / 2
        if iou < 0.5 or not (snapped[0] <= cx <= snapped[2] and snapped[1] <= cy <= snapped[3]):
            errors.append(f"Engine box disagrees with snapped cell r{r}c{c}")
    if errors:
        return errors
    if any(
        intersects(a["bbox_pt"], b["bbox_pt"]) for i, a in enumerate(cells) for b in cells[i + 1 :]
    ):
        return ["Overlapping snapped cells"]
    return []
