"""Adapters. All public bboxes are PDF points, origin top left, x right / y down."""

from __future__ import annotations

import html
import math
import re

TYPES = (
    "title front_matter abstract keywords heading paragraph header footer page_number "
    "footnote references caption table formula figure other"
).split()
# Explicit, reviewable source-label tables. Unknown labels stay `other`.
MINERU_TYPES = {
    **{t: t for t in TYPES},
    "text": "paragraph",
    "paragraph_title": "heading",
    "doc_title": "title",
    "page_footnote": "footnote",
    "equation": "formula",
    "equation_inline": "other",
    "image": "figure",
    "table_caption": "caption",
    "image_caption": "caption",
    "reference": "references",
    "list": "paragraph",
}
PADDLE_TYPES = {
    **{t: t for t in TYPES},
    "text": "paragraph",
    "doc_title": "title",
    "paragraph_title": "heading",
    "abstract_title": "heading",
    "reference": "references",
    "reference_content": "references",
    "reference_title": "references",
    "vision_footnote": "footnote",
    "figure_title": "caption",
    "table_title": "caption",
    "chart_title": "caption",
    "image": "figure",
    "chart": "figure",
    "display_formula": "formula",
    "inline_formula": "other",
    "formula_number": "other",
    "number": "page_number",
    "header_image": "header",
    "footer_image": "footer",
    "seal": "other",
    "algorithm": "other",
    "aside_text": "other",
    "footnote": "footnote",
}


def bbox_points(box, width, height, source_width=1, source_height=1):
    if box is None:
        return None
    values = [*box, width, height, source_width, source_height]
    if len(box) != 4 or not all(math.isfinite(float(v)) for v in values):
        raise ValueError("Invalid bbox or page dimensions")
    if min(width, height, source_width, source_height) <= 0:
        raise ValueError("Page dimensions must be positive")
    x0, y0, x1, y1 = map(float, box)
    if not (0 <= x0 <= x1 <= source_width * 1.01 and 0 <= y0 <= y1 <= source_height * 1.01):
        raise ValueError("BBox exceeds declared coordinate space")
    return [
        x0 * width / source_width,
        y0 * height / source_height,
        x1 * width / source_width,
        y1 * height / source_height,
    ]


def content_text(value):
    if isinstance(value, list):
        return "".join(content_text(v) for v in value)
    if isinstance(value, dict):
        return content_text(value.get("content", ""))
    return str(value or "")


def block(raw, kind, box, index, content):
    text = content_text(content)
    result = {
        "id": f"b{index:04d}",
        "type": kind,
        "order": index,
        "bbox_pt": box,
        "text": text,
        "source_type": raw,
    }
    if kind == "table":
        result["html"] = text
        result["text"] = html.unescape(re.sub(r"<[^>]*>", " ", text))
    elif kind == "formula":
        result["latex"] = text
    return result


def page_record(parser, version, config, paper, page, width, height, blocks):
    return {
        "parser": parser,
        "parser_version": version,
        "config": config,
        "paper": paper,
        "page": page,
        "page_width_pt": width,
        "page_height_pt": height,
        "coordinate_system": "top-left-pdf-points",
        "blocks": blocks,
    }


def mineru_pages(structured, middle, paper):
    geometry = middle["extensions"]["docvortex_layout"]["pages"]
    dimensions = {p["page_idx"]: (p["width_pt"], p["height_pt"]) for p in geometry}
    version = structured["metadata"]["producer"]["version"]
    pages = []
    for page in structured["pages"]:
        width, height = dimensions[page["page_idx"]]
        blocks = []
        for raw in page["blocks"]:
            kind = MINERU_TYPES.get(raw["type"], "other")
            blocks.append(
                block(
                    raw["type"],
                    kind,
                    bbox_points(raw.get("bbox"), width, height),
                    len(blocks),
                    raw.get("content", ""),
                )
            )
            for name, child_kind in (("captions", "caption"), ("footnotes", "footnote")):
                for child in raw.get(name, []):
                    blocks.append(
                        block(
                            name,
                            child_kind,
                            bbox_points(child.get("bbox"), width, height),
                            len(blocks),
                            child.get("content", ""),
                        )
                    )
        pages.append(
            page_record(
                "mineru",
                version,
                {"tier": "standard"},
                paper,
                page["page_idx"] + 1,
                width,
                height,
                blocks,
            )
        )
    return pages


def paddle_page(
    raw, paper, page, width, height, image_width, image_height, version="PaddleOCR-VL-1.6"
):
    # The runner always predicts a single rendered PNG. Do not guess PDF page indexing.
    payload = raw.get("res", raw)
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    blocks = []
    for item in payload["parsing_res_list"]:
        label = item["block_label"]
        blocks.append(
            block(
                label,
                PADDLE_TYPES.get(label, "other"),
                bbox_points(item.get("block_bbox"), width, height, image_width, image_height),
                len(blocks),
                item.get("block_content", ""),
            )
        )
    return page_record(
        "paddle",
        version,
        {"pipeline_version": "v1.6", "dpi": 150},
        paper,
        page,
        width,
        height,
        blocks,
    )
