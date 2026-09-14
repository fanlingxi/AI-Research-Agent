"""Offline A08 adjacency ablation replaying immutable prior query plans."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from statistics import mean
from uuid import uuid4

from app.benchmarking.feedback_trial import backup_archive
from app.benchmarking.live import APPROVAL, DATASET, ROOT, code_fingerprint, write_json
from app.benchmarking.live_dataset import approved_dataset
from app.benchmarking.retrieval_compare import retrieval_metrics, span_recall
from app.context.models import ContextBuildRequest, ContextPackage, canonical_package_sha256
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.retrieval.query_planning import QueryPlan

PRIOR = ROOT / "data/evaluation/a08/20260913T174426Z-462bc330"
MANIFEST = ROOT / "docs/improvement/a08/evidence.json"
BASES = ("off", "finite-v1")


class ReplayPlanner:
    def __init__(self, plan: QueryPlan):
        self.plan_record = plan

    def plan(self, question):
        if hashlib.sha256(question.encode()).hexdigest() != self.plan_record.question_sha256:
            raise ValueError("Recorded query plan does not match this task")
        # This is a query replay, not a fresh model attempt or usage settlement.
        return self.plan_record.model_copy(deep=True, update={"model_calls": 0, "usage": None})


def load_snapshot(path):
    package = ContextPackage.model_validate_json(path.read_bytes())
    if canonical_package_sha256(package) != package.package_sha256:
        raise ValueError("Prior snapshot integrity check failed")
    return package


def summary(rows):
    strategies, gates = {}, {}
    for base in BASES:
        for neighbors in ("off", "adjacent-v1"):
            subset = [r for r in rows if r["base"] == base and r["neighbors"] == neighbors]
            scores = {m: [r[m] for r in subset if r[m] is not None]
                      for m in ("snapshot_span_recall", "recall@10", "mrr@10", "ndcg@10")}
            strategies[f"{base}/{neighbors}"] = {
                "attempted": len(subset), "failed": sum(r["status"] != "ok" for r in subset),
                "macro": {m: mean(v) if v else None for m, v in scores.items()},
                "gold_tasks": len(scores["snapshot_span_recall"]),
                "expanded_groups": sum(r.get("expanded_groups", 0) for r in subset),
                "selected_expanded_groups": sum(r.get("selected_expanded_groups", 0)
                                                for r in subset),
            }
        old, new = strategies[f"{base}/off"], strategies[f"{base}/adjacent-v1"]
        gates[base] = {
            "snapshot_recall_gain": ((new["macro"]["snapshot_span_recall"] or 0)
                                     - (old["macro"]["snapshot_span_recall"] or 0)) >= .05,
            "mrr_nonregression": ((new["macro"]["mrr@10"] or 0)
                                  - (old["macro"]["mrr@10"] or 0)) >= -.02,
            "no_failures": old["attempted"] == new["attempted"] == 25
                           and old["failed"] == new["failed"] == 0,
        }
    return {"strategies": strategies, "gates": gates, "new_model_calls": 0,
            "new_cost_cny": 0, "generation_quality": "not_measured", "holdout": "not_run",
            "default_strategy": "legacy", "default_neighbors": "off"}


def run(*, execute=False):
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    dev = [t for t in tasks if t.split == "dev"]
    if not execute:
        return {"mode": "dry_run", "dev_tasks": len(dev), "builds": len(dev) * 4,
                "new_model_calls": 0, "prior": str(PRIOR)}
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if hashlib.sha256((PRIOR / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Prior evaluation archive changed")
    prior_config = json.loads((PRIOR / "config.json").read_text(encoding="utf-8"))
    if prior_config["dataset_freeze_sha256"] != approval["freeze_sha256"] or len(dev) != 25:
        raise ValueError("This trial requires the same approved 25 dev tasks")
    output = ROOT / "data/evaluation/a08-neighbors" / (
        f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", {
        "schema": "a08-adjacency-v1", "code": code_fingerprint(),
        "dataset_freeze_sha256": approval["freeze_sha256"], "prior": str(PRIOR),
        "prior_manifest": manifest, "context_max_tokens": 16000, "max_anchors": 10,
        "neighbor_radius": 1, "recursive": False, "new_model_calls": 0,
        "new_cost_cny": 0, "historical_model_attempts": 25, "historical_unknown_usage": 2,
        "gate": {"snapshot_recall_gain": .05, "mrr_min_delta": -.02},
    })
    backup_archive(PRIOR / "knowledge.db", output / "knowledge.db")
    repository = KnowledgeRepository(str(output / "knowledge.db"))
    mapping = json.loads((PRIOR / "source-map.json").read_text(encoding="utf-8"))
    spans = {s.id: s.model_dump() for s in corpus.spans}
    rows = []
    print(f"Adjacency output: {output}", flush=True)
    for task in dev:
        for base in BASES:
            original = load_snapshot(PRIOR / task.id / base / "snapshot.json")
            record = original.retrieval_audit.parameters.get("planning_attempt")
            planner = ReplayPlanner(QueryPlan.model_validate(record)) if record else None
            builder = ContextBuilderService(repository, query_planner=planner)
            for neighbors in ("off", "adjacent-v1"):
                row = {"task_id": task.id, "base": base, "neighbors": neighbors,
                       "status": "pending", "new_model_calls": 0}
                directory = output / task.id / base / neighbors
                write_json(directory / "result.json", row)
                units, ranking = [], []
                try:
                    if base == "finite-v1" and planner is None:
                        raise ValueError("Finite replay requires a recorded plan")
                    package = builder.build_context(ContextBuildRequest(
                        task_id=original.task.task_id, project_id=original.project.project_id,
                        max_tokens=16000, query_planning=base, context_neighbors=neighbors))
                    if (package.task != original.task or package.project != original.project
                            or (neighbors == "off" and package.knowledge != original.knowledge)):
                        raise ValueError("Baseline task, project or evidence changed")
                    selected = {b.claim.claim_id for b in package.knowledge.claim_bundles}
                    groups = package.retrieval_audit.parameters.get("context_neighbors", {}).get(
                        "groups", [])
                    for group in groups:
                        members = set(group["claim_ids"])
                        if ((members & selected and not members <= selected)
                                or (group["selected"] and not set(group["context_claim_ids"])
                                    <= selected)):
                            raise ValueError("A neighbor group was only partially selected")
                    if package.token_usage.used > 16000:
                        raise ValueError("Snapshot budget exceeded")
                    for bundle in package.knowledge.claim_bundles:
                        for chunk in bundle.chunks:
                            source = mapping["chunks"][chunk.chunk_id]
                            if (source["source_id"] not in task.allowed_source_ids
                                    or source["claim_id"] != bundle.claim.claim_id
                                    or source["text_sha256"] != chunk.content_sha256):
                                raise ValueError("Selected source scope, claim or content mismatch")
                    units = [[mapping["chunks"][c.chunk_id] for c in b.chunks]
                             for b in package.knowledge.claim_bundles]
                    ranking = [[mapping["chunks"][s.chunk_id] for s in item.sources]
                               for item in sorted((s for s in package.retrieval_audit.selections
                                                   if s.rank), key=lambda s: s.rank)]
                    row.update(status="ok", context_used=package.token_usage.used,
                               selected_claims=len(selected),
                               expanded_groups=sum(len(g["context_claim_ids"]) > 1 for g in groups),
                                selected_expanded_groups=sum(
                                   g["selected"] and len(g["context_claim_ids"]) > 1
                                   for g in groups))
                    write_json(directory / "snapshot.json", package.model_dump(mode="json"))
                except Exception as exc:
                    row.update(status="failed", error_type=type(exc).__name__, error=str(exc))
                    units, ranking = [], []
                # Expected spans are used only after the production selection has ended.
                gold = [spans[s] for s in task.expectation.required_evidence_span_ids]
                universe = [[v] for v in mapping["chunks"].values()
                            if v["source_id"] in task.allowed_source_ids]
                row.update(retrieval_metrics(gold, ranking, universe))
                row["snapshot_span_recall"] = span_recall(gold, units)
                write_json(directory / "result.json", row)
                rows.append(row)
                write_json(output / "results.json", rows)
                write_json(output / "summary.json", summary(rows))
        print(f"{task.id}: {','.join(r['status'] for r in rows[-4:])}", flush=True)
    return {"output": str(output), **summary(rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    print(json.dumps(run(execute=parser.parse_args().execute), ensure_ascii=False, indent=2))
