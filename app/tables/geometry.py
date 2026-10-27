"""Coordinates: displayed MediaBox, PDF points, top left; page numbers are 1-based."""

from __future__ import annotations

import math


def valid_box(box):
    return (
        isinstance(box, (list, tuple))
        and len(box) == 4
        and all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
        and box[0] < box[2]
        and box[1] < box[3]
    )


def union(boxes):
    boxes = list(boxes)
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def contains(box, other, tolerance=0):
    return (
        box[0] - tolerance <= other[0]
        and box[1] - tolerance <= other[1]
        and box[2] + tolerance >= other[2]
        and box[3] + tolerance >= other[3]
    )


def intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def overlap(a, b):
    area = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    area *= max(0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return area / small if small > 0 else 0


def pixel_box(box, page_size_pt, image_size_px):
    """Round outward; the actual integer crop origin must be used on the return trip."""
    if not valid_box(box) or min(*page_size_pt, *image_size_px) <= 0:
        raise ValueError("Invalid crop or page dimensions")
    sx, sy = [pixels / points for pixels, points in zip(image_size_px, page_size_pt, strict=True)]
    if not contains([0, 0, *page_size_pt], box):
        raise ValueError("Crop outside page")
    return [
        math.floor(box[0] * sx),
        math.floor(box[1] * sy),
        math.ceil(box[2] * sx),
        math.ceil(box[3] * sy),
    ]


def crop_box_to_points(box, crop_px, page_size_pt, image_size_px):
    if len(box) == 4 and all(isinstance(v, (list, tuple)) and len(v) == 2 for v in box):
        box = [v for point in box for v in point]
    if len(box) == 8:  # Paddle quadrilateral, not x0,y0,x1,y1.
        box = [min(box[::2]), min(box[1::2]), max(box[::2]), max(box[1::2])]
    if not valid_box(box) or min(*page_size_pt, *image_size_px) <= 0:
        raise ValueError("Invalid engine cell box")
    if not contains([0, 0, crop_px[2] - crop_px[0], crop_px[3] - crop_px[1]], box, 1):
        raise ValueError("Engine cell outside crop")
    sx, sy = [points / pixels for points, pixels in zip(page_size_pt, image_size_px, strict=True)]
    return [
        (box[0] + crop_px[0]) * sx,
        (box[1] + crop_px[1]) * sy,
        (box[2] + crop_px[0]) * sx,
        (box[3] + crop_px[1]) * sy,
    ]
