"""Prepare only local PDFs. Rendering is isolated in the MinerU Python environment."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .common import EN_PAPERS, PROJECT, ZH_PAPERS, digest, write
from .normalize import page_record


def corpus(zh_dir=None, en_dir=None):
    zh_dir = Path(zh_dir or PROJECT / "data/demos/chinese-rag-20260918/papers")
    en_dir = Path(en_dir or PROJECT / "data/evaluation/holdout-v4/papers")
    return [
        {"paper": name, "language": language, "pdf": str(folder / f"{name}.pdf")}
        for language, folder, names in (("zh", zh_dir, ZH_PAPERS), ("en", en_dir, EN_PAPERS))
        for name in names
    ]


def manifest(root, entries=None):
    from pypdf import PdfReader

    if (root / "attempts").exists():
        raise ValueError("Manifest is frozen after the first recorded attempt")
    papers = []
    for entry in entries if entries is not None else corpus():
        path = Path(entry["pdf"])
        count = len(PdfReader(path).pages)
        if count < 1:
            raise ValueError(f"Empty PDF: {path}")
        papers.append(
            {**entry, "sha256": digest(path), "total_pages": count, "page_count": min(12, count)}
        )
    result = {
        "schema_version": 1,
        "split": "development",
        "max_pages_per_parser": 150,
        "papers": papers,
    }
    write(root / "manifest.json", result)
    return result


def prepare(root, render_python, entries=None, run=subprocess.run):
    from pypdf import PdfReader

    from app.tools.pdf_layout import _lines, collect_text_runs, derive_document_blocks
    from app.tools.pdf_tools import collapse_fake_bold, decode_offset_glyphs
    from app.tools.text_safety import sanitize_utf8_text

    if (root / "attempts").exists():
        raise ValueError("Prepare cannot overwrite a trial with recorded attempts")
    result = manifest(root, entries)
    for paper in result["papers"]:
        reader = PdfReader(paper["pdf"])
        texts, layouts, pages = [], {}, []
        for i in range(paper["page_count"]):
            pdf_page = reader.pages[i]
            text = collapse_fake_bold(
                sanitize_utf8_text(decode_offset_glyphs(pdf_page.extract_text() or "").strip())
            )
            texts.append(text)
            layouts[i + 1] = collect_text_runs(pdf_page)
            width, height = float(pdf_page.mediabox.width), float(pdf_page.mediabox.height)
            lines = []
            for j, line in enumerate(_lines(text, i + 1, *layouts[i + 1])):
                # pdf_layout exposes x, baseline and font size, not glyph advances.
                # Keep the true anchor; do not fabricate a full-width bbox.
                anchor = [line.x, height - line.y] if line.x is not None else None
                lines.append(
                    {
                        "id": f"{paper['paper']}:p{i + 1:03d}:l{j + 1:04d}",
                        "page": i + 1,
                        "text": line.text,
                        "start": line.span.start,
                        "end": line.span.end,
                        "anchor_pt": anchor,
                        "font_size_pt": line.size,
                    }
                )
            pages.append(
                {
                    "page": i + 1,
                    "width_pt": width,
                    "height_pt": height,
                    "lines": lines,
                    "png": str((root / "pages" / paper["paper"] / f"{i + 1:03d}.png").resolve()),
                }
            )
        blocks = derive_document_blocks(texts, layouts=layouts)
        baseline = []
        for page in pages:
            page_blocks = []
            for index, item in enumerate(blocks):
                spans = [s for s in item.spans if s.page == page["page"]]
                if not spans:
                    continue
                page_blocks.append(
                    {
                        "id": f"b{index:04d}",
                        "type": item.kind,
                        "order": index,
                        "bbox_pt": None,
                        "text": "\n".join(texts[s.page - 1][s.start : s.end] for s in spans),
                        "spans": [s.model_dump() for s in spans],
                        "continuation": item.spans[0].page != page["page"],
                    }
                )
            baseline.append(
                page_record(
                    "pypdf-v3",
                    "pypdf-v3",
                    {},
                    paper["paper"],
                    page["page"],
                    page["width_pt"],
                    page["height_pt"],
                    page_blocks,
                )
            )
        write(root / "prepared" / f"{paper['paper']}.json", pages)
        write(root / "normalized/pypdf-v3" / f"{paper['paper']}.json", baseline)
        run(
            [
                str(render_python),
                str(Path(__file__).with_name("render_worker.py")),
                paper["pdf"],
                str(root / "pages" / paper["paper"]),
                str(paper["page_count"]),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    return result
