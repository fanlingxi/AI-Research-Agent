"""File fingerprints shared by frozen datasets and archive readers."""

import hashlib
from pathlib import Path


def file_digest(path: Path) -> str:
    """Preserve frozen hashes: PDF bytes are exact; UTF-8 text normalizes newlines."""
    content = path.read_bytes() if path.suffix == ".pdf" else path.read_text(
        encoding="utf-8"
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()
