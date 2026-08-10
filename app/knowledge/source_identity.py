from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class SourceIdentity:
    """Stable source identity without rewriting the user-provided source URI."""

    canonical_uri: str
    version: str
    content_sha256: str


def canonicalize_source_uri(uri: str) -> str:
    """Normalize a source URI deterministically while preserving query semantics.

    HTTP(S) identities ignore whitespace, URL fragments, host casing, default ports,
    and lexical path noise.  Other inputs are treated as local paths unless they
    are an explicitly hierarchical URI such as ``legacy://...``.
    """

    value = uri.strip()
    if not value:
        return ""

    parsed = urlsplit(value)
    if parsed.scheme and (parsed.netloc or parsed.scheme == "file"):
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            host = f"{host}:{port}"
        path = _normalize_url_path(parsed.path)
        return urlunsplit((scheme, host, path, parsed.query, ""))

    return _normalize_path(value)


def derive_source_identity(
    *, uri: str, content_sha256: str, metadata: dict[str, object]
) -> SourceIdentity:
    """Derive identity fields for a newly persisted source version.

    An upstream source version (for example an ETag or a publisher revision) is
    authoritative when supplied.  Otherwise the parsed content checksum is the
    version, making changes explicit without mutating older Source rows.
    """

    explicit_version = next(
        (
            str(metadata[key]).strip()
            for key in ("source_version", "etag", "version")
            if metadata.get(key) is not None and str(metadata[key]).strip()
        ),
        "",
    )
    version = explicit_version or (
        f"content-sha256:{content_sha256}" if content_sha256 else "legacy-unknown"
    )
    return SourceIdentity(
        canonical_uri=canonicalize_source_uri(uri),
        version=version,
        content_sha256=content_sha256,
    )


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _normalize_url_path(path: str) -> str:
    normalized = posixpath.normpath(path or "/")
    if path.endswith("/") and normalized != "/":
        normalized = f"{normalized}/"
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _normalize_path(path: str) -> str:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return "." if normalized == "" else normalized
