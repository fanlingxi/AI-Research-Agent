"""Explicit A08 dev-only comparison; model calls require --execute.

Use a new copy of the sealed evaluation seed, the production ContextBuilder and
the configured first-party LLM port. Gold is read only for post-selection scoring.
No production knowledge, holdout tuning, report generation or background runner.
"""

from __future__ import annotations

import argparse
import json
import os
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
    seeded_repository,
    write_json,
)
from app.benchmarking.live_dataset import approved_dataset, task_input
from app.benchmarking.retrieval_compare import retrieval_metrics, span_recall
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.llms.provider import MockLLMClient, get_llm_client
from app.retrieval.query_planning import QueryPlanner

MODES = ("off", "task-v1", "finite-v1")


class RecordingPlanner(QueryPlanner):
    directory: Path

    def plan(self, question):
        # A killed/uncertain attempt remains visible even without a final snapshot.
        write_json(self.directory / "planning.json", {"status": "pending", "usage": None})
        started = time.perf_counter()
        result = super().plan(question)
        write_json(self.directory / "planning.json", {
            **result.model_dump(mode="json"), "latency_seconds": time.perf_counter() - started,
            "billed_cost_cny": None,
        })
        return result


def summarize(rows):
    summary = {}
    for mode in MODES:
        subset = [r for r in rows if r["mode"] == mode]
        metric_values = {metric: [r["metrics"][metric] for r in subset
                                 if r["metrics"][metric] is not None]
                         for metric in ("recall@10", "mrr@10", "ndcg@10")}
        summary[mode] = {
            "attempted": len(subset), "failed": sum(r["status"] != "ok" for r in subset),
            "planner_fallbacks": sum(r.get("planning_status") == "fallback" for r in subset),
            "scope_violations": sum(r["scope_violation"] for r in subset),
            "macro": {m: mean(v) if v else None for m, v in metric_values.items()},
            "snapshot_span_recall": mean(v) if (v := [
                r["snapshot_span_recall"] for r in subset
                if r["snapshot_span_recall"] is not None]) else None,
            "model_calls": sum(r.get("model_calls", 0) for r in subset),
            "unknown_usage_calls": sum(r.get("model_calls", 0) > 0 and r.get("usage") is None
                                       for r in subset),
            "input_tokens": sum((r.get("usage") or {}).get("input_tokens", 0) for r in subset),
            "output_tokens": sum((r.get("usage") or {}).get("output_tokens", 0) for r in subset),
            "cost_cny": None if mode == "finite-v1" else 0,
        }
    old, new = summary["off"], summary["finite-v1"]
    gate = {
        "recall_gain": (new["macro"]["recall@10"] or 0)
                       - (old["macro"]["recall@10"] or 0) >= .05,
        "mrr_nonregression": (new["macro"]["mrr@10"] or 0)
                             - (old["macro"]["mrr@10"] or 0) >= -.02,
        "no_failures_or_scope_violations": new["failed"] == 0 and new["scope_violations"] == 0,
        "planner_available": new["attempted"] > 0 and new["planner_fallbacks"] == 0,
    }
    return {"strategies": summary, "gate": gate, "dev_gate_passed": all(gate.values()),
            "default_strategy": "legacy", "generation_quality": "not_measured",
            "holdout": "not_run", "independent_generalization": "not_established"}


