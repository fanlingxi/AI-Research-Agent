"""Conservative, offline PDF zoning. Raw extraction is never rewritten here.

Coordinates are optional evidence, not a replacement reading order. Blocks follow
the raw text order and carry separate spans wherever intervening text is excluded.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import median

from app.schemas.documents import DocumentBlock, TextSpan

CJK = r"\u3400-\u9fff\uf900-\ufaff，。！？；：、（）【】《》〈〉「」『』“”‘’"
CJK_GAPS = re.compile(rf"(?<=[{CJK}])[^\S\r\n]+(?=[{CJK}])")
_ABSTRACT = re.compile(r"^(?:摘\s*要|Abstract)(?:\s*[:：]\s*|\s*$)", re.I)
_KEYWORDS = re.compile(
    r"^(?:关\s*键\s*词|Keywords?|Key words)(?:\s*[:：]\s*|\s*$)", re.I,
)
_REFERENCES = re.compile(r"^(?:\d+[.、]?\s*)?(?:参考文献|References|Bibliography)\s*$", re.I)
_APPENDIX = re.compile(
    r"^(?i:Appendix|Appendices)\b|^附录|^[A-H](?:\.\d+)*\s+[A-Z]"
)
_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*[.、]?\s+\D|[一二三四五六七八九十]+、|"
    r"Introduction$|Conclusion[s]?$|Acknowledg(?:e)?ments?$|Limitations$)", re.I,
)
_AFFILIATION = re.compile(
    r"大学|学院|研究所|实验室|邮编|\b(?:University|Institute|Laboratory|Department|School)\b",
    re.I,
)
_CAPTION = re.compile(r"^(?:图|表)\s*\d+|^(?:Figure|Table)\s+\d+[.: ]", re.I)
_VENUE = re.compile(
    r"©|\(c\)|中国计算语言学大会|计算语言学专业委员会|^卷\s*\d|"
    r"^CCL\s*\d{4}|^Proceedings of|^Findings of|Association for Computational Linguistics",
    re.I,
)


@dataclass(frozen=True)
class TextRun:
    text: str
    x: float
    y: float
    size: float


@dataclass
class _Line:
    text: str
    span: TextSpan
    x: float | None = None
    y: float | None = None
    size: float | None = None
    height: float = 0
    kind: str = "paragraph"


def collect_text_runs(page) -> tuple[list[TextRun], float]:
    """A separate visitor pass leaves the legacy extract_text() call untouched."""
    runs = []

    def visit(text, cm, tm, font, size):
        if not text.strip():
            return
        # Compose text and current transformation matrices; reject rotations.
        if abs(cm[1]) > 0.01 or abs(cm[2]) > 0.01 or abs(tm[1]) > 0.01:
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        runs.append(TextRun(text, float(x), float(y), abs(float(size * cm[3]))))

    try:
        page.extract_text(visitor_text=visit)
        height = float(page.mediabox.height)
        if page.get("/Rotate", 0):
            return [], 0
        return runs, height
    except Exception:
        # Unsupported visitors, malformed matrices and missing geometry are normal
        # fallback cases. Raw extraction has already succeeded independently.
        return [], 0


def _run_key(text: str) -> str:
    from app.tools.pdf_tools import decode_offset_glyphs
    from app.tools.text_safety import sanitize_utf8_text

    text = sanitize_utf8_text(decode_offset_glyphs(text))
    text = re.sub(r"([^\x00-\x7f\s]) \1 \1", r"\1", text)
    return "".join(text.split())


def _lines(text: str, page: int, runs: list[TextRun], height: float) -> list[_Line]:
    stream = []
    owners = []
    for run in runs:
        key = _run_key(run.text)
        stream.append(key)
        owners.extend([run] * len(key))
    stream_text = "".join(stream)
    cursor = 0
    result = []
    for match in re.finditer(r"[^\r\n]+", text):
        value = match.group().strip()
        if not value:
            continue
        start = match.start() + len(match.group()) - len(match.group().lstrip())
        line = _Line(value, TextSpan(page=page, start=start, end=start + len(value)))
        key = "".join(value.split())
        pos = stream_text.find(key, cursor) if runs else -1
        if pos >= 0:
            selected = owners[pos:pos + len(key)]
            cursor = pos + len(key)
            ys = [r.y for r in selected]
            sizes = [r.size for r in selected]
            # Do not invent line coordinates for a visitor returning whole pages.
            if (selected and max(ys) - min(ys) <= max(sizes)
                    and all(len(r.text.strip().splitlines()) == 1 for r in selected)
                    and 0 < median(ys) < height and median(sizes) > 0):
                line.x = min(r.x for r in selected)
                line.y = median(ys)
                line.size = median(sizes)
                line.height = height
        result.append(line)
    return result


def _structured(text: str) -> bool:
    """Leave numeric tables, equations, code and column-like spacing untouched."""
    compact = "".join(text.split())
    if not compact:
        return True
    symbols = sum(c.isdigit() or c in "=+*/\\<>|{}_^%∑∫≈≤≥" for c in compact)
    return (
        symbols / len(compact) > 0.30
        or bool(re.search(r"[=∑∫\t]|\s{3,}", text))
        or bool(re.match(r"(?:def |class |import |return |for .+ in |SELECT |```)", text))
    )


def clean_body_line(text: str) -> str:
    return text if _structured(text) else CJK_GAPS.sub("", text)


def _margin(line: _Line, index: int, count: int) -> str | None:
    if line.y is not None:
        if line.y > line.height * 0.90:
            return "header"
        if line.y < line.height * 0.10:
            return "footer"
        return None
    if index < 2:
        return "header"
    if index >= count - 2:
        return "footer"
    return None


def _signature(text: str) -> str:
    return re.sub(r"\d+", "#", "".join(text.casefold().split()))


def _heading(text: str) -> bool:
    return (len(text) < 100 and not re.search(r"[。！？.!?]$", text)
            and bool(_HEADING.match(text) or _APPENDIX.match(text)))


def _reference_entry(text: str) -> bool:
    return bool(
        re.match(r"^(?:\[\d+\]\s*[A-Z\u3400-\u9fff]|[A-Z][A-Za-z’'-]+[ ,.]+[A-Z]"
                 r"|[\u3400-\u9fff]{2,}[，,、])", text)
        and re.search(r"\b(?:19|20)\d{2}[a-z]?[.。 ]", text)
        and len(text) > 25
    )


def derive_document_blocks(
    page_texts: list[str], page_numbers: list[int] | None = None,
    layouts: dict[int, tuple[list[TextRun], float]] | None = None,
) -> list[DocumentBlock]:
    numbers = page_numbers or list(range(1, len(page_texts) + 1))
    raw = dict(zip(numbers, page_texts, strict=True))
    pages = [_lines(t, n, *(layouts or {}).get(n, ([], 0)))
             for n, t in zip(numbers, page_texts, strict=True)]
    repeated = defaultdict(set)
    for lines in pages:
        for i, line in enumerate(lines):
            edge = _margin(line, i, len(lines))
            if edge and len(line.text) < 200:
                repeated[(edge, _signature(line.text))].add(line.span.page)
    for lines in pages:
        for i, line in enumerate(lines):
            edge = _margin(line, i, len(lines))
            if edge and (
                len(repeated[(edge, _signature(line.text))]) >= 2
                or re.fullmatch(r"[-–—]?\s*\d{1,5}\s*[-–—]?", line.text)
                or (line.y is not None and _VENUE.search(line.text))
            ):
                line.kind = edge

    flat = [line for lines in pages for line in lines]
    # A front-matter region requires an explicit abstract plus title/affiliation
    # evidence. Bilingual first pages may contain two such regions.
    first = [line for line in flat if line.span.page == 1]
    region_start = 0
    for i, line in enumerate(first):
        if _KEYWORDS.match(line.text):
            region_start = i + 1
        if re.match(r"^(?:1\s+)?(?:引言|Introduction)\s*$", line.text, re.I):
            region_start = i + 1
        if not _ABSTRACT.match(line.text):
            continue
        prefix = [item for item in first[region_start:i] if item.kind == "paragraph"]
        sizes = [item.size for item in prefix if item.size]
        title_evidence = (sizes and max(sizes) >= 1.15 * (line.size or median(sizes)))
        affiliation = any(_AFFILIATION.search(item.text) or "@" in item.text for item in prefix)
        prose = any(re.search(r"[。！？.!?]$", item.text) for item in prefix)
        if len(prefix) <= 24 and (title_evidence or (affiliation and not prose)):
            for item in prefix:
                item.kind = "front_matter"
        region_start = i + 1

    in_abstract = False
    in_references = False
    for i, line in enumerate(flat):
        if line.kind in {"header", "footer", "front_matter"}:
            if line.kind == "front_matter":
                in_abstract = False
            continue
        text = line.text
        if _REFERENCES.fullmatch(text):
            following = [item.text for item in flat[i + 1:i + 9]
                         if item.kind not in {"header", "footer"}]
            # Heading alone (e.g. an ordinary mention) never suppresses the tail.
            in_references = _reference_entry(" ".join(following[:4]))
            in_abstract = False
        if (in_references and _APPENDIX.match(text) and _heading(text)
                and not re.search(r"[,，]|\b(?:19|20)\d{2}\b", text)):
            in_references = False
        if in_references:
            line.kind = "references"
        elif _ABSTRACT.match(text):
            line.kind = "abstract"
            in_abstract = True
        elif _KEYWORDS.match(text):
            line.kind = "keywords"
            in_abstract = False
        elif _heading(text):
            line.kind = "heading"
            in_abstract = False
        elif _CAPTION.match(text):
            line.kind = "caption"
            in_abstract = False
        elif _structured(text):
            line.kind = "other"
        elif in_abstract:
            line.kind = "abstract"

    blocks: list[DocumentBlock] = []
    previous: _Line | None = None
    for line in flat:
        if line.kind == "other":
            page_text = raw[line.span.page]
            start = page_text.rfind("\n", 0, line.span.start) + 1
            end = page_text.find("\n", line.span.end)
            end = len(page_text) if end < 0 else end
            line.span = TextSpan(page=line.span.page, start=start, end=end)
            line.text = page_text[start:end]
        value = clean_body_line(line.text) if line.kind in {"paragraph", "abstract"} else line.text
        if previous and blocks and _can_join(previous, line, raw):
            block = blocks[-1]
            block.text = _join(block.text, value)
            last = block.spans[-1]
            if last.page == line.span.page:
                last.end = line.span.end
            else:
                block.spans.append(line.span.model_copy())
        else:
            blocks.append(DocumentBlock(kind=line.kind, text=value, spans=[line.span.model_copy()]))
        previous = line
    return blocks


def _join(left: str, right: str) -> str:
    # Keep the line-break hyphen and whitespace: anchors accepts either spelling.
    space = "" if re.search(rf"[{CJK}]$", left) and re.match(rf"[{CJK}]", right) else " "
    return left + space + right


def _can_join(a: _Line, b: _Line, raw: dict[int, str]) -> bool:
    if a.kind != b.kind or a.kind not in {"paragraph", "abstract"}:
        return False
    if _ABSTRACT.match(a.text) or _ABSTRACT.match(b.text):
        return False
    if a.x is None or b.x is None or a.size is None or b.size is None:
        return False
    if abs(a.size - b.size) > max(1, a.size * 0.12):
        return False
    # Same column, allowing a first-line indent to return to the left edge.
    if not -2.2 * a.size <= b.x - a.x <= 0.5 * a.size:
        return False
    if a.span.page == b.span.page:
        gap = raw[a.span.page][a.span.end:b.span.start]
        if gap.strip() or gap.count("\n") > 1:
            return False
        return 0.7 * a.size <= a.y - b.y <= 1.65 * a.size
    # Only directly adjacent, clearly unfinished body text may cross pages.
    # Footers/headers between them remain separate blocks, never silently spliced.
    return (
        b.span.page == a.span.page + 1
        and a.y < a.height * 0.18 and b.y > b.height * 0.78
        and not re.search(r"[.!?。！？:：;；][\"'”’)]?$", a.text)
        and bool(re.match(r"[a-z\u3400-\u9fff]", b.text))
        and not raw[a.span.page][a.span.end:].strip()
        and not raw[b.span.page][:b.span.start].strip()
    )


def block_counts(blocks: list[DocumentBlock]) -> dict[str, int]:
    return dict(Counter(block.kind for block in blocks))
