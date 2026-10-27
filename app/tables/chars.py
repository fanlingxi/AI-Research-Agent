"""Lazy local PDF extraction and conservative, whitespace-only source alignment."""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from statistics import median


def compact(text):
    return "".join(c for c in text if not c.isspace())


def garbled_layer(text):
    """Apply the existing CCL noise test to both known offset font families."""
    from app.tools.pdf_tools import has_garbled_glyph_decoding

    second_family = "".join(chr(ord(c) + 0x400) if 0x5700 <= ord(c) <= 0x577F else c for c in text)
    return has_garbled_glyph_decoding(text) or has_garbled_glyph_decoding(second_family)


def align_chars(chars, raw_text, *, garbled=None):
    chars = [dict(c, offset=None) for c in chars]
    if garbled is None:
        garbled = garbled_layer(raw_text) or garbled_layer("".join(c["text"] for c in chars))
    source = [(i, c) for i, c in enumerate(raw_text) if not c.isspace()]
    target = [(i, c["text"]) for i, c in enumerate(chars) if not c["text"].isspace()]
    a, b = "".join(c for _, c in target), "".join(c for _, c in source)
    if any(len(c) != 1 for _, c in target):
        raise ValueError("Character records must contain single Unicode code points")
    if not garbled:

        def matches(left, right):
            return {
                i + k: j + k
                for i, j, n in SequenceMatcher(
                    None, left, right, autojunk=False
                ).get_matching_blocks()
                for k in range(n)
            }

        forward = matches(a, b)
        reverse = {len(a) - 1 - i: len(b) - 1 - j for i, j in matches(a[::-1], b[::-1]).items()}
        for i, j in forward.items():
            if reverse.get(i) == j:
                chars[target[i][0]]["offset"] = source[j][0]
    return chars, bool(garbled)


FAKE_BOLD_OFFSET_PT = 0.6


def deduplicate_glyphs(chars):
    """Mark overprinted glyphs against the first source glyph, without losing provenance."""
    output, groups = [], {}
    for source in chars:
        char = dict(source)
        char.pop("duplicate_of", None)
        key = (char["text"], round(char.get("font_size_pt", 0), 3))
        box = char["bbox_pt"]
        for first in groups.get(key, []):
            other = first["bbox_pt"]
            # Require strong overlap as well as tiny offsets: narrow genuine repeats survive.
            width = min(box[2] - box[0], other[2] - other[0])
            overlap = min(box[2], other[2]) - max(box[0], other[0])
            if (
                char["id"] != first["id"]
                and max(abs(a - b) for a, b in zip(box, other, strict=True)) < FAKE_BOLD_OFFSET_PT
                and overlap > 0.7 * width
                and abs(char.get("rotation", 0) - first.get("rotation", 0)) < 0.1
            ):
                char["duplicate_of"] = first["id"]
                break
        if "duplicate_of" not in char:
            groups.setdefault(key, []).append(char)
        output.append(char)
    return output


def text_lines(chars, *, segments=True, cell=False):
    """Group by vertical overlap, retaining superscripts and subscripts on their line."""
    lines = []
    for char in sorted(chars, key=lambda c: (c["bbox_pt"][1], c["bbox_pt"][0])):
        box = char["bbox_pt"]
        choices = []
        for i, line in enumerate(lines):
            lb = [median(c["bbox_pt"][k] for c in line) for k in range(4)]
            common = min(lb[3], box[3]) - max(lb[1], box[1])
            centre = (box[1] + box[3]) / 2
            if (
                cell
                and (
                    common > 0.30 * min(lb[3] - lb[1], box[3] - box[1]) or lb[1] <= centre <= lb[3]
                )
            ) or (
                common > 0.45 * min(lb[3] - lb[1], box[3] - box[1])
                and max(lb[3] - lb[1], box[3] - box[1]) <= 2 * min(lb[3] - lb[1], box[3] - box[1])
            ):
                choices.append((common, i))
        if choices:
            lines[max(choices)[1]].append(char)
        else:
            lines.append([char])
    output = []
    for line in sorted(lines, key=lambda line: min(c["bbox_pt"][1] for c in line)):
        line = sorted(line, key=lambda c: (c["bbox_pt"][0], c["bbox_pt"][1]))
        width = median(max(0.1, c["bbox_pt"][2] - c["bbox_pt"][0]) for c in line)
        parts = [[]]
        for char in line:
            if (
                segments
                and parts[-1]
                and char["bbox_pt"][0] - parts[-1][-1]["bbox_pt"][2] > 2 * width
            ):
                parts.append([])
            parts[-1].append(char)
        output.extend(parts)
    return output


def reading_order(chars):
    return [char for line in text_lines(chars) for char in line]


def layout_records(layout):
    """pdfminer has already applied /Rotate and translated the MediaBox origin."""
    from pdfminer.layout import LTChar, LTCurve, LTFigure, LTImage, LTLine, LTRect

    chars, rules = [], []
    ox, oy, _, _ = layout.bbox
    height = layout.height

    def box_of(item):
        x0, y0, x1, y1 = item.bbox
        return [x0 - ox, height - (y1 - oy), x1 - ox, height - (y0 - oy)]

    def walk(node):
        if isinstance(node, LTChar):
            text = node.get_text()
            box = box_of(node)
            angle = math.degrees(math.atan2(node.matrix[1], node.matrix[0]))
            for char in text:
                chars.append(
                    {
                        "id": len(chars),
                        "text": char,
                        "bbox_pt": box,
                        "font_size_pt": node.size,
                        "fontname": getattr(node, "fontname", ""),
                        "rotation": angle,
                        "multi_codepoint_glyph": len(text) != 1,
                    }
                )
        elif isinstance(node, (LTLine, LTRect)):
            rules.append({"bbox_pt": box_of(node), "kind": type(node).__name__})
        elif isinstance(node, (LTCurve, LTImage, LTFigure)):
            rules.append({"bbox_pt": box_of(node), "kind": type(node).__name__})
            if hasattr(node, "__iter__"):
                for child in node:
                    walk(child)
        elif hasattr(node, "__iter__"):
            for child in node:
                walk(child)

    walk(layout)
    return chars, rules


