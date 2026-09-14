"""Frozen paper retrieval comparison; no model, network or production database access.

python -m app.benchmarking.retrieval_compare --split dev
python -m app.benchmarking.retrieval_compare --split holdout --dev-run <dev output>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from uuid import uuid4

from app.benchmarking.live import (
    APPROVAL,
    DATASET,
    ROOT,
    code_fingerprint,
    isolated_settings,
    seeded_repository,
    write_json,
)
from app.benchmarking.live_dataset import approved_dataset, task_input
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.retrieval import QdrantContextCandidateRetriever
from app.context.service import ContextBuilderService
from app.knowledge.query import KnowledgeQueryService, _latin_query_terms
from app.knowledge.schemas import ChunkSearchHit
from app.retrieval.contracts import SourceIdentity
from app.retrieval.embeddings import HashEmbeddingProvider
from app.retrieval.hybrid import PARAMETERS

STRATEGIES = ("legacy", "bm25-v1", "hybrid-v1")
KS = (5, 10, 20)
OUTPUT_ROOT = ROOT / "data/evaluation/a04"


class LocalHashProjection:
    """Cosine over the existing hash provider; an evaluation projection, not neural retrieval."""

    def __init__(self, repository):
        self.provider = HashEmbeddingProvider()
        with repository.database.connect() as connection:
            rows = connection.execute("""
                SELECT c.id, c.document_id, c.content, c.content_sha256 AS chunk_sha,
                       d.source_id, d.content_sha256 AS document_sha,
                       s.version, s.content_sha256 AS source_sha
                FROM chunks c JOIN documents d ON d.id = c.document_id
                JOIN sources s ON s.id = d.source_id ORDER BY c.id
            """).fetchall()
        self.entries = []
        for row in rows:
            vector = self.provider.embed_query(row["content"])
            identity = SourceIdentity(
                source_id=row["source_id"],
                source_version=row["version"],
                source_sha256=row["source_sha"],
                document_id=row["document_id"],
                document_sha256=row["document_sha"],
                chunk_id=row["id"],
                chunk_sha256=row["chunk_sha"],
            )
            self.entries.append((identity, {i: value for i, value in enumerate(vector) if value}))

    def search(self, query, *, allowed_paper_ids, top_k):
        vector = self.provider.embed_query(_latin_query_terms(query) or query)
        scored = [
            ChunkSearchHit(
                chunk_id=identity.chunk_id,
                score=sum(vector[i] * value for i, value in sparse.items()),
                source_identity=identity,
            )
            for identity, sparse in self.entries
            if identity.document_id in allowed_paper_ids
        ]
        return sorted(scored, key=lambda item: (-item.score, item.chunk_id))[
            : min(max(top_k * 6, top_k), 100)
        ]


class NoGraphProjection:
    def search(self, *args, **kwargs):
        return []


def overlaps(left, right):
    return left["source_id"] == right["source_id"] and max(left["start"], right["start"]) < min(
        left["end"], right["end"]
    )


def span_recall(gold, units):
    if not gold:
        return None
    spans = [span for unit in units for span in unit]
    covered = 0
    for target in gold:
        end = target["start"]
        for span in sorted(spans, key=lambda item: item["start"]):
            if span["source_id"] == target["source_id"] and span["start"] <= end:
                end = max(end, span["end"])
        covered += end >= target["end"]
    return covered / len(gold)


def retrieval_metrics(gold, units, universe):
    """Binary passage relevance for ordering; full interval union for span recall."""
    if not gold:
        return {f"{metric}@{k}": None for k in KS for metric in ("hit", "recall", "mrr", "ndcg")}
    relevant_total = sum(
        any(overlaps(span, target) for span in unit for target in gold) for unit in universe
    )
    result = {}
    for k in KS:
        relevance = [
            any(overlaps(span, target) for span in unit for target in gold) for unit in units[:k]
        ]
        dcg = sum(1 / math.log2(rank + 1) for rank, hit in enumerate(relevance, 1) if hit)
        ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, relevant_total) + 1))
        result.update(
            {
                f"hit@{k}": float(any(relevance)),
                f"recall@{k}": span_recall(gold, units[:k]),
                f"mrr@{k}": next((1 / rank for rank, hit in enumerate(relevance, 1) if hit), 0),
                f"ndcg@{k}": dcg / ideal if ideal else 0,
            }
        )
    return result


def summarize(results):
    summary = {}
    for path in ("project_run", "quick_report"):
        summary[path] = {}
        for strategy in STRATEGIES:
            rows = [r for r in results if r["path"] == path and r["strategy"] == strategy]
            times = sorted(r["latency_ms"] for r in rows)
            summary[path][strategy] = {
                "attempted": len(rows),
                "failed": sum(r["status"] != "ok" for r in rows),
                "no_gold": sum(not r["gold_count"] for r in rows),
                "scope_violations": sum(r["scope_violation"] for r in rows),
                "macro": {
                    key: mean(values) if values else None
                    for key in (
                        f"{metric}@{k}" for k in KS for metric in ("hit", "recall", "mrr", "ndcg")
                    )
                    if (
                        values := [r["metrics"][key] for r in rows if r["metrics"][key] is not None]
                    )
                    is not None
                },
                "p95_latency_ms": times[max(0, math.ceil(0.95 * len(times)) - 1)]
                if times
                else None,
                "model_calls": 0,
                "cost_cny": 0,
            }
    return summary


def gate(summary):
    results = {}
    for path, strategies in summary.items():
        old, new = strategies["legacy"], strategies["hybrid-v1"]
        metrics, baseline = new["macro"], old["macro"]
        results[path] = {
            "recall": (metrics["recall@10"] or 0) - (baseline["recall@10"] or 0) >= 0.05,
            "mrr": (metrics["mrr@10"] or 0) - (baseline["mrr@10"] or 0) >= -0.02,
            "ndcg": (metrics["ndcg@10"] or 0) - (baseline["ndcg@10"] or 0) >= -0.02,
            "failures": new["failed"] == 0 and new["scope_violations"] == 0,
            "latency": new["p95_latency_ms"] <= min(5000, 3 * old["p95_latency_ms"]),
        }
    return results


def run(split: str, dev_run: Path | None = None):
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    code = code_fingerprint()
    config = {
        "schema": "a04-retrieval-comparison-v1",
        "split": split,
        "code": code,
        "dataset_freeze_sha256": approval["freeze_sha256"],
        "strategies": STRATEGIES,
        "parameters": PARAMETERS,
        "context_max_tokens": 16000,
        "report_top_k": 20,
        "embedding": "local-hash-384-cosine",
        "graph": "disabled",
        "model_calls": 0,
        "generation_quality": "not_measured",
        "cost_cny": 0,
        "gate": {
            "recall_gain": 0.05,
            "mrr_ndcg_max_drop": 0.02,
            "p95_ratio_max": 3,
            "p95_ms_max": 5000,
        },
        "limitations": [
            "Hash vectors are not neural semantic vectors or Qdrant timings.",
            "Required spans are retrieval labels, not generated-answer truth.",
            "Correlated paper families are not independent samples.",
        ],
    }
    if split == "holdout":
        if dev_run is None:
            raise ValueError("Holdout requires the frozen dev run")
        previous = json.loads((dev_run / "config.json").read_text(encoding="utf-8"))
        previous_summary = json.loads((dev_run / "summary.json").read_text(encoding="utf-8"))
        if previous["split"] != "dev" or previous_summary["attempted"] != 150:
            raise ValueError(
                "Holdout requires a complete 25-task, two-path, three-strategy dev run"
            )
        for field in (
            "dataset_freeze_sha256",
            "parameters",
            "embedding",
            "gate",
            "context_max_tokens",
            "report_top_k",
        ):
            if config[field] != previous[field]:
                raise ValueError(f"Configuration changed after dev: {field}")
        if code["source_sha256"] != previous["code"]["source_sha256"]:
            raise ValueError("Code changed after dev; freeze a new dev comparison")
        # The claim lives with the dev record, outside individual attempt outputs.
        with (dev_run / "holdout-claimed.json").open("x", encoding="utf-8") as stream:
            json.dump({"claimed_at": datetime.now(UTC).isoformat()}, stream)
        config["dev_config_sha256"] = hashlib.sha256(
            (dev_run / "config.json").read_bytes()
        ).hexdigest()
        config["dev_gate"] = previous_summary["gate"]
    output = OUTPUT_ROOT / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{split}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", config)
    print(f"Comparison output: {output}", flush=True)
    results = []
    write_json(output / "summary.json", {"attempted": 0, "status": "preparing"})
    try:
        repository, mapping = seeded_repository(output, corpus, approval)
        settings = isolated_settings(output, Settings(_env_file=None, llm_provider="mock"))
        setup = time.perf_counter()
        projection = LocalHashProjection(repository)
        config["projection_build_ms"] = (time.perf_counter() - setup) * 1000
        write_json(output / "config.json", config)
        write_json(output / "source-map.json", mapping)
        context = ContextBuilderService(
            repository,
            vector_retriever=QdrantContextCandidateRetriever(projection),
        )
        spans = {span.id: span.model_dump() for span in corpus.spans}
        for task in (t for t in tasks if t.split == split):
            inputs = task_input(task)
            memory = repository.memory_repository
            project = memory.create_project(
                name=f"A04 {task.id}",
                goal="仅依据授权论文完成研究任务。",
                domain="research",
                metadata={"evaluation_only": True},
            )
            scopes = [mapping["sources"][sid]["scope"] for sid in inputs["allowed_source_ids"]]
            memory.replace_project_knowledge_scopes(
                project.id,
                scopes,
                expected_project_revision=project.revision,
            )
            work = memory.create_workspace_task(
                project_id=project.id,
                title=inputs["question"][:200],
                goal=inputs["question"],
                priority="high",
                metadata={"expected_output": "\n".join(inputs["report_requirements"])},
            )
            # Labels are not supplied to the production retrieval services.
            gold = [spans[key] for key in task.expectation.required_evidence_span_ids]
            universe = [
                [item]
                for item in mapping["chunks"].values()
                if item["source_id"] in inputs["allowed_source_ids"]
            ]
            for path in ("project_run", "quick_report"):
                for strategy in STRATEGIES:
                    started = time.perf_counter()
                    record = dict(
                        task_id=task.id,
                        split=split,
                        path=path,
                        strategy=strategy,
                        status="running",
                        gold_count=len(gold),
                        scope_violation=False,
                        model_calls=0,
                        cost_cny=0,
                        selected=[],
                    )
                    artifact = output / task.id / path / strategy
                    write_json(artifact / "result.json", record)
                    units = []
                    try:
                        if path == "project_run":
                            package = context.build_context(
                                ContextBuildRequest(
                                    task_id=work.id,
                                    project_id=project.id,
                                    max_tokens=16000,
                                    enable_vector_candidates=strategy != "bm25-v1",
                                    enable_graph_candidates=False,
                                    retrieval_strategy=strategy,
                                )
                            )
                            snapshot = package.model_dump(mode="json")
                            units = [
                                [mapping["chunks"][item.chunk_id] for item in bundle.chunks]
                                for bundle in package.knowledge.claim_bundles
                            ]
                            audit = snapshot["retrieval_audit"]
                        else:
                            query = KnowledgeQueryService(
                                repository,
                                chunk_search=projection,
                                graph_search=NoGraphProjection(),
                                settings=settings.model_copy(
                                    update={"report_retrieval_strategy": strategy}
                                ),
                            )
                            snapshot = query.search(
                                inputs["question"], topic_slugs=scopes, top_k=20
                            )
                            units = [
                                [mapping["chunks"][item["chunk_id"]]]
                                for item in snapshot["evidence"]
                            ]
                            audit = snapshot["retrieval_diagnostics"]["retrieval_audit"]
                        record["latency_ms"] = (time.perf_counter() - started) * 1000
                        write_json(artifact / "retrieval.json", snapshot)
                        record["selected"] = units
                        record["scope_violation"] = any(
                            item["source_id"] not in inputs["allowed_source_ids"]
                            for unit in units
                            for item in unit
                        )
                        if record["scope_violation"]:
                            raise ValueError("Retrieval crossed allowed paper scope")
                        record["rejected_candidates"] = sum(
                            c["status"] == "rejected" for c in audit["candidates"]
                        )
                        record["status"] = "ok"
                    except Exception as exc:
                        record.update(
                            status="failed", error_type=type(exc).__name__, error=str(exc)
                        )
                        units = []
                    record.setdefault("latency_ms", (time.perf_counter() - started) * 1000)
                    record["metrics"] = retrieval_metrics(gold, units, universe)
                    record["budget_span_recall"] = span_recall(gold, units)
                    write_json(artifact / "result.json", record)
                    results.append(record)
            write_json(output / "results.json", results)
            print(
                f"{task.id}: {sum(r['status'] == 'ok' for r in results[-6:])}/6 retrieved",
                flush=True,
            )
    finally:
        write_json(output / "results.json", results)
        aggregate = summarize(results) if results else {}
        complete = len(results) == (25 if split == "dev" else 15) * 6
        checks = gate(aggregate) if complete else {}
        write_json(
            output / "summary.json",
            {
                "attempted": len(results),
                "complete": complete,
                "aggregate": aggregate,
                "gate": checks,
                "default_decision": "retain_legacy",
                "candidate_gate_passed": complete
                and all(all(row.values()) for row in checks.values()),
                "model_calls": 0,
                "cost_cny": 0,
            },
        )
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--dev-run", type=Path)
    args = parser.parse_args()
    output = run(args.split, args.dev_run)
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    raise SystemExit(0 if summary["complete"] else 2)


if __name__ == "__main__":
    main()
