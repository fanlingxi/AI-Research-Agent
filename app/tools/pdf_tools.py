from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.config.settings import Settings
from app.schemas.documents import ParsedDocument
from app.tools.text_safety import sanitize_utf8_text


def parse_pdf_source(
    source: str,
    download_dir: str = "data/raw_papers",
    max_pages: int | None = None,
    *,
    derive_blocks: bool = False,
    parser_version: str = "pypdf-v3",
    download_settings: Settings | None = None,
) -> ParsedDocument:
    if _is_url(source):
        # Run the existing parser on the staging file before publishing it.
        validated = []

        def validate(path: Path) -> None:
            parsed = parse_pdf_source(
                str(path), max_pages=max_pages, derive_blocks=derive_blocks,
                parser_version=parser_version,
            )
            if parsed.title == path.stem:
                parsed.title = None
            validated.append(parsed)

        path = _download_pdf(source, download_dir, settings=download_settings, validate=validate)
        parsed = validated[0]
        return parsed.model_copy(update={
            "source": str(path),
            "title": parsed.title or path.stem.partition("-")[2] or path.stem,
            "metadata": {**parsed.metadata, "file_name": path.name, "original_source": source},
        })

    path = _resolve_pdf_source(source=source, download_dir=download_dir)

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency boundary
        raise RuntimeError("pypdf is required for PDF parsing. Install requirements.txt.") from exc

    reader = PdfReader(str(path))
    page_limit = min(len(reader.pages), max_pages) if max_pages else len(reader.pages)

    page_texts = []
    page_numbers = []
    fallback_pages = []
    tex_pages = []
    tex_fonts = set()
    layouts = {}
    for page_index in range(page_limit):
        decoder = None
        if parser_version == "pypdf-v4":
            text, decoder = extract_tex_decoded_text(reader.pages[page_index])
            if decoder:
                tex_pages.append(page_index + 1)
                tex_fonts.update(decoder.labels)
        else:
            text = reader.pages[page_index].extract_text() or ""
        if has_garbled_glyph_decoding(text):
            text = decode_offset_glyphs(text)
            fallback_pages.append(page_index + 1)
        if text.strip():
            page_texts.append(collapse_fake_bold(sanitize_utf8_text(text.strip())))
            page_numbers.append(page_index + 1)
            if derive_blocks:
                from app.tools.pdf_layout import collect_text_runs

                layouts[page_index + 1] = collect_text_runs(reader.pages[page_index], decoder)

    metadata_title = reader.metadata.title if reader.metadata and reader.metadata.title else None
    title = (
        _usable_title(sanitize_utf8_text(metadata_title or ""))
        or _infer_title(page_texts)
        or path.stem
    )
    blocks = None
    if derive_blocks:
        from app.tools.pdf_layout import derive_document_blocks

        blocks = derive_document_blocks(page_texts, page_numbers, layouts, version=parser_version)
    return ParsedDocument(
        source=str(path),
        title=title,
        text="\n\n".join(page_texts),
        pages=page_limit,
        page_texts=page_texts,
        page_numbers=page_numbers,
        blocks=blocks,
        metadata={
            "file_name": path.name,
            "original_source": source,
            **(
                {
                    "glyph_offset_decoded_pages": fallback_pages,
                    "glyph_offset_decoding": "U+5B00 offset, OT1 ligatures",
                }
                if fallback_pages
                else {}
            ),
            **(
                {
                    "tex_font_decoded_pages": tex_pages,
                    "tex_font_decoded_fonts": sorted(tex_fonts),
                    "tex_font_decoding": "Per-font U+5700/U+5B00 offset, TeX OT1 encoding",
                }
                if tex_pages
                else {}
            ),
        },
    )


# Broken embedded Latin fonts in CCL PDFs map ASCII/ligatures into U+5B00-5B7F.
# Allow adjacent glyphs in the surrounding blocks, but require rare core glyphs:
# ordinary Chinese, names and fake-bold repetitions must not trigger a fallback.
_GLYPH_RUN = re.compile(r"[\u5a00-\u5bff\s]+")
_COMMON_CORE_GLYPHS = frozenset("子孔孕字存孙孝孟季孤孩孫")


def has_garbled_glyph_decoding(text: str) -> bool:
    """Detect clustered CCL font-decoding noise, not general Chinese mojibake.

    A local run needs eight glyphs, four distinct glyphs and at least half rare
    U+5B00-5B7F glyphs. Local evidence catches even small corrupt citations on
    otherwise normal pages without relying on a page-wide Chinese percentage.
    This deliberately conservative heuristic is not a language/word classifier.
    """
    for match in _GLYPH_RUN.finditer(text):
        glyphs = "".join(match.group().split())
        if len(glyphs) < 8 or len(set(glyphs)) < 4:
            continue
        rare_count = sum(
            "\u5b00" <= char <= "\u5b7f" and char not in _COMMON_CORE_GLYPHS
            for char in glyphs
        )
        if rare_count >= max(4, len(glyphs) / 2):
            return True
    return False


