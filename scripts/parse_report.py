"""Local-only review aid: python -m scripts.parse_report (no pass/fail threshold)."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from app.knowledge.service import KnowledgeIngestionService
from app.retrieval.anchors import locate_quote
from app.schemas.documents import PaperMetadata
from app.tools.pdf_layout import CJK_GAPS, block_counts, clean_body_line
from app.tools.pdf_tools import parse_pdf_source


def _distribution(lengths):
    return {"count": len(lengths), "min": min(lengths, default=0),
            "median": statistics.median(lengths) if lengths else 0,
            "max": max(lengths, default=0),
            "bins": dict(Counter("<=300" if n <= 300 else "301-1200" if n <= 1200
                                 else ">1200" for n in lengths))}


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chinese", type=Path,
                        default=Path("data/demos/chinese-rag-20260918/papers"))
    parser.add_argument("--english", type=Path, default=Path("data/evaluation/holdout-v4/papers"))
    parser.add_argument("--output", type=Path, default=Path(".pytest-tmp/parse-a-report"))
    args = parser.parse_args()
    output = args.output.resolve()
    workspace = Path(__file__).resolve().parents[1]
    if not output.is_relative_to(workspace / ".pytest-tmp"):
        raise ValueError("Report output must stay under this worktree's .pytest-tmp")
    output.mkdir(parents=True, exist_ok=True)
    # Compare against the actual pre-change implementation, not a rewritten oracle.
    baseline_code = subprocess.check_output(
        ["git", "show", "HEAD:app/tools/pdf_tools.py"], cwd=workspace,
    ).decode("utf-8")
    baseline = {"__name__": "parse_report_baseline"}
    exec(compile(baseline_code, "HEAD:app/tools/pdf_tools.py", "exec"), baseline)
    service = object.__new__(KnowledgeIngestionService)
    service.settings = SimpleNamespace(chunk_size=900, chunk_overlap=150)
    paths = [*sorted(args.chinese.glob("*.pdf")), *sorted(args.english.glob("*.pdf"))[:4]]
    summaries = []
    for path in paths:
        legacy = baseline["parse_pdf_source"](str(path), max_pages=12)
        parsed = parse_pdf_source(str(path), max_pages=12, derive_blocks=True)
        paper = PaperMetadata(id=path.stem, title=parsed.title or path.stem)
        old = service._page_chunks(paper, parsed, parser_version="pypdf-v2")
        new = service._page_chunks(paper, parsed, parser_version="pypdf-v3")
        pages = dict(zip(parsed.page_numbers, parsed.page_texts, strict=True))
        blocks = parsed.blocks or []
        before = after = 0
        unanchored = []
        for index, block in enumerate(blocks):
            raw = "\n".join(pages[s.page][s.start:s.end] for s in block.spans)
            if block.kind in {"paragraph", "abstract"}:
                before += len(CJK_GAPS.findall(raw))
                after += len(CJK_GAPS.findall(block.text))
            if locate_quote(block.text, raw) is None:
                unanchored.append(index)
        summary = {
            "paper": path.name, "pages": parsed.pages, "blocks": block_counts(blocks),
            "body_cjk_gaps_before": before, "body_cjk_gaps_after": after,
            "all_raw_cjk_gaps": sum(len(CJK_GAPS.findall(t)) for t in parsed.page_texts),
            "all_derived_cjk_gaps": sum(len(CJK_GAPS.findall(b.text)) for b in blocks),
            "paragraph_lengths": _distribution([len(b.text) for b in blocks
                                                  if b.kind == "paragraph"]),
            "old_chunks": _distribution([len(c.text) for c in old]),
            "new_chunks": _distribution([len(c.text) for c in new]),
            "unanchored_blocks": unanchored,
            "raw_hash_equal": _hash(legacy.text) == _hash(parsed.text),
            "page_hashes_equal": [_hash(t) for t in legacy.page_texts]
                                  == [_hash(t) for t in parsed.page_texts],
            "page_numbers_equal": legacy.page_numbers == parsed.page_numbers,
            "legacy_sha256": _hash(legacy.text), "current_sha256": _hash(parsed.text),
        }
        details = {**summary, "per_page": [],
                   "blocks_full": [b.model_dump() for b in blocks],
                   "old_chunks_full": [c.model_dump() for c in old],
                   "new_chunks_full": [c.model_dump() for c in new]}
        for number in parsed.page_numbers:
            page_blocks = [b for b in blocks if any(s.page == number for s in b.spans)]
            kinds = sorted({b.kind for b in page_blocks})
            body_lines = [line for b in page_blocks if b.kind in {"paragraph", "abstract"}
                          for s in b.spans if s.page == number
                          for line in pages[number][s.start:s.end].splitlines()]
            details["per_page"].append({
                "page": number, "counts": block_counts(page_blocks),
                "body_cjk_gaps_before": sum(len(CJK_GAPS.findall(t)) for t in body_lines),
                "body_cjk_gaps_after": sum(len(CJK_GAPS.findall(clean_body_line(t)))
                                           for t in body_lines),
                "examples": {kind: [b.text[:120] for b in page_blocks if b.kind == kind][:3]
                             for kind in kinds},
                "excluded": [b.model_dump() for b in page_blocks if b.kind in
                             {"front_matter", "header", "footer", "references"}],
            })
        (output / f"{path.stem}.json").write_text(
            json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    (output / "summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    rows = [
        "本地解析测量（每篇最多 12 页；计数为块数，非准确率；无自动通过门槛）。",
        "",
        "正文空格只统计 paragraph/abstract；未清洗的标题、表格、参考文献另见 JSON。",
        "",
        "|论文|页|前置/页眉/页脚/文献|正文空格 前→后|段落数|段落长度 最小/中位/最大|"
        "切片数 旧→新|新切片长度 最小/中位/最大|原文哈希一致|",
        "|---|---:|---|---|---:|---|---|---|---|",
    ]
    for summary in summaries:
        counts = summary["blocks"]
        excluded = "/".join(str(counts.get(k, 0)) for k in
                            ("front_matter", "header", "footer", "references"))
        paragraph = summary["paragraph_lengths"]
        chunk = summary["new_chunks"]
        rows.append(
            f"|{summary['paper']}|{summary['pages']}|{excluded}|"
            f"{summary['body_cjk_gaps_before']}→{summary['body_cjk_gaps_after']}|"
            f"{paragraph['count']}|{paragraph['min']}/{paragraph['median']}/{paragraph['max']}|"
            f"{summary['old_chunks']['count']}→{chunk['count']}|"
            f"{chunk['min']}/{chunk['median']}/{chunk['max']}|"
            f"{summary['raw_hash_equal'] and summary['page_hashes_equal']}|"
        )
    rows.extend(["", "每篇 JSON 含逐页类型计数、120 字示例、排除文本、块偏移、"
                 "完整新旧切片对照和长度分桶；summary.json 含修改前后 SHA-256。"])
    (output / "summary.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"Review artifacts: {output}")


if __name__ == "__main__":
    main()
