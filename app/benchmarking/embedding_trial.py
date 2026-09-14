"""Explicit real-embedding dev comparisons through both production retrieval paths."""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from statistics import mean
from uuid import uuid4

from app.benchmarking import feedback_trial
from app.benchmarking.live import APPROVAL, DATASET, ROOT, code_fingerprint
from app.benchmarking.live import write_json as _write_json
from app.benchmarking.live_dataset import approved_dataset, task_input
from app.benchmarking.passage_dataset import import_passages
from app.benchmarking.retrieval_compare import (
    LocalHashProjection,
    NoGraphProjection,
    retrieval_metrics,
)
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.retrieval import QdrantContextCandidateRetriever
from app.context.service import ContextBuilderService
from app.knowledge.query import KnowledgeQueryService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.semantic_chunking import pdf_heading_offsets, semantic_ranges, structure_ranges
from app.retrieval.neural_embeddings import LocalEmbeddingProvider, model_identity
from app.retrieval.semantic_projection import SemanticProjection

FIXED_SEED = ROOT / "data/evaluation/a08-passages/20260914T035545Z-77f83ffd"
OUTPUT_ROOT = ROOT / "data/evaluation/a08-embedding"
VALIDATION_QUESTIONS = ROOT / "docs/improvement/a08-embedding/new-questions.json"
VARIANTS = [
    ("current", "legacy"),
    ("none", "bm25-v1"),
    ("hash", "hybrid-v1"),
    ("qwen3-local", "dense-v1"),
    ("qwen3-local", "hybrid-v1"),
    ("bge-m3-local", "dense-v1"),
    ("bge-m3-local", "hybrid-v1"),
]


