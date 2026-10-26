"""D040-E rules. Geometry supplies evidence; uncertain text stays in the body.

Physical blocks stay in extraction order. A continuation after a caption or a
page margin has paragraph_start=False without joining text across a deletion.
The v3 implementation and its coordinate/anchor normalization are frozen.
"""

from __future__ import annotations

import re
from collections import defaultdict
from statistics import median

from app.schemas.documents import DocumentBlock
from app.tools.pdf_layout import (
    _ABSTRACT,
    _APPENDIX,
    _CAPTION,
    _KEYWORDS,
    _VENUE,
    _heading,
    _join,
    _lines,
    _signature,
    clean_body_line,
    derive_document_blocks,
)

_REFERENCES = re.compile(
    r"^(?:(?:\d+|[IVX]+)[.、]?\s*)?"
    r"(?:参考文献|引用文献|参考书目|References?(?:\s+and\s+Notes)?|Bibliography|Works Cited)"
    r"\s*[:：]?\s*$", re.I,
)
_PAGE = re.compile(
    r"^(?:[-–—]\s*)?(?:\d{1,5}|[ivxlcdm]{1,8}|(?:page\s*)?\d+\s*(?:of|/)\s*\d+"
    r"|第\s*\d+\s*页(?:\s*[共/，,]\s*\d+\s*页)?)\s*(?:[-–—])?$", re.I,
)
_NOTE = re.compile(
    r"通讯作者|通信作者|基金项目|资助|许可|版权|©|copyright|creative commons|"
    r"corresponding author|equal contribution|all rights reserved|permission to|"
    r"^[*∗†‡]\s*\S|^[¹²³⁴⁵⁶⁷⁸⁹⁰]+\s*\S|"
    r"^\d{1,2}\s*(?:https?://|www\.)|^https?://|^www\.", re.I,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
_NUMBERED_ENTRY = re.compile(r"^(?:\[\d{1,3}\]|\d{1,3}[.)、])\s*\S")
_AUTHOR = re.compile(r"^[A-ZÀ-ÖØ-Þ][\w’'’-]+[ ,.]+[A-ZÀ-ÖØ-Þ]|^[\u3400-\u9fff]{2,}[，,、]")
_CORPORATE_AUTHOR = re.compile(r"^[A-Z][\w .&'-]{2,60}\.\s*(?:19|20)\d{2}\.")
_BULLET = re.compile(r"^[•●▪]\s*\S|^RQ\d+\s+[A-Z]")


def _section_heading(text):
    if not _heading(text):
        return False
    # A line-wrap starting '4 for ...', '(2) ...', or 'Appendix section ...'
    # is prose, even though v3's permissive numbered-heading regex accepts it.
    if re.match(r"^\d", text):
        return bool(re.match(r"^\d+(?:\.\d+)*[.、]?\s+[A-Z\u3400-\u9fff]", text)
                    and not re.search(r"[,，;；]", text))
    if re.match(r"(?i)^appendix\s+", text):
        return bool(re.match(r"^Appendix\s+(?:[A-Z]|\d)", text))
    return True


def _reference_entry(text):
    # A heading and a recognizable bibliographic entry are both required.
    # A bare [1] URL or an ordinary sentence containing a year is insufficient.
    return bool(
        len(text) > 25 and _YEAR.search(text)
        and (_NUMBERED_ENTRY.match(text) or _AUTHOR.match(text) or _CORPORATE_AUTHOR.match(text))
    )


def _edge(line):
    if line.y is None:
        return None
    if line.y > line.height * 0.90:
        return "header"
    if line.y < line.height * 0.10:
        return "footer"
    return None


def _margins(pages):
    signatures = defaultdict(set)
    venue_pages = defaultdict(set)
    for lines in pages:
        for i, line in enumerate(lines):
            edge = _edge(line)
            if edge and len(line.text) < 200:
                signatures[edge, _signature(line.text)].add(line.span.page)
            if line.y is None and i >= len(lines) - 6 and _VENUE.search(line.text):
                venue_pages[_signature(line.text)].add(line.span.page)
    for lines in pages:
        sizes = [line.size for line in lines if line.size and len(line.text) > 25]
        body_size = median(sizes) if sizes else 10
        for line in lines:
            edge = _edge(line)
            if edge and (
                len(signatures[edge, _signature(line.text)]) >= 2
                or _PAGE.fullmatch(line.text)
                or _VENUE.search(line.text)
            ):
                line.kind = edge
            if (line.y is None and line in lines[-6:]
                    and len(venue_pages[_signature(line.text)]) >= 2):
                line.kind = "footer"
        # Footnotes can start above the narrow running-footer band. Demand a
        # semantic marker there, then extend only through adjacent small text
        # in the same column. Extraction order need not be geometric order.
        bottom = sorted(
            [line for line in lines if line.y is not None and line.y < line.height * 0.26],
            key=lambda line: -line.y,
        )
        notes = []
        for line in bottom:
            small = line.size <= body_size * 0.96
            explicit = bool(_NOTE.search(line.text) or _VENUE.search(line.text))
            if not small and not re.match(
                r"^(?:[*∗†‡©¹²³⁴⁵⁶⁷⁸⁹⁰]|通讯作者|通信作者|基金项目|版权|"
                r"copyright|creative commons|proceedings of|findings of)", line.text, re.I,
            ):
                explicit = False
            # A standalone URL in body-sized prose is not itself a footnote.
            if re.match(r"^(?:https?://|www\.)", line.text, re.I) and not small:
                explicit = False
            numbered = bool(re.match(r"^\d{1,2}\s*[A-Za-z\u3400-\u9fff]", line.text))
            continuation = any(
                0 < note.y - line.y <= 1.8 * max(note.size, line.size)
                and abs(note.size - line.size) < 0.6
                and -2.5 * line.size <= line.x - note.x <= 2.5 * line.size
                for note in notes
            )
            if explicit or (small and numbered) or (small and continuation):
                line.kind = "footer"
                notes.append(line)


def _structured_evidence(text):
    """v3's broad spacing/equals test protects cleanup, not body exclusion."""
    compact = "".join(text.split())
    words = re.findall(r"[A-Za-z]{2,}|[\u3400-\u9fff]", text)
    numbers = re.findall(r"(?<!\w)[+-]?\d+(?:\.\d+)?%?", text)
    # Require actual cells, or a standalone symbolic expression. Prose with
    # inline math, citations, percentages or wide justification stays body.
    cells = re.split(r"\t+|\s{2,}", text.strip())
    if len(cells) >= 3 and len(numbers) >= 2 and len(words) < 8:
        return True
    symbols = sum(c in "=+*/\\<>|{}_^∑∫≈≤≥" for c in compact)
    return (symbols >= 2 and len(words) <= 2 and len(compact) < 100
            and not re.search(r"[\u3400-\u9fff]", text))


def _caption_prose(text):
    return bool(re.match(
        r"^(?:Figure|Table)\s+\d+(?:\s+(?:shows?|illustrates?|presents?|reports?)\b"
        r"|\.\s+As (?:illustrated|shown|discussed)\b)|"
        r"^(?:图|表)\s*\d+(?:\s*\([a-z]\))?\s*(?:统计|展示|显示|给出|说明|表明|中|分别|和图)",
        text, re.I,
    ))


def _classify(flat):
    abstract = references = False
    for i, line in enumerate(flat):
        if line.kind in {"header", "footer", "front_matter"}:
            if line.kind == "front_matter":
                abstract = False
            continue
        text = clean_body_line(line.text)
        if _REFERENCES.fullmatch(text):
            following = []
            for item in flat[i + 1:]:
                if item.kind in {"header", "footer"}:
                    continue
                if _section_heading(item.text) and not _NUMBERED_ENTRY.match(item.text):
                    break
                following.append(item.text)
                if len(following) == 8:
                    break
            references = any(_reference_entry(" ".join(following[j:j + 4]))
                             for j in range(min(4, len(following))))
            abstract = False
        if (references and _APPENDIX.match(text) and _section_heading(text)
                and not re.search(r"[,，]|\b(?:19|20)\d{2}\b", text)):
            references = False
        if references:
            line.kind = "references"
        elif _ABSTRACT.match(text):
            line.kind, abstract = "abstract", True
        elif _KEYWORDS.match(text):
            line.kind, abstract = "keywords", False
        elif _section_heading(text):
            line.kind, abstract = "heading", False
        elif _CAPTION.match(text):
            # Figure/Table mentions inside prose are not captions.
            if len(text) < 180 and not _caption_prose(text):
                line.kind = "caption"
        elif _structured_evidence(line.text):
            line.kind = "other"
        elif abstract:
            line.kind = "abstract"


def _column_edges(lines):
    candidates = [line for line in lines if line.kind in {"paragraph", "abstract"}
                  and line.x is not None and len(line.text) >= 20]
    if not candidates:
        candidates = [line for line in lines if line.kind in {"paragraph", "abstract"}
                      and line.x is not None]
    if not candidates:
        return []
    size = median(line.size for line in candidates)
    clusters = []
    for line in sorted(candidates, key=lambda line: line.x):
        if not clusters or line.x - clusters[-1][0].x > 6 * size:
            clusters.append([])
        clusters[-1].append(line)
    # A small left indent must not be mistaken for a second column. Large
    # clusters are column candidates; isolated centered fragments aren't.
    if len(clusters) > 2:
        clusters = sorted(sorted(clusters, key=len, reverse=True)[:2], key=lambda c: c[0].x)
    return [min(line.x for line in cluster) for cluster in clusters]


def _paragraph_start(line, previous_body, preceding, edges, raw):
    if line.kind not in {"paragraph", "abstract"}:
        return line.kind in {"heading", "keywords"}
    if _ABSTRACT.match(clean_body_line(line.text)):
        return True
    if _BULLET.match(line.text):
        return True
    if preceding and (preceding.kind == "heading"
                      or _ABSTRACT.match(clean_body_line(preceding.text))):
        return True
    if line.x is None or not edges:
        # Without geometry retain explicit blank-line boundaries. Otherwise
        # keep blocks separate, but do not invent boundaries on every wrap.
        return previous_body is None or (
            previous_body.span.page == line.span.page
            and raw[line.span.page][previous_body.span.end:line.span.start].count("\n") > 1
        )
    edge = min(edges, key=lambda edge: abs(edge - line.x))
    indent = line.x - edge
    if 0.65 * line.size <= indent <= 3 * line.size:
        if preceding and _BULLET.match(preceding.text):
            return False
        if (preceding and preceding.x is not None and abs(preceding.x - line.x) < 0.3 * line.size
                and preceding.kind == line.kind and preceding.span.page == line.span.page
                and not re.search(r"[.!?。！？]$", preceding.text)):
            return False
        return True
    if previous_body is None:
        return line.span.page == 1
    if (preceding is previous_body and preceding.span.page == line.span.page
            and preceding.x is not None and abs(preceding.x - line.x) < 3 * line.size
            and preceding.y - line.y > 1.35 * line.size
            and re.search(r"[.!?。！？][\"'”’)]?\d*$", preceding.text)
            and re.match(r"[A-Z\u3400-\u9fff]", line.text)):
        return True
    # Flush column/page starts and returns after floats continue the paragraph;
    # a large gap is not positive evidence for a new paragraph by itself.
    return False


def derive_blocks(page_texts, page_numbers=None, layouts=None):
    numbers = page_numbers or list(range(1, len(page_texts) + 1))
    raw = dict(zip(numbers, page_texts, strict=True))
    pages = [_lines(text, number, *(layouts or {}).get(number, ([], 0)))
             for number, text in zip(numbers, page_texts, strict=True)]
    _margins(pages)
    flat = [line for lines in pages for line in lines]
    # Reuse only v3's established bilingual front-matter decision. Other v3
    # exclusions, particularly 'other', must not leak into v4.
    front = {(span.page, span.start, span.end)
             for block in derive_document_blocks(page_texts, numbers, layouts)
             if block.kind == "front_matter" for span in block.spans}
    for line in flat:
        if line.kind == "paragraph" and (line.span.page, line.span.start, line.span.end) in front:
            line.kind = "front_matter"
    _classify(flat)
    columns = {(number, kind): _column_edges([line for line in lines if line.kind == kind])
               for number, lines in zip(numbers, pages, strict=True)
               for kind in ("paragraph", "abstract")}
    blocks = []
    previous_body = preceding = previous_line = None
    for line in flat:
        start = _paragraph_start(line, previous_body, preceding,
                                 columns.get((line.span.page, line.kind), []), raw)
        value = clean_body_line(line.text) if line.kind in {"paragraph", "abstract"} else line.text
        # Join adjacent lines only. A paragraph can have multiple physical
        # blocks, with explicit continuation metadata across excluded material.
        join = (blocks and previous_line and line.kind == previous_line.kind
                and line.kind in {"paragraph", "abstract"} and not start
                and line.span.page == previous_line.span.page
                and not raw[line.span.page][previous_line.span.end:line.span.start].strip())
        if join:
            blocks[-1].text = _join(blocks[-1].text, value)
            blocks[-1].spans[-1].end = line.span.end
        else:
            blocks.append(DocumentBlock(kind=line.kind, text=value,
                                        spans=[line.span.model_copy()], paragraph_start=start))
        if line.kind in {"paragraph", "abstract"}:
            previous_body = line
        if line.kind not in {"header", "footer"}:
            preceding = line
        previous_line = line
    return blocks