# Their ToUnicode map shifts every code by U+5B00 ("孩孴" = "it"); codes 0x0B-0x0F are the
# TeX OT1 ligatures. Any extractor that trusts the map returns the same noise.
_OT1_LIGATURES = {0x0B: "ff", 0x0C: "fi", 0x0D: "fl", 0x0E: "ffi", 0x0F: "ffl"}


# pdfTeX + the CJK package can attach a CJK subfont's ToUnicode map (TeX-Uni57-0:
# <00>-<FF> -> U+5700) to CMR10, so "表 圲场" is really "表 2:". Decode such fonts
# with the OT1 maps pdfTeX writes for the healthy CM fonts of the same PDFs
# (TeX-cmr12-builtin, TeX-cmtt12-builtin): ASCII decoding would turn quotes into
# \ and ", en dashes into { and drop the accents of author names.
_OT1_SHARED = {
    **{code: chr(code) for code in range(0x21, 0x7F)},
    **dict(enumerate("\u0393\u2206\u0398\u039b\u039e\u03a0\u03a3\u03a5\u03a6\u03a8\u2126")),
    **dict(enumerate("\u0131\u0237`\u00b4\u02c7\u02d8\u00af\u02da\u00b8ßæœøÆŒØ", 0x10)),
    0x27: "\u2019", 0x60: "\u2018", 0x7F: "\u00a8",
}
_OT1_TEXT = {
    **_OT1_SHARED, **_OT1_LIGATURES,
    0x20: "", 0x22: "\u201d", 0x3C: "\u00a1", 0x3E: "\u00bf", 0x5C: "\u201c",
    0x5E: "\u02c6", 0x5F: "\u02d9", 0x7B: "\u2013", 0x7C: "\u2014",
    0x7D: "\u02dd", 0x7E: "\u02dc",
}
_OT1_ITALIC = {**_OT1_TEXT, 0x24: "\u00a3"}
_OT1_TYPEWRITER = {
    **_OT1_SHARED, 0x0B: "\u2191", 0x0C: "\u2193", 0x0D: "'",
    0x0E: "\u00a1", 0x0F: "\u00bf", 0x20: "\u2423",
}
_TEX_TEXT = re.compile(
    r"(CMR|CMBX|CMB|CMSL|CMBXSL|CMTI|CMBXTI|CMU|CMSS|CMSSBX|CMSSDC|CMSSI|"
    r"CMSSQ|CMSSQI|CMCSC|CMDUNH|CMFIB)\d+", re.I,
)
_TEX_TYPEWRITER = re.compile(r"(?:CMTT|CMSLTT|CMITT|CMTCSC)\d+", re.I)


def _font_name(font) -> str:
    return str(font.get("/BaseFont", "")).removeprefix("/") if font else ""


@dataclass
class TexFontDecoder:
    """One page's verdict: BaseFont -> (shifted block base, OT1 table); others pass."""

    fonts: dict[str, tuple[int, dict[int, str]]]

    @property
    def labels(self) -> list[str]:
        return [f"{name} U+{base:04X}" for name, (base, _) in self.fonts.items()]

    def __call__(self, text: str, font) -> str:
        rule = self.fonts.get(_font_name(font))
        if rule is None:
            return text
        base, table = rule
        return "".join(char if char.isspace() else table.get(ord(char) - base, char)
                       for char in text)


def extract_tex_decoded_text(page) -> tuple[str, TexFontDecoder | None]:
    """pypdf-v4: the same extract_text() output, plus the font that drew each chunk."""
    chunks = []
    text = page.extract_text(
        visitor_text=lambda chunk, cm, tm, font, size: chunks.append((chunk, font)),
    ) or ""
    return decode_tex_font_page(text, chunks)


def decode_tex_font_page(text: str, chunks) -> tuple[str, TexFontDecoder | None]:
    """Trust font evidence only when visitor chunks account for the entire output."""
    spans = []
    blocks = {}
    pos = 0
    for chunk, font in chunks:
        # pypdf may repeat a whole XObject after its individual visitor chunks.
        if not chunk or not text.startswith(chunk, pos):
            continue
        end = pos + len(chunk)
        spans.append((pos, end, font))
        blocks.setdefault(_font_name(font), set()).update(
            ord(char) & ~0x7F for char in text[pos:end] if not char.isspace()
        )
        pos = end
    if pos != len(text):
        return text, None
    fonts = {}
    for name, bases in blocks.items():
        if len(bases) != 1 or not bases <= {0x5700, 0x5B00}:
            continue
        family = re.sub(r"^[A-Z]{6}\+", "", name, flags=re.I)
        match = _TEX_TEXT.fullmatch(family)
        if match:
            table = _OT1_ITALIC if match[1].upper() in {"CMTI", "CMBXTI", "CMU"} else _OT1_TEXT
        elif _TEX_TYPEWRITER.fullmatch(family):
            table = _OT1_TYPEWRITER
        else:
            continue
        fonts[name] = (next(iter(bases)), table)
    if not fonts:
        return text, None
    decoder = TexFontDecoder(fonts)
    return "".join(decoder(text[start:end], font) for start, end, font in spans), decoder


