"""Deterministic caption, ruling and numeric-column candidate signals."""

from __future__ import annotations

import re

from .chars import text_lines
from .geometry import intersects, overlap, union

# CCL fonts encode punctuation/digits at two fixed Unicode offsets.
CAPTION = re.compile(
    r"^\s*(?:Table\s*[A-Z]?\d+\s*[:.]|表\s*[0-9一二三四五六七八九十]+\s*[:：.]"
    r"|(?:表|\u5b54\u5b61\u5b62\u5b6c\u5b65|\u5754\u5761\u5762\u576c\u5765)"
    r"\s*[\u5b30-\u5b39\u5730-\u5739]+\s*[\u5b3a\u573a:：])",
    re.I,
)
FIGURE = re.compile(
    r"^\s*(?:Figure\s*\d|Fig\.|图\s*\d|"
    r"\u5b46\u5b69\u5b67\u5b75\u5b72\u5b65|\u5746\u5769\u5767\u5775\u5772\u5765)",
    re.I,
)


def caption_distance(a, b):
    if min(a[2], b[2]) <= max(a[0], b[0]):
        return float("inf")
    return max(0, max(a[1], b[1]) - min(a[3], b[3]))


def ruling_groups(page, lines):
    rules = [
        r["bbox_pt"]
        for r in page.get("rules", [])
        if r["bbox_pt"][3] - r["bbox_pt"][1] <= 2 and r["bbox_pt"][2] - r["bbox_pt"][0] >= 50
    ]
    groups = []
    for box in sorted(rules, key=lambda b: b[1]):
        target = None
        for group in groups:
            prev = group[-1]
            compatible = (
                (abs(prev[0] - box[0]) <= 4 and abs(prev[2] - box[2]) <= 4)
                or (prev[0] - 4 <= box[0] and box[2] <= prev[2] + 4)
                or (box[0] - 4 <= prev[0] and prev[2] <= box[2] + 4)
            )
            blocked = False
            for line in lines:
                b, text = line["bbox_pt"], line["text"].strip()
                if prev[3] < b[1] and b[3] < box[1] and caption_distance(prev, b) < float("inf"):
                    prose = (
                        b[2] - b[0] >= 0.9 * (prev[2] - prev[0])
                        and text
                        and (text[0].islower() or "\u4e00" <= text[0] <= "\u9fff")
                    )
                    if CAPTION.match(text) or FIGURE.match(text) or prose:
                        blocked = True
            if compatible and not blocked:
                target = group
                break
        if target is None:
            groups.append([box])
        else:
            target.append(box)
    return groups


def layout_fallback(page):
    """Reserved comparison port. No implicit model calls."""
    return []