def extract_pages(pdf, pages):
    """Called only by the explicit pilot run command; never on import."""
    from pdfminer.high_level import extract_pages as pdfminer_pages
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    selected = sorted(set(pages))
    if not selected or selected[0] < 1 or selected[-1] > len(reader.pages):
        raise ValueError("Invalid 1-based page selection")
    layouts = pdfminer_pages(str(pdf), page_numbers=[p - 1 for p in selected])
    for number, layout in zip(selected, layouts, strict=True):
        page = reader.pages[number - 1]
        raw = page.extract_text() or ""  # No strip/CCL repair/fake-bold normalization.
        chars, rules = layout_records(layout)
        chars, garbled = align_chars(chars, raw, garbled=False)
        chars = deduplicate_glyphs(decode_fonts(chars))
        garbled = any(c.get("font_decode_unresolved") for c in chars)
        unit = float(page.get("/UserUnit", 1))
        for item in [*chars, *rules]:
            item["bbox_pt"] = [v * unit for v in item["bbox_pt"]]
            if "font_size_pt" in item:
                item["font_size_pt"] *= unit
        yield {
            "page": number,
            "width_pt": layout.width * unit,
            "height_pt": layout.height * unit,
            "raw_text": raw,
            "chars": chars,
            "rules": rules,
            "garbled": garbled,
            "coordinate_system": "rotated-mediabox-top-left-pdf-points",
            "cropbox_matches_mediabox": list(page.cropbox) == list(page.mediabox),
        }


def watermark_glyph(char):
    """Oblique glyphs are watermarks, mapped or not: the CCL 2025 stamp sits in the text
    layer as NUL-padded junk ("\\x00C\\x00C\\x00L"), so its letters can align to it.
    Axis-aligned rotations (vertical headers) stay subject to the rotation check."""
    angle = char.get("rotation", 0) % 90
    return min(angle, 90 - angle) > 1


def expansion_mappable(char, chars, raw):
    if not char.get("multi_codepoint_glyph"):
        return True
    group = [c for c in chars if c.get("multi_codepoint_glyph") and c["bbox_pt"] == char["bbox_pt"]]
    offsets = [c.get("offset") for c in group]
    return (
        all(type(o) is int for o in offsets)
        and offsets == list(range(offsets[0], offsets[0] + len(offsets)))
        and raw[offsets[0] : offsets[0] + len(offsets)] == "".join(c["text"] for c in group)
    )


# Explicit Latin TeX families; Chinese subset fonts are deliberately absent.
TEX_LATIN = re.compile(
    r"^(?:CM(?:R|SS|SSBX|SSDC|SSI|SSQ|SSQI|TT|TI|SL|B|BX|BXSL|BXTI|BSY|MI|MIB|SY|EX|U|DUNH|FIB|FF|VTT)|MSBM|MSAM)[0-9]+$",
    re.I,
)
OT1 = {11: "ff", 12: "fi", 13: "fl", 14: "ffi", 15: "ffl"}


def decode_fonts(chars):
    """Decode only page-wide uniform broken Latin font maps, after raw alignment."""
    output = [dict(c) for c in chars]
    groups = {}
    for c in output:
        groups.setdefault(c.get("fontname", ""), []).append(c)
    for font, glyphs in groups.items():
        bases = {ord(c["text"]) // 128 * 128 for c in glyphs if len(c["text"]) == 1}
        base = next(iter(bases)) if len(bases) == 1 else None
        allowed = bool(TEX_LATIN.fullmatch(font.split("+")[-1]))
        if allowed and base in (0x5700, 0x5B00):
            for c in glyphs:
                original = c["text"]
                code = ord(original) - base
                if 32 <= code <= 126 or code in OT1:
                    c["decoded_from"] = original
                    c["text"] = OT1.get(code, chr(code))
                else:
                    c["font_decode_unresolved"] = True
        elif allowed:
            for c in glyphs:
                if any(0x5700 <= ord(t) <= 0x577F or 0x5B00 <= ord(t) <= 0x5B7F for t in c["text"]):
                    c["font_decode_unresolved"] = True
    return output


def display_text(chars, raw):
    """Restore source whitespace without changing decoded glyphs or references."""
    inferred = any(c.get("offset") is None for c in chars)
    output = []
    for line in text_lines(chars, segments=False, cell=True):
        text = ""
        previous = None
        for char in line:
            if previous is not None:
                a, b = previous.get("offset"), char.get("offset")
                if a is not None and b is not None:
                    space = b > a and any(t.isspace() for t in raw[a + 1 : b])
                else:
                    size = char.get("font_size_pt", char["bbox_pt"][3] - char["bbox_pt"][1])
                    space = char["bbox_pt"][0] - previous["bbox_pt"][2] > 0.25 * size
                if previous["text"] in "([{?" or char["text"] in ")]},.;:!??":
                    space = False
                if space:
                    text += " "
            text += char["text"]
            previous = char
        output.append(text)
    return "\n".join(output), inferred
