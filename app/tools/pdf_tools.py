from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from app.schemas.documents import ParsedDocument
from app.tools.text_safety import sanitize_utf8_text


def parse_pdf_source(
    source: str,
    download_dir: str = "data/raw_papers",
    max_pages: int | None = None,
) -> ParsedDocument:
    path = _resolve_pdf_source(source=source, download_dir=download_dir)

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency boundary
        raise RuntimeError("pypdf is required for PDF parsing. Install requirements.txt.") from exc

    reader = PdfReader(str(path))
    page_limit = min(len(reader.pages), max_pages) if max_pages else len(reader.pages)

    page_texts = []
    page_numbers = []
    for page_index in range(page_limit):
        text = reader.pages[page_index].extract_text() or ""
        if text.strip():
            page_texts.append(collapse_fake_bold(sanitize_utf8_text(text.strip())))
            page_numbers.append(page_index + 1)

    metadata_title = reader.metadata.title if reader.metadata and reader.metadata.title else None
    title = (
        _usable_title(sanitize_utf8_text(metadata_title or ""))
        or _infer_title(page_texts)
        or path.stem
    )
    return ParsedDocument(
        source=str(path),
        title=title,
        text="\n\n".join(page_texts),
        pages=page_limit,
        page_texts=page_texts,
        page_numbers=page_numbers,
        metadata={
            "file_name": path.name,
            "original_source": source,
        },
    )


def _resolve_pdf_source(source: str, download_dir: str) -> Path:
    if _is_url(source):
        return _download_pdf(source=source, download_dir=download_dir)

    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"PDF source does not exist: {source}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file, got: {source}")
    return path


def _download_pdf(source: str, download_dir: str) -> Path:
    import httpx

    target_dir = Path(download_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(source)
    file_name = Path(parsed.path).name or "downloaded-paper.pdf"
    if not file_name.endswith(".pdf"):
        file_name = f"{_slugify(file_name)}.pdf"
    target_path = target_dir / file_name

    response = httpx.get(source, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    target_path.write_bytes(response.content)
    return target_path


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


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
