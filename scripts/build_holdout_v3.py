"""Build holdout-v3 offline from local drafts, adjudications, PDFs and ACL landing pages.

Run from the repository root: python -m scripts.build_holdout_v3
Only the output directory is written; local PDFs, texts and provenance stay untouched.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tempfile
from collections import Counter
from datetime import UTC, datetime
from html.parser import HTMLParser
from importlib.metadata import version
from pathlib import Path

from app.tools.file_hash import file_digest
from scripts.validate_holdout_v2 import (
    DATA_V3,
    OUTCOME_ANSWERABILITY,
    dataset_path,
    extract_pages,
    schemas,
    validate_dataset,
)

SOURCES = {
    "rankgpt": "2023.emnlp-main.923", "prp": "2024.findings-naacl.97",
    "query2doc": "2023.emnlp-main.585", "iter-retgen": "2023.findings-emnlp.620",
    "replug": "2024.naacl-long.463", "aar": "2023.acl-long.136",
    "m3-embedding": "2024.findings-acl.137", "skr": "2023.findings-emnlp.691",
}
FAMILIES = {
    "rerank": ("dev", ["rankgpt", "prp"]),
    "expansion": ("holdout", ["query2doc", "iter-retgen"]),
    "retriever": ("holdout", ["replug", "aar"]),
    "need": ("holdout", ["m3-embedding", "skr"]),
}
LABELERS = ["Codex (GPT): 出题者", "DeepSeek: 独立标注者", "Claude: 仲裁者"]
VERSION = "research-holdout-v3"


class CitationMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name, content = attrs.get("name", ""), attrs.get("content")
        if tag == "meta" and name.startswith("citation_") and content:
            self.fields.setdefault(name, []).append(content.strip())


def _timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def _corpus(data: Path, evidence_spans: list[dict]) -> dict:
    sources, extracted = [], {}
    for sid, acl in SOURCES.items():
        family, split = next((fid, split) for fid, (split, ids) in FAMILIES.items() if sid in ids)
        pdf = data / "papers" / f"{acl}.pdf"
        landing = data / "provenance" / f"{acl}.html"
        metadata = CitationMetadata()
        metadata.feed(landing.read_text(encoding="utf-8"))
        title = metadata.fields["citation_title"][0]
        authors = metadata.fields["citation_author"]
        text, pages = extract_pages(pdf)
        extracted[sid] = text, pages
        url = f"https://aclanthology.org/{acl}/"
        sources.append({
            "id": sid, "family_id": family, "split": split, "version": acl,
            "title": title, "authors": authors, "landing_url": url,
            "pdf_url": f"https://aclanthology.org/{acl}.pdf",
            "doi": metadata.fields.get("citation_doi", [None])[0],
            "fetched_at": _timestamp(landing), "pdf_path": f"papers/{acl}.pdf",
            "pdf_sha256": file_digest(pdf), "pdf_pages": len(pages),
            "license": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "license_evidence_url": url,
            "license_evidence": (
                "ACL Anthology landing page footer: materials published in or after 2016 "
                "are licensed under Creative Commons Attribution 4.0 International. "
                "Saved HTML identified by landing_page_sha256."
            ),
            "landing_page_sha256": file_digest(landing),
            "attribution": f"{'; '.join(authors)}. {title}. {acl}. {url}",
            "transformations": (
                "pypdf plain extraction; collapse whitespace per PDF page to one ASCII space; "
                "join pages with two LF; no dehyphenation, translation or table reconstruction. "
                "Full text is not tracked; fetched_at uses saved landing page modification time."
            ),
            "extractor": f"pypdf {version('pypdf')}",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "text_chars": len(text), "pages": pages,
        })
    spans = []
    for span in evidence_spans:
        sid, number, quote = span["source_id"], span["pdf_page"], span["quote"]
        text, pages = extracted[sid]
        if not isinstance(number, int) or not 1 <= number <= len(pages):
            raise ValueError(f"{span['id']}: invalid PDF page {number}")
        page = pages[number - 1]
        offset = text[page["start"]:page["end"]].find(quote)
        if not quote or offset < 0:
            raise ValueError(f"{span['id']}: quote not found verbatim in {sid}, PDF page {number}")
        start = page["start"] + offset
        spans.append({**span, "source_version": SOURCES[sid],
                      "start": start, "end": start + len(quote)})
    return {"version": f"{VERSION}-corpus", "full_text_tracked": False,
            "sources": sources, "spans": spans}


def _merge(original: dict, overrides: dict) -> dict:
    """Partial object overrides merge recursively; lists replace the previous list."""
    result = copy.deepcopy(original)
    for key, value in overrides.items():
        result[key] = (_merge(result[key], value)
                       if isinstance(result.get(key), dict) and isinstance(value, dict)
                       else copy.deepcopy(value))
    return result


def _annotation(record: dict | None) -> dict:
    if record is None:
        record = {
            "status": "consensus",
            "adjudication": "待仲裁：缺少独立标注及仲裁记录；verdicts 为占位。",
            "verdicts": [
                {"labeler": name, "answerability": None, "answerability_agrees": None,
                 "points": "missing", "quote_overlap": "missing"}
                for name in ("DeepSeek (待标注)", "Claude (待仲裁)")
            ],
        }
    return {
        "label_provenance": "ai-consensus", "human_verified": False,
        "drafted_by": "Codex", "status": record["status"],
        "adjudication": record["adjudication"], "verdicts": record["verdicts"],
        "tie_breaker": record.get("tie_breaker"), "record_path": "consensus.json",
    }


def _tasks(drafts: list[dict], records: dict, spans: list[dict]):
    tasks, dropped, pending, annotations = [], [], [], {}
    span_sources = {s["id"]: s["source_id"] for s in spans}
    if len({t["id"] for t in drafts}) != len(drafts):
        raise ValueError("Duplicate draft task IDs")
    unknown = records.keys() - {t["id"] for t in drafts}
    if unknown:
        raise ValueError(f"Consensus contains unknown task IDs: {sorted(unknown)}")
    for draft in drafts:
        tid = draft["id"]
        record = records.get(tid)
        if record and record.get("drop", False):
            dropped.append({"id": tid, "reason": record.get("adjudication", "仲裁删除")})
            continue
        task = _merge(draft, record.get("overrides", {}) if record else {})
        if task["id"] != tid:
            raise ValueError(f"{tid}: overrides cannot change task identity")
        exp = task["expectation"]
        evidence = sorted({s for c in exp["required_conclusions"] for s in c["evidence_span_ids"]})
        exp.setdefault("required_evidence_span_ids", evidence)
        exp.setdefault("conflict_span_ids", [])
        exp.setdefault("missing_information", [])
        tags = []
        if len({span_sources[s] for s in evidence}) >= 2:
            tags.append("multi_source")
        if len(evidence) >= 2:
            tags.append("cross_span")
        outcome_tag = {"abstain": "no_answer", "needs_review": "insufficient",
                       "scope_refusal": "out_of_scope"}.get(exp["outcome"])
        if outcome_tag:
            tags.append(outcome_tag)
        task.setdefault("tags", tags or ["exact_name"])
        task.setdefault("version", "v1")
        task.setdefault("target", "project_run")
        task.setdefault("report_requirements", [
            "用中文回答；引用允许范围内的论文证据并保留限定条件。",
        ])
        family_ids = FAMILIES[task["family_id"]][1]
        task.setdefault(
            "forbidden_source_ids",
            sorted((SOURCES.keys() if exp["outcome"] == "scope_refusal" else set(family_ids))
                   - set(task["allowed_source_ids"])),
        )
        task["annotation"] = _annotation(record)
        if record is None:
            pending.append(tid)
        # Supplied verdicts must refer to the final (possibly overridden) expected outcome.
        expected = OUTCOME_ANSWERABILITY[exp["outcome"]]
        for verdict in task["annotation"]["verdicts"]:
            if verdict["answerability"] is not None and verdict["answerability_agrees"] != (
                verdict["answerability"] == expected
            ):
                raise ValueError(f"{tid}: verdict agreement contradicts the final outcome")
        annotations[tid] = {k: task["annotation"][k]
                            for k in ("status", "verdicts", "tie_breaker", "adjudication")}
        if record and record.get("overrides"):
            annotations[tid]["overrides"] = record["overrides"]
        tasks.append(task)
    return tasks, dropped, pending, annotations


def _write(path: Path, data) -> None:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def build(data: Path = DATA_V3, output: Path | None = None) -> dict:
    output = output or dataset_path("v3")
    draft_path = data / "work/draft.json"
    consensus_path = data / "work/consensus.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    records = (json.loads(consensus_path.read_text(encoding="utf-8"))
               if consensus_path.is_file() else {})
    corpus = _corpus(data, draft["evidence_spans"])
    tasks, dropped, pending, annotations = _tasks(draft["tasks"], records, corpus["spans"])
    notice = (f"待仲裁：{len(pending)} 题使用占位 verdicts，尚未形成实际 AI 共识。"
              if pending else "已载入 AI 共识/仲裁记录；非人工核验。")
    counts = dict(Counter(t["split"] for t in tasks))
    manifest = {
        "protocol_version": f"{VERSION}-task", "dataset_version": VERSION,
        "corpus_version": f"{VERSION}-corpus", "status": "frozen_ai_consensus",
        "label_provenance": "ai-consensus", "human_verified": False,
        "language": "zh_questions_en_sources", "created_at": _timestamp(draft_path)[:10],
        "labelers": LABELERS,
        "families": [
            {"id": fid, "split": split, "source_ids": ids,
             "task_count": sum(t["family_id"] == fid for t in tasks)}
            for fid, (split, ids) in FAMILIES.items()
        ],
        "split_counts": counts, "dropped_tasks": dropped,
        "decision_use": f"{notice} 仅供研究评估；不得称为人工金标。保留集用于调优后失去保留性质。",
        "coverage_note": "4 个论文家族、8 篇 ACL 论文；中文题目、英文来源；家族间论文身份不重叠。",
        "raw_papers_are_evaluation_input": True, "annotations_are_model_input": False,
    }
    files = {
        "corpus.json": corpus, "manifest.json": manifest, "schema.json": schemas("v3"),
        "consensus.json": {
            "label_provenance": "ai-consensus", "human_verified": False,
            "statement": notice, "labelers": LABELERS, "drafted_tasks": len(draft["tasks"]),
            "kept_tasks": len(tasks), "dropped": dropped, "pending_tasks": pending,
            "input_sha256": {"draft.json": file_digest(draft_path),
                             "consensus.json": (file_digest(consensus_path)
                                                if consensus_path.is_file() else None)},
            "tasks": annotations,
        },
        **{f"tasks.{split}.json": {
            "protocol_version": f"{VERSION}-task", "dataset_version": VERSION, "split": split,
            "tasks": [t for t in tasks if t["split"] == split],
        } for split in ("dev", "holdout")},
        "README.md": (
            "# holdout-v3：AI 共识保留集\n\n"
            f"{notice}\n\n"
            "标签来源为 AI 共识（ai-consensus），非人工核验。出题者 Codex（GPT），"
            "独立标注者 DeepSeek，仲裁者 Claude；drafted_by 记为 Codex。"
            "Claude 的复核为非盲审；逐题标注、一致性与仲裁理由见 consensus.json。\n\n"
            f"共 {len(tasks)} 题，dev {counts.get('dev', 0)} 题，"
            f"holdout {counts.get('holdout', 0)} 题；删除 {len(dropped)} 题。"
            "rerank 为开发集；expansion、retriever、need 为保留集。\n\n"
            "论文均来自 ACL Anthology，许可证 CC BY 4.0；标题、作者、署名、许可链接、"
            "PDF 与落地页 SHA-256 见 corpus.json。仅跟踪元数据和短引文，不含全文。"
            "本地 papers/<ACL编号>.pdf 是规范输入，texts/ 不写入仓库。"
            "fetched_at 使用本地落地页文件修改时间，不宣称是经验证的下载时间。\n\n"
            "抽取使用 pypdf plain：逐页合并空白为一个空格，页间用两个换行连接；"
            "不修复断词、不翻译、不重建表格。span 偏移和逐字引文均基于此规则。\n\n"
            "v3 的拒答题可引用说明缺失信息的证据；越界题可将其他家族论文列为禁止来源，"
            "但允许来源仍严格限制在本家族内。\n\n"
            "在仓库根目录运行 `python -m scripts.build_holdout_v3`。"
            "默认读取 `D:/AI-Research-Agent/data/evaluation/holdout-v3`；"
            "可用 `--data`、`--output` 指定路径。缺少共识记录时生成待仲裁占位，"
            "不得将 status=consensus 或结构校验通过解释为语义核验通过。\n\n"
            "`python -m scripts.validate_holdout_v2 --dataset benchmarks/research/holdout-v3`\n\n"
            "`python -m app.benchmarking.holdout_v2 --dataset v3 --arms-preset d030 --plan`\n\n"
            "运行器仅向模型提供题号、问题和允许来源；评分器由运行 config.json 选择数据集。"
            "D030 比较 A-default（research）和 C-candidate（research_v8）。"
            "保留集用于调优后不再是保留集；这些 AI 标签不能替代人工核验。\n"
        ),
        "TASKS.md": "# holdout-v3 题目\n\n" + notice + "\n\n" + "\n\n".join(
            f"## {t['id']}（{t['split']} / {t['kind']} / {t['difficulty']}）\n\n"
            f"{t['question']}\n\n允许来源：{', '.join(t['allowed_source_ids'])}。\n\n"
            f"预期处理：{t['expectation']['outcome']}。\n\n"
            + "\n".join(f"- {c['statement']}" for c in t['expectation']['required_conclusions'])
            for t in tasks
        ) + "\n",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and {p.name for p in output.iterdir()} - (files.keys() | {"freeze.json"}):
        raise ValueError(f"Output contains unexpected files: {output}")
    # Validate in staging before replacing any existing output file.
    with tempfile.TemporaryDirectory(prefix="holdout-v3-", dir=output.parent) as folder:
        stage = Path(folder)
        for name, content in files.items():
            _write(stage / name, content)
        _write(stage / "freeze.json", {name: file_digest(stage / name) for name in sorted(files)})
        result = validate_dataset(stage, data=None, version="v3")
        output.mkdir(parents=True, exist_ok=True)
        for path in stage.iterdir():
            (output / path.name).write_bytes(path.read_bytes())
    return {**result, "quotes_checked_against_pdf_text": len(corpus["spans"]),
            "output": str(output), "pending_adjudication": len(pending), "notice": notice}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_V3)
    parser.add_argument("--output", type=Path, default=dataset_path("v3"))
    args = parser.parse_args()
    print(json.dumps(build(args.data, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
