from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from app.schemas.documents import ParsedDocument
from app.schemas.research import ToolResult


def parse_pdf(
    source: str,
    download_dir: str = "data/raw_papers",
    max_pages: int | None = None,
) -> ToolResult:
    """Parse a local or remote PDF into extracted text."""

    try:
        parsed = parse_pdf_source(source=source, download_dir=download_dir, max_pages=max_pages)
        return ToolResult(
            tool_name="parse_pdf",
            status="success",
            content=f"Extracted {len(parsed.text)} characters from {parsed.pages} pages.",
            metadata={"document": parsed.model_dump()},
        )
    except Exception as exc:  # pragma: no cover - defensive tool boundary
        return ToolResult(
            tool_name="parse_pdf",
            status="error",
            content=str(exc),
            metadata={"source": source},
        )


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
    for page_index in range(page_limit):
        text = reader.pages[page_index].extract_text() or ""
        if text.strip():
            page_texts.append(text.strip())

    metadata_title = reader.metadata.title if reader.metadata and reader.metadata.title else None
    title = _usable_title(metadata_title) or _infer_title(page_texts) or path.stem
    return ParsedDocument(
        source=str(path),
        title=title,
        text="\n\n".join(page_texts),
        pages=page_limit,
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

    response = httpx.get(source, timeout=30.0)
    response.raise_for_status()
    target_path.write_bytes(response.content)
    return target_path


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug or "downloaded-paper"


def _usable_title(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized or normalized.lower().endswith(".pdf"):
        return None
    return normalized


def _infer_title(page_texts: list[str]) -> str | None:
    """Use the first plausible heading when a PDF has no metadata title."""

    if not page_texts:
        return None

    blocked_prefixes = ("arxiv:", "preprint", "submitted", "copyright")
    lines = page_texts[0].splitlines()[:24]
    for index, line in enumerate(lines):
        candidate = re.sub(r"\s+", " ", line).strip()
        lowered = candidate.lower()
        if (
            12 <= len(candidate) <= 220
            and any(char.isalpha() for char in candidate)
            and not lowered.startswith(blocked_prefixes)
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
        if any(marker in candidate for marker in ("@", "†", "‡")):
            break
        if re.search(r"\d", candidate):
            break
        if not any(char.isalpha() for char in candidate):
            break
        parts.append(candidate)
        if len(parts) == 1:
            break
    return " ".join(parts)
