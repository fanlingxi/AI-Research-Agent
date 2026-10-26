"""Conservative same-page text alignment with spatial disambiguation."""

from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher

LABELS = (
    "front_matter header footer page_number references body caption table_text formula_text other"
).split()
EXCLUDED = {"front_matter", "header", "footer", "page_number", "references"}
LINE_TYPES = {
    "title": "front_matter",
    "table": "table_text",
    "formula": "formula_text",
    **{t: t for t in LABELS},
    **dict.fromkeys(("paragraph", "abstract", "keywords", "heading"), "body"),
}


def text_key(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"[\s*$`]+", "", text).casefold()


def spatial_match(line, box):
    if box is None or line.get("anchor_pt") is None:
        return None
    x, y = line["anchor_pt"]
    size = line.get("font_size_pt") or 10
    # The baseline's font-height band must overlap the block vertically;
    # allow a small horizontal tolerance for OCR and leading indentation.
    return box[0] - size <= x <= box[2] + size and y >= box[1] and y - size <= box[3]


def align_page(lines, page):
    predictions, seen = [], set()
    for line in lines:
        if line["page"] != page["page"]:
            raise ValueError("Alignment must stay within one physical page")
        key = text_key(line["text"])
        candidates = []
        for b in page["blocks"]:
            if b["type"] == "figure":
                continue
            spatial = spatial_match(line, b.get("bbox_pt"))
            if spatial is False:
                continue
            # Baseline raw spans disambiguate repeated text without approximate geometry.
            if "spans" in b and not any(s["start"] <= line["start"] < s["end"] for s in b["spans"]):
                continue
            target = text_key(b["text"])
            if not key or not target:
                continue
            exact = key in target
            similarity = SequenceMatcher(None, key, target, autojunk=False).ratio()
            if exact or (spatial and similarity >= 0.82):
                score = (2 if exact else similarity) + (0.25 if spatial else 0)
                candidates.append((score, b))
        candidates.sort(key=lambda pair: (-pair[0], pair[1]["order"]))
        # Equal evidence for multiple repeated blocks is uncertain, not an arbitrary hit.
        chosen = (
            candidates[0][1]
            if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0])
            else None
        )
        label = LINE_TYPES.get(chosen["type"], "other") if chosen else "unaligned"
        start = bool(
            chosen
            and label == "body"
            and chosen["id"] not in seen
            and not chosen.get("continuation", False)
        )
        predictions.append(
            {
                "id": line["id"],
                "page": line["page"],
                "label": label,
                "paragraph_start": start,
                "block_id": chosen["id"] if chosen else None,
            }
        )
        if chosen:
            seen.add(chosen["id"])
    return {
        "parser": page["parser"],
        "paper": page["paper"],
        "page": page["page"],
        "lines": predictions,
        "unaligned": sum(p["label"] == "unaligned" for p in predictions),
    }
