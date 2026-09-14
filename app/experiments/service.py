"""Read-only views of managed evaluation artifacts; never import them as knowledge."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

from app.tools.file_hash import file_digest

ROOT = Path(__file__).resolve().parents[2]
STRATEGIES = ("legacy", "bm25-v1", "hybrid-v1")
PATHS = ("project_run", "quick_report")


class ExperimentDataError(ValueError):
    pass


def _number(value):
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def _safe_text(value):
    text = str(value or "")[:20000]
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[已隐藏密钥]", text)
    return re.sub(r"(?i)(api[_ -]?key|authorization|password)\s*[:=]\s*\S+", r"\1=[已隐藏]", text)


class ExperimentReadService:
    def __init__(
        self, root: Path | None = None, dataset: Path | None = None,
        semantic_archive: Path | None = None,
    ):
        self.root = (root or ROOT / "data/evaluation").resolve()
        self.dataset = (dataset or ROOT / "benchmarks/research/papers-v1").resolve()
        self.semantic_archive = (semantic_archive or ROOT / "docs/improvement/a05").absolute()

    def semantic_observation(self):
        from app.experiments.observation import read_observation

        return read_observation(self)

    @staticmethod
    def _read(base: Path, path: Path, *, optional=False):
        if not path.resolve().is_relative_to(base):
            raise ExperimentDataError("评测文件位置不在受管目录内。")
        if not path.exists() and optional:
            return None
        try:
            if path.stat().st_size > 24_000_000:
                raise ExperimentDataError("评测文件超过读取上限。")
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExperimentDataError("评测文件缺失或损坏，请检查本机归档。") from exc

    def _papers(self):
        freeze = self._read(self.dataset, self.dataset / "freeze.json")
        data = {}
        for name in ("corpus.json", "tasks.dev.json", "tasks.holdout.json"):
            path = self.dataset / name
            value = self._read(self.dataset, path)
            if file_digest(path) != freeze.get(name):
                raise ExperimentDataError("论文或任务内容不符合冻结指纹。")
            data[name] = value
        tasks = {
            t["id"]: t
            for name in ("tasks.dev.json", "tasks.holdout.json")
            for t in data[name]["tasks"]
        }
        return data["corpus.json"], tasks

    def _directory(self, experiment_id):
        match = re.fullmatch(r"(a02|a04)--([A-Za-z0-9][A-Za-z0-9_-]{0,120})", experiment_id)
        if not match:
            raise KeyError(experiment_id)
        kind, name = match.groups()
        directory = self.root / kind / name
        if not directory.resolve().is_relative_to(self.root / kind):
            raise ExperimentDataError("评测目录位置无效。")
        if not directory.is_dir():
            raise KeyError(experiment_id)
        return kind, directory

    def _load(self, experiment_id):
        kind, directory = self._directory(experiment_id)
        config = self._read(self.root, directory / "config.json")
        summary = self._read(self.root, directory / "summary.json")
        if not isinstance(config, dict) or not isinstance(summary, dict):
            raise ExperimentDataError("评测配置或汇总结构无效。")
        supported = (
            config.get("schema") == "a04-retrieval-comparison-v1"
            if kind == "a04"
            else config.get("version") in {"a02-live-v1", "a02-live-v2"}
        )
        if not supported:
            raise ExperimentDataError("该评测格式尚不支持展示。")
        corpus, tasks = self._papers()
        expected = (
            config.get("dataset_freeze_sha256")
            if kind == "a04"
            else config.get("approval", {}).get("freeze_sha256")
        )
        if expected != file_digest(self.dataset / "freeze.json"):
            raise ExperimentDataError("评测与当前冻结论文集不匹配。")
        if kind == "a04":
            raw = self._read(self.root, directory / "results.json", optional=True)
            raw = [] if raw is None else raw
            if not isinstance(raw, list):
                raise ExperimentDataError("逐题结果结构无效。")
            by_key = {}
            for row in raw:
                key = (row.get("task_id"), row.get("path"), row.get("strategy"))
                if (
                    key in by_key
                    or key[0] not in tasks
                    or key[1] not in PATHS
                    or key[2] not in STRATEGIES
                    or tasks[key[0]]["split"] != config.get("split")
                ):
                    raise ExperimentDataError("逐题身份重复或超出实验范围。")
                by_key[key] = row
            rows = [
                dict(
                    by_key.get((task["id"], path, strategy), {}),
                    task_id=task["id"],
                    path=path,
                    strategy=strategy,
                )
                for task in tasks.values()
                if task["split"] == config.get("split")
                for path in PATHS
                for strategy in STRATEGIES
            ]
        else:
            raw = summary.get("results", [])
            if not isinstance(raw, list):
                raise ExperimentDataError("逐题结果结构无效。")
            strategy = str(config.get("strategy", "legacy")).split(";", 1)[0].strip()
            if config.get("version") == "a02-live-v1":
                strategy = "legacy"
            if strategy not in STRATEGIES:
                raise ExperimentDataError("生成实验的检索策略无法识别。")
            rows = []
            for index, row in enumerate(raw, 1):
                if row.get("task_id") not in tasks:
                    raise ExperimentDataError("评测包含未知题目。")
                rows.append(
                    dict(
                        row,
                        path="project_run",
                        strategy=strategy,
                        folder=f"{index:02d}-{row['task_id']}",
                    )
                )
            if len({r["task_id"] for r in rows}) != len(rows):
                raise ExperimentDataError("同一次生成实验包含重复题目。")
        return kind, directory, config, summary, corpus, tasks, rows

    def list_experiments(self):
        available, unavailable = [], []
        for family in ("a04", "a02"):
            base = self.root / family
            if not base.resolve().is_relative_to(self.root):
                continue
            for directory in sorted(base.iterdir(), reverse=True) if base.is_dir() else []:
                if not directory.is_dir() or not (directory / "config.json").exists():
                    continue
                experiment_id = f"{family}--{directory.name}"
                try:
                    available.append(self.describe(experiment_id)["experiment"])
                except (ExperimentDataError, KeyError, TypeError, AttributeError) as exc:
                    unavailable.append(
                        {
                            "id": experiment_id,
                            "reason": str(exc)
                            if isinstance(exc, ExperimentDataError)
                            else "评测结构无效。",
                        }
                    )
        return {"experiments": available, "unavailable": unavailable}

    def _variant(self, kind, config, summary, row):
        status = row.get("status", "missing")
        status = "completed" if status == "ok" else status
        calls = [
            c
            for c in summary.get("budget", {}).get("calls", [])
            if row.get("attempt_id") and c.get("attempt_id") == row["attempt_id"]
        ]
        upper = None
        if calls and all(
            c.get("status") == "settled" and _number(c.get("cost_upper_micro_cny")) is not None
            for c in calls
        ):
            upper = sum(c["cost_upper_micro_cny"] for c in calls) / 1_000_000
        return {
            "strategy": row["strategy"],
            "path": row["path"],
            "status": status,
            "metrics": {k: _number(v) for k, v in row.get("metrics", {}).items()},
            "span_recall": _number(
                row.get("budget_span_recall")
                if kind == "a04"
                else row.get("retrieval", {}).get("span_recall")
            ),
            "latency_ms": _number(row.get("latency_ms"))
            if kind == "a04"
            else (
                _number(row.get("elapsed_seconds")) * 1000
                if _number(row.get("elapsed_seconds")) is not None
                else None
            ),
            "cost_cny": _number(row.get("cost_cny")) if kind == "a04" else None,
            "cost_upper_cny": upper,
            "model_calls": _number(row.get("model_calls"))
            if kind == "a04"
            else (len(calls) if calls else None),
            "model": "未调用（仅检索）"
            if kind == "a04"
            else config.get("policy", {}).get("request_model", "未知"),
            "semantic_success": None,
            "semantic_review": "未测量" if kind == "a04" else "完整语义评分未核验",
            "error": _safe_text(row.get("error")),
            "evidence": [],
            "issues": [],
            "content": None,
        }

    def describe(self, experiment_id):
        kind, directory, config, summary, _, tasks, rows = self._load(experiment_id)
        grouped = {}
        for row in rows:
            task_id = row["task_id"]
            grouped.setdefault(
                task_id,
                {
                    "id": task_id,
                    "question": tasks[task_id]["question"],
                    "split": tasks[task_id]["split"],
                    "variants": [],
                },
            )["variants"].append(self._variant(kind, config, summary, row))
        counts = dict(Counter(v["status"] for task in grouped.values() for v in task["variants"]))
        experiment = {
            "id": experiment_id,
            "name": directory.name,
            "kind": "retrieval" if kind == "a04" else "generation",
            "planned": len(rows) if kind == "a04" else summary.get("planned_count", len(rows)),
            "recorded": len(rows) - counts.get("missing", 0),
            "task_count": len(grouped),
            "status_counts": counts,
            "splits": sorted({t["split"] for t in grouped.values()}),
            "model": "本地 hash 向量 · 384 维"
            if kind == "a04"
            else config.get("policy", {}).get("request_model", "未知"),
            "decision": "保留旧策略默认；采用结论以本批次评测汇总为准"
            if kind == "a04" and summary.get("default_decision") == "retain_legacy"
            else "未记录采用结论",
            "limitations": ["检索命中不等于生成回答正确。", "缺失评分与未知费用保留为空。"]
            + (
                ["本地 hash 向量对照，不代表神经语义检索或线上延迟。"]
                if kind == "a04"
                else ["运行完成不等于语义通过；费用上界不等于实际账单。"]
            ),
        }
        return {"experiment": experiment, "tasks": list(grouped.values())}

    @staticmethod
    def _evidence(corpus, task, locations):
        papers = {p["id"]: p for p in corpus["sources"]}
        result = []
        for rank, location in enumerate(locations, 1):
            paper = papers.get(location.get("source_id"))
            if (
                paper is None
                or paper["id"] not in task["allowed_source_ids"]
                or location.get("source_version") != paper["version"]
                or location.get("pdf_sha256") != paper["pdf_sha256"]
            ):
                raise ExperimentDataError("原文身份、版本或题目允许范围不匹配。")
            start, end = location.get("start"), location.get("end")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= len(paper["text"])
                or not any(
                    p["pdf_page"] == location.get("pdf_page")
                    and p["start"] <= start < end <= p["end"]
                    for p in paper["pages"]
                )
            ):
                raise ExperimentDataError("原文页码或跨度无效。")
            text = paper["text"][start:end]
            if (
                location.get("text_sha256")
                and hashlib.sha256(text.encode()).hexdigest() != location["text_sha256"]
            ):
                raise ExperimentDataError("原文内容指纹不匹配。")
            result.append(
                {
                    "rank": rank,
                    "source_id": paper["id"],
                    "title": paper["title"],
                    "version": paper["version"],
                    "page": location["pdf_page"],
                    "start": start,
                    "end": end,
                    "text": text,
                }
            )
        return result

    def task_detail(self, experiment_id, task_id, path="project_run"):
        kind, directory, config, summary, corpus, tasks, rows = self._load(experiment_id)
        selected = [r for r in rows if r["task_id"] == task_id and r["path"] == path]
        if not selected:
            raise KeyError(task_id)
        task, variants = tasks[task_id], []
        for row in selected:
            variant = self._variant(kind, config, summary, row)
            try:
                if kind == "a04":
                    locations = [item for unit in row.get("selected", []) for item in unit]
                else:
                    folder = directory / row["folder"]
                    run = self._read(self.root, folder / "run.json", optional=True)
                    if run:
                        variant["error"] = _safe_text(run.get("error_message")) or variant["error"]
                    output = self._read(self.root, folder / "output.json", optional=True)
                    if output:
                        variant["content"] = _safe_text(output.get("rendered_text"))
                        if len(str(output.get("rendered_text") or "")) > 20000:
                            variant["issues"].append(
                                "报告超过展示上限，仅显示前20000字符；完整产物保留在归档中。"
                            )
                    elif row.get("status") == "completed":
                        variant["issues"].append("运行标为完成，但产物文件缺失。")
                    snapshot = self._read(self.root, folder / "snapshot.json", optional=True)
                    mapping = self._read(self.root, directory / "source-map.json", optional=True)
                    locations = []
                    if snapshot and mapping:
                        from app.context.models import ContextPackage, canonical_package_sha256

                        package = ContextPackage.model_validate(snapshot)
                        if canonical_package_sha256(package) != row.get("snapshot_sha256"):
                            raise ExperimentDataError("历史快照指纹不匹配。")
                        locations = [
                            mapping["chunks"][chunk.chunk_id]
                            for bundle in package.knowledge.claim_bundles
                            for chunk in bundle.chunks
                        ]
                    else:
                        variant["issues"].append("历史快照或来源映射缺失，无法查看原文。")
                variant["evidence"] = self._evidence(corpus, task, locations)
            except (ExperimentDataError, ValueError, KeyError, TypeError, AttributeError) as exc:
                variant["issues"].append(
                    str(exc)
                    if isinstance(exc, ExperimentDataError)
                    else "产物结构无效，未展示未经核验的原文。"
                )
            variants.append(variant)
        return {
            "task_id": task_id,
            "question": task["question"],
            "split": task["split"],
            "path": path,
            "variants": variants,
        }