def write_json(path, value):
    # Windows scanners can briefly hold a destination open during atomic replace.
    for attempt in range(5):
        try:
            _write_json(path, value)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def error_details(exc):
    return {
        "error_type": type(exc).__name__,
        "errno": getattr(exc, "errno", None),
        "frames": [
            {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
            for f in traceback.extract_tb(exc.__traceback__)[-5:]
        ],
    }


def runtime_versions():
    versions = {}
    for name in ("torch", "sentence-transformers", "transformers", "numpy"):
        try:
            versions[name] = package_version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


class ObservedProjection:
    """Retain encoder failures even when a production hybrid path degrades gracefully."""

    def __init__(self, projection):
        self.projection = projection
        self.failures = []

    def search(self, *args, **kwargs):
        try:
            return self.projection.search(*args, **kwargs)
        except Exception as exc:
            self.failures.append(type(exc).__name__)
            raise


def summarize(rows):
    summary = []
    keys = sorted({(r["chunking"], r["model"], r["strategy"], r["path"]) for r in rows})
    for chunking, model, strategy, path in keys:
        group = [
            r
            for r in rows
            if (r["chunking"], r["model"], r["strategy"], r["path"])
            == (chunking, model, strategy, path)
        ]
        metric_keys = ("recall@10", "mrr@10", "ndcg@10")
        summary.append(
            {
                "chunking": chunking,
                "model": model,
                "strategy": strategy,
                "path": path,
                "planned": len(group),
                "failed": sum(r["status"] == "failed" for r in group),
                "unfinished": sum(r["status"] not in {"ok", "failed"} for r in group),
                "no_gold": sum(r["status"] == "ok" and not r.get("gold_count") for r in group),
                "scored": sum(
                    r["status"] == "ok" and r.get("metrics", {}).get("recall@10") is not None
                    for r in group
                ),
                "metrics": {
                    k: mean(values)
                    if (
                        values := [
                            r.get("metrics", {}).get(k)
                            for r in group
                            if r["status"] == "ok" and r.get("metrics", {}).get(k) is not None
                        ]
                    )
                    else None
                    for k in metric_keys
                },
            }
        )
    return summary


def choose_model(summary):
    expected = {(m, s, p) for m, s in VARIANTS for p in ("project_run", "quick_report")}
    actual = {(r["model"], r["strategy"], r["path"]) for r in summary}
    if actual != expected or any(r["failed"] or r["unfinished"] for r in summary):
        return None
    eligible = []
    for model, strategy in VARIANTS:
        if model not in {"qwen3-local", "bge-m3-local"}:
            continue
        pair = [r for r in summary if r["model"] == model and r["strategy"] == strategy]
        if len(pair) == 2 and all(r["failed"] == 0 and r["scored"] for r in pair):
            eligible.append(
                (
                    mean(r["metrics"]["recall@10"] for r in pair),
                    mean(r["metrics"]["mrr@10"] for r in pair),
                    model,
                    strategy,
                )
            )
    if not eligible:
        return None
    best = sorted(eligible, key=lambda x: (-x[0], -x[1], x[2], x[3]))[0]
    return {"model": best[2], "strategy": best[3], "purpose": "dev candidate; not default adoption"}


def quality_gate(groups, baseline):
    """Development evidence only; later unseen questions and human review still required."""
    result = []
    for group in groups:
        old = next((r for r in baseline if r["path"] == group["path"]), None)
        valid = old is not None and all(
            r["failed"] == 0 and r["unfinished"] == 0 and r["scored"] > 0 for r in (group, old)
        )
        deltas = (
            {k: group["metrics"][k] - old["metrics"][k] for k in ("recall@10", "mrr@10", "ndcg@10")}
            if valid
            else {}
        )
        result.append(
            {
                "chunking": group["chunking"],
                "model": group["model"],
                "strategy": group["strategy"],
                "path": group["path"],
                "deltas": deltas,
                "passed": bool(
                    valid
                    and deltas["recall@10"] > 0
                    and deltas["mrr@10"] >= -0.02
                    and deltas["ndcg@10"] >= -0.02
                ),
            }
        )
    return result


def choose_chunking(groups, baseline, candidate):
    if (
        len(groups) != 4
        or len(baseline) != 2
        or any(r["failed"] or r["unfinished"] or not r["scored"] for r in groups + baseline)
    ):
        return None
    gates = quality_gate(groups, baseline)
    options = [(mean(r["metrics"]["recall@10"] for r in baseline), "sentence-2400-v2")]
    for cut in ("structure-v1", "semantic-v1"):
        if all(r["passed"] for r in gates if r["chunking"] == cut):
            options.append(
                (mean(r["metrics"]["recall@10"] for r in groups if r["chunking"] == cut), cut)
            )
    chosen = sorted(options, key=lambda item: (-item[0], item[1]))[0][1]
    return {
        **candidate,
        "chunking": chosen,
        "purpose": "frozen new-question validation candidate; not production adoption",
    }


def run(*, execute=False, stage="embeddings", from_run=None, device="cpu"):
    if stage not in {"embeddings", "chunking"} or device not in {"cpu", "cuda"}:
        raise ValueError("Unknown comparison stage or device")
    if not execute:
        return {
            "mode": "dry_run",
            "stage": stage,
            "dev_tasks": 25,
            "planned": 350 if stage == "embeddings" else 100,
            "generation_calls": 0,
        }
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Real model entry disabled in offline tests")
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    dev = [t for t in tasks if t.split == "dev"]
    candidate = None
    parent_fingerprints = {}
    previous_groups = []
    if stage == "chunking":
        previous = Path(from_run or "").resolve()
        if previous.parent != OUTPUT_ROOT.resolve():
            raise ValueError("Chunking requires a managed embedding comparison")
        config = feedback_trial.read(previous / "config.json")
        if (
            config["dataset_freeze_sha256"] != approval["freeze_sha256"]
            or config["stage"] != "embeddings"
            or config["device"] != device
            or config["task_ids"] != [t.id for t in dev]
            or config.get("new_questions_sha256") != feedback_trial.digest(VALIDATION_QUESTIONS)
        ):
            raise ValueError("Dataset changed between stages")
        previous_rows = feedback_trial.read(previous / "results.json")
        if len(previous_rows) != 350:
            raise ValueError("Incomplete embedding comparison")
        previous_groups = summarize(previous_rows)
        candidate = choose_model(previous_groups)
        if candidate != feedback_trial.read(previous / "summary.json")["candidate"]:
            raise ValueError("Embedding candidate does not match recorded results")
        if not candidate:
            raise ValueError("Embedding comparison has no usable candidate")
        parent_fingerprints = {
            str(previous / name): feedback_trial.digest(previous / name)
            for name in ("config.json", "results.json", "summary.json")
        }
    output = OUTPUT_ROOT / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{stage}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    versions = ["sentence-2400-v2"] if stage == "embeddings" else ["structure-v1", "semantic-v1"]
    variants = VARIANTS if candidate is None else [(candidate["model"], candidate["strategy"])]
    rows = [
        dict(task_id=t.id, chunking=c, model=m, strategy=s, path=p, status="not_attempted")
        for c in versions
        for m, s in variants
        for t in dev
        for p in ("project_run", "quick_report")
    ]
    write_json(
        output / "config.json",
        {
            "schema": "a08-embedding-trial-v1",
            "code": code_fingerprint(),
            "stage": stage,
            "dataset_freeze_sha256": approval["freeze_sha256"],
            "task_ids": [t.id for t in dev],
            "new_questions_sha256": feedback_trial.digest(VALIDATION_QUESTIONS),
            "device": device,
            "runtime_versions": runtime_versions(),
            "models": [model_identity(m) for m in ["qwen3-local", "bge-m3-local"]],
            "context_max_tokens": 16000,
            "quick_report_top_k": 20,
            "variants": variants,
            "candidate": candidate,
            "from_run": str(from_run),
            "parent_fingerprints": parent_fingerprints,
            "holdout": "not_used",
            "semantic_limits": {"unit_chars": 400, "min": 1200, "target": 2400, "max": 6000},
            "generation_calls": 0,
            "default_changed": False,
            "projection": "local numpy cosine via production retrieval ports; no Qdrant timing",
            "query_cache": "shared across strategies; latency includes mixed cold/warm calls",
            "current_baseline": "project vector disabled; quick_report uses existing hash encoder",
        },
    )
    write_json(output / "results.json", rows)
    protected_paths = [
        FIXED_SEED / "seed.db",
        FIXED_SEED / "source-map.json",
        ROOT / "data/knowledge/knowledge.db",
    ]
    protected = {p: feedback_trial.digest(p) for p in protected_paths if p.exists()}
    protected.update({Path(p): digest for p, digest in parent_fingerprints.items()})
    print(f"Embedding comparison: {output}", flush=True)
    providers = {}
    spans = {s.id: s.model_dump() for s in corpus.spans}
    try:
        for version in versions:
            folder = output / version
            folder.mkdir()
            if stage == "embeddings":
                feedback_trial.backup_archive(FIXED_SEED / "seed.db", folder / "knowledge.db")
                repo = KnowledgeRepository(str(folder / "knowledge.db"))
                mapping = feedback_trial.read(FIXED_SEED / "source-map.json")
            else:
                model = candidate["model"]
                provider = providers.setdefault(model, LocalEmbeddingProvider(model, device=device))
                ranges = {}
                for paper in corpus.sources:
                    ranges[paper.id] = (
                        structure_ranges(paper.text, pdf_heading_offsets(paper, DATASET))
                        if version == "structure-v1"
                        else semantic_ranges(paper.text, provider)
                    )
                write_json(folder / "ranges.json", ranges)
                repo = KnowledgeRepository(str(folder / "knowledge.db"))
                mapping = import_passages(
                    repo, corpus, DATASET, version=version, ranges_by_source=ranges
                )
            write_json(folder / "source-map.json", mapping)
            workspace = {}
            for task in dev:
                inputs = task_input(task)
                mem = repo.memory_repository
                project = mem.create_project(
                    name=f"Embedding {task.id}",
                    goal="仅依据授权论文回答问题",
                    domain="research",
                    metadata={"evaluation_only": True},
                )
                scopes = [mapping["sources"][s]["scope"] for s in inputs["allowed_source_ids"]]
                mem.replace_project_knowledge_scopes(
                    project.id, scopes, expected_project_revision=project.revision
                )
                work = mem.create_workspace_task(
                    project_id=project.id,
                    title=task.question[:200],
                    goal=task.question,
                    priority="high",
                    metadata={"expected_output": "\n".join(inputs["report_requirements"])},
                )
                workspace[task.id] = work, scopes
            projections = {}
            projection_errors = {}
            for model, strategy in variants:
                model_rows = [
                    r
                    for r in rows
                    if r["chunking"] == version
                    and r["model"] == model
                    and r["strategy"] == strategy
                ]
                try:
                    if model in projection_errors:
                        for row in model_rows:
                            row.update(status="failed", **projection_errors[model])
                        write_json(output / "results.json", rows)
                        continue
                    if model not in projections:
                        if model == "none":
                            projections[model] = NoGraphProjection()
                        elif model in {"hash", "current"}:
                            projections[model] = LocalHashProjection(repo)
                        else:
                            provider = providers.setdefault(
                                model, LocalEmbeddingProvider(model, device=device)
                            )
                            projections[model] = SemanticProjection(
                                repo, provider, folder / f"index-{model}"
                            )
                        print(f"{version}/{model}: projection ready", flush=True)
                    projection = ObservedProjection(projections[model])
                    builder = ContextBuilderService(
                        repo, vector_retriever=QdrantContextCandidateRetriever(projection)
                    )
                    query_service = KnowledgeQueryService(
                        repo,
                        chunk_search=projection,
                        graph_search=NoGraphProjection(),
                        settings=Settings(_env_file=None, report_retrieval_strategy=strategy),
                    )
                    for task in dev:
                        work, scopes = workspace[task.id]
                        gold = [spans[key] for key in task.expectation.required_evidence_span_ids]
                        universe = [
                            [v]
                            for v in mapping["chunks"].values()
                            if v["source_id"] in task.allowed_source_ids
                        ]
                        for path in ("project_run", "quick_report"):
                            row = next(
                                r
                                for r in model_rows
                                if r["task_id"] == task.id and r["path"] == path
                            )
                            row["status"] = "running"
                            row["gold_count"] = len(gold)
                            failures_before = len(projection.failures)
                            write_json(output / "results.json", rows)
                            started = time.perf_counter()
                            try:
                                if path == "project_run":
                                    package = builder.build_context(
                                        ContextBuildRequest(
                                            task_id=work.id,
                                            max_tokens=16000,
                                            reading_format="inline-v1",
                                            retrieval_strategy=strategy,
                                            enable_vector_candidates=model
                                            not in {"none", "current"},
                                        )
                                    )
                                    units = [
                                        [mapping["chunks"][c.chunk_id] for c in b.chunks]
                                        for b in package.knowledge.claim_bundles
                                    ]
                                    artifact = package.model_dump(mode="json")
                                    row["context_tokens"] = package.token_usage.used
                                else:
                                    artifact = query_service.search(
                                        task.question, topic_slugs=scopes, top_k=20
                                    )
                                    units = [
                                        [mapping["chunks"][e["chunk_id"]]]
                                        for e in artifact["evidence"]
                                    ]
                                write_json(
                                    folder / task.id / f"{model}-{strategy}-{path}.json", artifact
                                )
                                if len(projection.failures) > failures_before:
                                    raise RuntimeError(
                                        "Projection failed; degraded retrieval is not scored"
                                    )
                                if any(
                                    s["source_id"] not in task.allowed_source_ids
                                    for u in units
                                    for s in u
                                ):
                                    raise ValueError("Out-of-scope evidence selected")
                                row.update(
                                    status="ok",
                                    metrics=retrieval_metrics(gold, units, universe),
                                    selected=units,
                                    scope_violation=False,
                                )
                            except Exception as exc:
                                row.update(status="failed", metrics={}, **error_details(exc))
                            row["projection_failures"] = projection.failures[failures_before:]
                            row["latency_ms"] = (time.perf_counter() - started) * 1000
                            write_json(output / "results.json", rows)
                    print(f"{version}/{model}/{strategy}: complete", flush=True)
                except Exception as exc:
                    if model not in projections:
                        projection_errors[model] = error_details(exc)
                    for row in model_rows:
                        if row["status"] in {"not_attempted", "running"}:
                            row.update(status="failed", metrics={}, **error_details(exc))
                    write_json(output / "results.json", rows)
                    print(f"{version}/{model}: {type(exc).__name__}", flush=True)
        summary = summarize(rows)
        baseline = (
            [r for r in summary if r["model"] == "current" and r["strategy"] == "legacy"]
            if stage == "embeddings"
            else [
                r
                for r in previous_groups
                if r["model"] == candidate["model"] and r["strategy"] == candidate["strategy"]
            ]
        )
        write_json(
            output / "summary.json",
            {
                "groups": summary,
                "candidate": choose_model(summary) if stage == "embeddings" else candidate,
                "default_changed": False,
                "development_gate": quality_gate(summary, baseline),
                "generation_quality": "not_measured",
                "validation_profile": choose_chunking(summary, baseline, candidate)
                if stage == "chunking"
                else None,
            },
        )
    finally:
        write_json(
            output / "isolation.json",
            {str(p): feedback_trial.digest(p) == d for p, d in protected.items()},
        )
        if any(feedback_trial.digest(p) != d for p, d in protected.items()):
            raise ValueError("Protected data changed")
    return {
        "output": str(output),
        "planned": len(rows),
        "failed": sum(r["status"] != "ok" for r in rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--stage", choices=["embeddings", "chunking"], default="embeddings")
    parser.add_argument("--from-run")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    args = parser.parse_args()
    print(
        json.dumps(
            run(execute=args.execute, stage=args.stage, from_run=args.from_run, device=args.device),
            ensure_ascii=False,
        )
    )