def run(*, execute=False):
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    dev = [t for t in tasks if t.split == "dev"]
    if len(dev) > 25:
        raise ValueError("This preregistered trial allows at most 25 dev questions")
    if not execute:
        return {"mode": "dry_run", "tasks": [t.id for t in dev], "max_model_calls": len(dev),
                "strategies": MODES, "context_tokens": 16000, "production_writes": False}
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Live query planning is disabled in offline tests")
    client = get_llm_client(Settings())
    if isinstance(client, MockLLMClient):
        raise ValueError("A configured real model is required for --execute")
    output = ROOT / "data/evaluation/a08" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    config = {"schema": "a08-finite-query-v1", "code": code_fingerprint(),
              "dataset_freeze_sha256": approval["freeze_sha256"], "split": "dev",
              "strategies": MODES, "max_calls": len(dev), "context_tokens": 16000,
              "provider": client.provider_name, "model": client.model,
              "query_output_tokens": 1024, "query_timeout_seconds": 60, "sdk_retries": 0,
              "vector": "disabled", "graph": "disabled", "spending_mode": "observe",
              "cost_cny": None, "gate": {"recall_gain": .05, "mrr_min_delta": -.02}}
    write_json(output / "config.json", config)
    print(f"A08 output: {output}", flush=True)
    repository, mapping = seeded_repository(output, corpus, approval)
    write_json(output / "source-map.json", mapping)
    planner = RecordingPlanner(client)
    builder = ContextBuilderService(repository, query_planner=planner)
    spans = {span.id: span.model_dump() for span in corpus.spans}
    rows = []
    for task in dev:
        inputs = task_input(task)
        memory = repository.memory_repository
        project = memory.create_project(name=f"A08 {task.id}", goal="仅依据授权论文完成研究任务。",
                                        domain="research", metadata={"evaluation_only": True})
        memory.replace_project_knowledge_scopes(
            project.id, [mapping["sources"][s]["scope"] for s in inputs["allowed_source_ids"]],
            expected_project_revision=project.revision)
        work = memory.create_workspace_task(
            project_id=project.id, title=inputs["question"][:200], goal=inputs["question"],
            priority="high", metadata={"expected_output": "\n".join(inputs["report_requirements"])})
        gold = [spans[s] for s in task.expectation.required_evidence_span_ids]
        universe = [[item] for item in mapping["chunks"].values()
                    if item["source_id"] in inputs["allowed_source_ids"]]
        for mode in MODES:
            directory = output / task.id / mode
            planner.directory = directory
            row = {"task_id": task.id, "mode": mode, "status": "pending",
                   "scope_violation": False, "model_calls": 0, "usage": None}
            write_json(directory / "result.json", row)
            units, ranked_units = [], []
            started = time.perf_counter()
            try:
                package = builder.build_context(ContextBuildRequest(
                    task_id=work.id, max_tokens=16000, query_planning=mode))
                write_json(directory / "snapshot.json", package.model_dump(mode="json"))
                units = [[mapping["chunks"][c.chunk_id] for c in b.chunks]
                         for b in package.knowledge.claim_bundles]
                audit = package.retrieval_audit
                ranked_units = [[mapping["chunks"][s.chunk_id] for s in item.sources]
                                for item in sorted((s for s in audit.selections if s.rank),
                                                   key=lambda s: s.rank)]
                row["scope_violation"] = any(s["source_id"] not in inputs["allowed_source_ids"]
                                             for unit in ranked_units for s in unit)
                if row["scope_violation"] or package.token_usage.used > 16000:
                    raise ValueError("Scope or snapshot budget violation")
                plan = audit.parameters.get("planning_attempt") or audit.parameters.get(
                    "query_planning", {})
                row.update(status="ok", planning_status=plan.get("status"),
                           model_calls=plan.get("model_calls", 0), usage=plan.get("usage"),
                           context_used=package.token_usage.used,
                           selected_claims=len(package.knowledge.claim_bundles))
            except Exception as exc:
                row.update(status="failed", error_type=type(exc).__name__)
                units, ranked_units = [], []
                # A snapshot failure must not erase an earlier planning call.
                if (directory / "planning.json").exists():
                    call = json.loads((directory / "planning.json").read_text(encoding="utf-8"))
                    row.update(model_calls=call.get("model_calls", 1), usage=call.get("usage"))
            row.update(latency_seconds=time.perf_counter() - started,
                       metrics=retrieval_metrics(gold, ranked_units, universe),
                       snapshot_span_recall=span_recall(gold, units))
            write_json(directory / "result.json", row)
            rows.append(row)
            write_json(output / "results.json", rows)
            write_json(output / "summary.json", summarize(rows))
        print(f"{task.id}: {rows[-1]['status']}, planning={rows[-1].get('planning_status')}",
              flush=True)
    return {"output": str(output), **summarize(rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    print(json.dumps(run(execute=parser.parse_args().execute), ensure_ascii=False, indent=2))