def detect(page, fallback=None):
    lines = [
        {
            "bbox_pt": union(c["bbox_pt"] for c in line),
            "text": "".join(c["text"] for c in line),
            "chars": line,
        }
        for line in text_lines(page["chars"])
    ]
    captions = [dict(line) for line in lines if CAPTION.match(line["text"])]
    for cap in captions:
        for line in lines:
            b, cb = line["bbox_pt"], cap["bbox_pt"]
            if (
                0 <= b[1] - cb[3] <= 4
                and abs(b[0] - cb[0]) <= 8
                and not CAPTION.match(line["text"])
                and not FIGURE.match(line["text"])
                and not any(
                    cb[3] <= r["bbox_pt"][1] <= b[1]
                    for r in page.get("rules", [])
                    if r["bbox_pt"][2] - r["bbox_pt"][0] >= 50
                )
            ):
                cap["bbox_pt"] = union([cb, b])
                cap["text"] += " " + line["text"]
    signals = []
    figures = [line for line in lines if FIGURE.match(line["text"])]
    rejected = page["rejected"] = []
    for group in ruling_groups(page, lines):
        box = union(group)
        if len(group) >= 2 and box[3] - box[1] >= 12:
            vertical = [
                r["bbox_pt"]
                for r in page.get("rules", [])
                if r["bbox_pt"][2] - r["bbox_pt"][0] <= 2
                and intersects([box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2], r["bbox_pt"])
            ]
            signals.append(
                {
                    "bbox_pt": union([box, *vertical]),
                    "sources": ["rulings"],
                    "rule_ys": sorted(set(b[1] for b in group)),
                }
            )
    for rule in page.get("rules", []):
        b = rule["bbox_pt"]
        if (
            rule.get("kind") in {"LTRect", "LTCurve", "LTImage", "LTFigure"}
            and b[2] - b[0] > 40
            and b[3] - b[1] > 20
        ):
            if not any(overlap(b, s["bbox_pt"]) > 0.8 for s in signals):
                rejected.append({"bbox_pt": b, "reason": "Non-grid graphic content"})

    numeric = []
    numeric_lines = [
        {
            "bbox_pt": union(c["bbox_pt"] for c in line),
            "text": "".join(c["text"] for c in line),
            "chars": line,
        }
        for line in text_lines(page["chars"], segments=False)
    ]
    for line in numeric_lines:
        text = line["text"]
        if CAPTION.match(text):
            continue
        chars = line["chars"]
        runs = []
        for char in chars:
            if char["text"].isdigit():
                if not runs or char["bbox_pt"][0] - runs[-1][-1]["bbox_pt"][2] > 12:
                    runs.append([])
                runs[-1].append(char)
        if len(runs) >= 2 and sum(c.isdigit() for c in text) / max(1, len(text)) >= 0.25:
            numeric.append((line, [r[0]["bbox_pt"][0] for r in runs]))
    clusters = []
    for line, xs in numeric:
        if clusters and line["bbox_pt"][1] - clusters[-1][-1][0]["bbox_pt"][3] <= 24:
            previous = clusters[-1][-1][1]
            if sum(any(abs(x - y) <= 8 for y in previous) for x in xs) >= 2:
                clusters[-1].append((line, xs))
                continue
        clusters.append([(line, xs)])
    for cluster in clusters:
        if len(cluster) >= 2:
            box = union(line["bbox_pt"] for line, _ in cluster)
            # Include nearby nonnumeric header rows, stopping at prose/captions/large gaps.
            for line in reversed(lines):
                b = line["bbox_pt"]
                if b[3] <= box[1] and 0 <= box[1] - b[3] <= 18:
                    if CAPTION.match(line["text"]) or b[0] < box[0] - 20 or b[2] > box[2] + 20:
                        break
                    box = union([box, b])
            ruled = [
                s for s in signals if "rulings" in s["sources"] and overlap(box, s["bbox_pt"]) > 0.1
            ]
            if ruled:
                max(ruled, key=lambda s: overlap(box, s["bbox_pt"]))["sources"].append(
                    "numeric_columns"
                )
            else:
                signals.append({"bbox_pt": box, "sources": ["numeric_columns"]})

    for cap in captions:
        near = [
            s
            for s in signals
            if min(
                abs(s["bbox_pt"][1] - cap["bbox_pt"][3]), abs(cap["bbox_pt"][1] - s["bbox_pt"][3])
            )
            <= 30
            and min(s["bbox_pt"][2], cap["bbox_pt"][2]) > max(s["bbox_pt"][0], cap["bbox_pt"][0])
        ]
        if near:
            target = min(
                near,
                key=lambda s: min(
                    abs(s["bbox_pt"][1] - cap["bbox_pt"][3]),
                    abs(cap["bbox_pt"][1] - s["bbox_pt"][3]),
                ),
            )
            target.update(caption=cap["text"], caption_bbox_pt=cap["bbox_pt"])
            target["sources"].append("caption")
    merged = []
    for signal in [*signals, *(fallback(page) if fallback else [])]:
        target = next(
            (
                s
                for s in merged
                if overlap(s["bbox_pt"], signal["bbox_pt"]) > 0.5
                and (
                    not s.get("caption")
                    or not signal.get("caption")
                    or s["caption"] == signal["caption"]
                )
            ),
            None,
        )
        if target is None:
            merged.append(dict(signal))
        else:
            if "rulings" not in target["sources"]:
                target["bbox_pt"] = (
                    signal["bbox_pt"]
                    if "rulings" in signal["sources"]
                    else union([target["bbox_pt"], signal["bbox_pt"]])
                )
            target["sources"] = sorted(set(target["sources"] + signal["sources"]))
            if signal.get("caption"):
                target.update({k: signal[k] for k in ("caption", "caption_bbox_pt")})
    kept = []
    for candidate in merged:
        figure_gap = min(
            (caption_distance(candidate["bbox_pt"], f["bbox_pt"]) for f in figures),
            default=float("inf"),
        )
        table_gap = (
            caption_distance(candidate["bbox_pt"], candidate["caption_bbox_pt"])
            if candidate.get("caption")
            else float("inf")
        )
        nearby_figure = figure_gap <= 30 and figure_gap < table_gap
        bars = [
            r
            for r in page.get("rules", [])
            if r.get("kind") == "LTRect"
            and overlap(candidate["bbox_pt"], r["bbox_pt"]) > 0.5
            and 2
            < r["bbox_pt"][2] - r["bbox_pt"][0]
            < 0.2 * (candidate["bbox_pt"][2] - candidate["bbox_pt"][0])
            and r["bbox_pt"][3] - r["bbox_pt"][1]
            > max(10, 1.5 * (r["bbox_pt"][2] - r["bbox_pt"][0]))
        ]
        graphic = len(bars) >= 4 or any(
            overlap(candidate["bbox_pt"], r["bbox_pt"]) > 0.8 for r in rejected
        )
        if nearby_figure or len(bars) >= 4 or (graphic and "rulings" not in candidate["sources"]):
            rejected.append(
                {
                    **candidate,
                    "reason": "Nearby figure caption"
                    if nearby_figure
                    else "Non-grid graphic content",
                }
            )
        else:
            kept.append(candidate)
    for i, candidate in enumerate(sorted(kept, key=lambda s: s["bbox_pt"][1])):
        b = candidate["bbox_pt"]
        candidate.update(
            id=f"p{page['page']:03d}-t{i + 1:03d}",
            page=page["page"],
            fragment=bool(re.search(r"continued|续", candidate.get("caption", ""), re.I))
            or bool(
                page.get("table_continues_from_previous") and "rulings" not in candidate["sources"]
            ),
            captioned=bool(candidate.get("caption")),
            caption=candidate.get("caption", ""),
        )
        candidate["bbox_pt"] = [
            max(0, b[0] - 2),
            max(0, b[1] - 2),
            min(page["width_pt"], b[2] + 2),
            min(page["height_pt"], b[3] + 2),
        ]
        yield candidate
