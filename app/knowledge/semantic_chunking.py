"""Offset-preserving structure and semantic candidates; never synthesize source text."""

from __future__ import annotations

import re

import numpy as np

from app.knowledge.evidence_passages import passage_ranges

_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*|[A-Z])\s+[A-Z][^\n]{2,70}$")
_SENTENCE = re.compile(r'[.!?。！？]["”’\)\]]*(?:\s+|$)')


def pdf_heading_offsets(paper, root):
    from pypdf import PdfReader

    reader = PdfReader(root / paper.pdf_path)
    headings = []
    for page in paper.pages:
        raw = reader.pages[page.pdf_page - 1].extract_text() or ""
        normalized = " ".join(raw.split())
        frozen = paper.text[page.start : page.end]
        if normalized != frozen.strip():
            raise ValueError("PDF extraction no longer matches frozen source text")
        leading = len(frozen) - len(frozen.lstrip())
        offset = 0
        for line in raw.splitlines(keepends=True):
            if _HEADING.fullmatch(line.strip()):
                prefix = " ".join(raw[:offset].split())
                headings.append(page.start + leading + len(prefix) + bool(prefix))
            offset += len(line)
    return sorted(set(headings))


def structure_ranges(text, headings):
    boundaries = sorted({0, len(text), *headings})
    if boundaries[0] != 0 or boundaries[-1] != len(text):
        raise ValueError("Invalid heading offsets")
    result = []
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        for a, b, hard in passage_ranges(text[left:right]):
            result.append((left + a, left + b, hard))
    return result


def semantic_ranges(text, provider):
    """Group sentence windows at low similarity, with fixed 1200/2400/6000 char limits.

    Encode ~400-char units (not individual words); choose the least similar
    adjacent boundary between 1200 and 3600 chars nearest each 2400-char target.
    The thresholds are frozen before dev results and do not use task labels.
    """
    units, start = [], 0
    for match in _SENTENCE.finditer(text):
        if match.end() - start >= 400:
            end = match.end()
            while end - start > 6000:
                units.append((start, start + 6000))
                start += 6000
            units.append((start, end))
            start = end
    while len(text) - start > 6000:
        units.append((start, start + 6000))
        start += 6000
    if start < len(text):
        units.append((start, len(text)))
    if not units:
        return []
    vectors = np.asarray(provider.embed_documents([text[a:b] for a, b in units]))
    if vectors.ndim != 2 or len(vectors) != len(units) or not np.all(np.isfinite(vectors)):
        raise ValueError("Invalid segmentation embeddings")
    norms = np.linalg.norm(vectors, axis=1)
    if np.any(norms == 0):
        raise ValueError("Empty segmentation embedding")
    vectors = vectors / norms[:, None]
    similarities = np.sum(vectors[:-1] * vectors[1:], axis=1)
    output, index = [], 0
    while index < len(units):
        left = units[index][0]
        candidates = [i for i in range(index, len(units) - 1) if 1200 <= units[i][1] - left <= 3600]
        if candidates:
            last = min(
                candidates,
                key=lambda i: (float(similarities[i]), abs(units[i][1] - left - 2400), i),
            )
        else:
            last = index
            while last + 1 < len(units) and units[last + 1][1] - left <= 6000:
                last += 1
        end = units[last][1]
        output.append(
            (
                left,
                end,
                not bool(_SENTENCE.search(text[max(left, end - 20) : end])) and end < len(text),
            )
        )
        index = last + 1
    return output