def decode_offset_glyphs(text: str) -> str:
    """Decode only the glyph runs the detector flags; ordinary Chinese is left alone."""

    def decode(match: re.Match[str]) -> str:
        run = match.group()
        if not has_garbled_glyph_decoding(run):
            return run
        out = []
        for char in run:
            code = ord(char) - 0x5B00
            if 0x20 <= code <= 0x7E:
                out.append(chr(code))
            elif code in _OT1_LIGATURES:
                out.append(_OT1_LIGATURES[code])
            else:
                out.append(char)
        return "".join(out)

    return _GLYPH_RUN.sub(decode, text)


def _resolve_pdf_source(source: str, download_dir: str) -> Path:
    if _is_url(source):
        return _download_pdf(source=source, download_dir=download_dir)

    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"PDF source does not exist: {source}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file, got: {source}")
    return path


def _download_pdf(
    source: str, download_dir: str, *, settings: Settings | None = None,
    validate: Callable[[Path], None] | None = None,
) -> Path:
    from app.tools.pdf_download import download_pdf

    return download_pdf(
        source, download_dir, settings=settings,
        validate=validate or (lambda path: parse_pdf_source(str(path))),
    )


def _is_url(source: str) -> bool:
    return bool(urlparse(source).netloc) or "://" in source


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug or "downloaded-paper"


_PLACEHOLDER_TITLES = {"untitled", "title", "untitled document", "no title", "document", "paper"}
# Fake-bold PDFs draw each glyph several times; extractors emit "基 基 基于 于 于".
_FAKE_BOLD = re.compile(r"([^\x00-\x7f\s]) \1 \1")


# Running headers such as "Proceedings of ... pages 338–354" or "©2024 Association for ...".
_VENUE_LINE = re.compile(
    r"\bpages \d+\s*[–-]\s*\d+|©|\(v ?olume \d|association for computational linguistics"
    r"|\b(?:january|february|march|april|may|june|july|august|september|october|november"
    r"|december) \d{1,2}\s*[–-]\s*\d{1,2},? \d{4}|arxiv:\d",
    re.IGNORECASE,
)


def collapse_fake_bold(text: str) -> str:
    """Undo repeated-glyph stutter only on lines that clearly show the pattern."""

    lines = []
    for line in text.split("\n"):
        if len(_FAKE_BOLD.findall(line)) >= 2:
            line = _FAKE_BOLD.sub(r"\1", line)
        lines.append(line)
    return "\n".join(lines)


def _usable_title(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value).strip()
    lowered = normalized.lower()
    if (
        not normalized
        or lowered.endswith((".pdf", ".doc", ".docx", ".tex"))
        or lowered in _PLACEHOLDER_TITLES
        or lowered.startswith("microsoft word - ")
    ):
        return None
    return normalized


def _infer_title(page_texts: list[str]) -> str | None:
    """Use the first plausible heading when a PDF has no metadata title."""

    if not page_texts:
        return None

    blocked_prefixes = (
        "arxiv:",
        "preprint",
        "submitted",
        "copyright",
        "provided proper attribution",
        "reproduce the tables",
        "scholarly works",
        "published as",
        "proceedings of",
        "conference on",
    )
    lines = page_texts[0].splitlines()[:24]
    for index, line in enumerate(lines):
        candidate = re.sub(r"\s+", " ", line).strip()
        lowered = candidate.lower()
        cjk = sum("一" <= char <= "鿿" for char in candidate)
        if (
            (12 if cjk < 4 else 6) <= len(candidate) <= 220
            and any(char.isalpha() for char in candidate)
            and not lowered.startswith(blocked_prefixes)
            and not _VENUE_LINE.search(candidate)
        ):
            continuation = _title_continuation(lines[index + 1 : index + 3])
            return f"{candidate} {continuation}".strip()
    return None


def _title_continuation(lines: list[str]) -> str:
    """Join wrapped title lines while stopping before author metadata."""

    parts: list[str] = []
    for line in lines:
        candidate = re.sub(r"\s+", " ", line).strip()
        if not candidate or len(candidate) > 140:
            break
        if any(marker in candidate for marker in ("@", "∗", "*", "†", "‡", "，", "、")):
            break
        words = candidate.split()
        if candidate.count(",") >= 2 or (
            len(words) >= 6 and all(w[:1].isupper() and w.isalpha() for w in words)
        ):
            break  # English author list
        # A short all-CJK line under a Chinese title is an author name, not a wrap.
        if len(candidate) <= 4 and all("一" <= char <= "鿿" for char in candidate):
            break
        if re.search(r"\d", candidate):
            break
        if not any(char.isalpha() for char in candidate):
            break
        parts.append(candidate)
        if len(parts) == 1:
            break
    return " ".join(parts)
