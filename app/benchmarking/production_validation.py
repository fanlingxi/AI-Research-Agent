"""A08 production freeze replay and opt-in Worker report comparison on fixed dev."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from statistics import mean
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.models import AgentRunCreateRequest
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.benchmarking import feedback_trial
from app.benchmarking.context_neighbors import PRIOR, ReplayPlanner, load_snapshot
from app.benchmarking.live import (
    APPROVAL,
    DATASET,
    ROOT,
    code_fingerprint,
    isolated_settings,
    write_json,
)
from app.benchmarking.live_dataset import approved_dataset
from app.benchmarking.reading_trial import CASES, RecordedClient
from app.benchmarking.retrieval_compare import retrieval_metrics, span_recall
from app.benchmarking.selection_options import sha
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LangChainChatClient, get_llm_client
from app.retrieval.query_planning import QueryPlan
from app.retrieval.reranking import EvidenceReranker, input_digest
from app.worker import KnowledgeWorker

RANKS = ROOT / "data/evaluation/a08-options/20260913T182938Z-99d11a31"


class ArchivedRanker:
    def __init__(self, directory):
        self.directory = directory

    def rank(self, payload):
        request = json.loads((self.directory / "request.json").read_text(encoding="utf-8"))
        expected = json.loads(request["prompt"].split("\n", 1)[1])
        if expected != payload:
            raise ValueError("Recorded ranking candidate text/order or question changed")
        response = json.loads((self.directory / "response.json").read_text(encoding="utf-8"))
        return {
            "status": "ranked",
            "indices": json.loads(response["text"])["indices"],
            "input_sha256": input_digest(payload),
            "model_calls": 0,
            "usage": None,
            "origin": str(self.directory),
            "kind": "exact_input_replay",
        }


def run(execute=False):
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    dev = [t for t in tasks if t.split == "dev"]
    live = Settings()
    client = None
    if execute:
        if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
            raise ValueError("Live calls disabled under offline tests")
        client = get_llm_client(live)
        if not isinstance(client, LangChainChatClient):
            raise ValueError("Configured real model required")
        client = replace(
            client, max_tokens=8192, timeout=120, max_retries=0, thinking_enabled=False
        )
    output = (
        ROOT
        / "data/evaluation/a08-production"
        / (f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
    )
    output.mkdir(parents=True, exist_ok=False)
    write_json(
        output / "config.json",
        {
            "code": code_fingerprint(),
            "execute": execute,
            "dataset_freeze_sha256": approval["freeze_sha256"],
            "ranks": str(RANKS),
            "context_tokens": 16000,
            "run_tokens": 1048576,
            "max_live_calls": 30 if execute else 0,
            "model": client.model if client else None,
            "spending_mode": "observe",
            "output_tokens": 8192,
            "timeout_seconds": 120,
            "sdk_retries": 0,
            "thinking": "disabled",
        },
    )
    feedback_trial.backup_archive(PRIOR / "knowledge.db", output / "replay.db")
    repo = KnowledgeRepository(str(output / "replay.db"))
    mapping = json.loads((PRIOR / "source-map.json").read_text(encoding="utf-8"))["chunks"]
    spans = {s.id: s.model_dump() for s in corpus.spans}
    rows = []
    print(f"Production output: {output}", flush=True)
    for task in dev:
        old = load_snapshot(PRIOR / task.id / "finite-v1/snapshot.json")
        plan = QueryPlan.model_validate(old.retrieval_audit.parameters["planning_attempt"])
        builder = ContextBuilderService(
            repo,
            query_planner=ReplayPlanner(plan),
            evidence_reranker=ArchivedRanker(RANKS / task.id),
        )
        for fmt, mode in [
            ("legacy", "off"),
            ("legacy", "llm-v1"),
            ("research-v1", "llm-v1"),
            ("inline-v1", "llm-v1"),
        ]:
            package = builder.build_context(
                ContextBuildRequest(
                    task_id=old.task.task_id,
                    max_tokens=16000,
                    query_planning="finite-v1",
                    evidence_reranking=mode,
                    reading_format=fmt,
                )
            )
            if mode == "off" and package.knowledge != old.knowledge:
                raise ValueError("Legacy baseline changed")
            units = [
                [mapping[c.chunk_id] for c in b.chunks] for b in package.knowledge.claim_bundles
            ]
            if any(u["source_id"] not in task.allowed_source_ids for group in units for u in group):
                raise ValueError("Out of scope frozen evidence")
            folder = output / task.id / f"{fmt}-{mode}"
            write_json(folder / "snapshot.json", package.model_dump(mode="json"))
            rows.append(
                {
                    "task_id": task.id,
                    "format": fmt,
                    "reranking": mode,
                    "status": "ok",
                    "used": package.token_usage.used,
                    "span_recall": span_recall(
                        [spans[s] for s in task.expectation.required_evidence_span_ids], units
                    ),
                }
            )
        write_json(output / "replay-results.json", rows)
    print("100 production snapshots complete", flush=True)
    results = []
    if execute:
        for case in CASES:
            old = load_snapshot(PRIOR / case / "finite-v1/snapshot.json")
            plan = QueryPlan.model_validate(old.retrieval_audit.parameters["planning_attempt"])
            cache = {}
            for fmt in ["research-v1", "inline-v1"]:
                folder = output / case / f"live-{fmt}"
                folder.mkdir(parents=True)
                feedback_trial.backup_archive(PRIOR / "knowledge.db", folder / "knowledge.db")
                repo = KnowledgeRepository(str(folder / "knowledge.db"))
                settings = isolated_settings(folder, live)
                llm = RecordedClient(client, folder / "generation", cache)
                rank_llm = RecordedClient(replace(client, timeout=90), folder / "ranking", {})
                builder = ContextBuilderService(
                    repo,
                    query_planner=ReplayPlanner(plan),
                    evidence_reranker=EvidenceReranker(rank_llm),
                )
                service = AgentRunService(repo, settings=settings, llm=llm, context_builder=builder)
                worker = KnowledgeWorker(
                    repo,
                    ingestion_service=None,
                    report_service=None,
                    agent_runtime=AgentRuntime(
                        service,
                        checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path),
                    ),
                    lease_seconds=600,
                )
                run = service.create_run(
                    old.project.project_id,
                    old.task.task_id,
                    AgentRunCreateRequest(
                        workflow="research_v2",
                        token_budget=1048576,
                        context_max_tokens=16000,
                        query_planning="finite-v1",
                        evidence_reranking="llm-v1",
                        reading_format=fmt,
                    ),
                )
                row = {"task_id": case, "format": fmt, "run_id": run.id, "status": "queued"}
                results.append(row)
                write_json(output / "live-results.json", results)
                try:
                    if not worker.run_once():
                        raise ValueError("Missing queued job")
                finally:
                    row.update(feedback_trial.archive_run(service, run.id, folder / "archive"))
                    with repo.database.connect() as db:
                        attempts = [
                            dict(r)
                            for r in db.execute(
                                "SELECT * FROM research_generation_attempts WHERE run_id=?",
                                (run.id,),
                            )
                        ]
                    write_json(folder / "attempts.json", attempts)
                    write_json(output / "live-results.json", results)
                print(f"{case}/{fmt}: {row['status']}", flush=True)
    return {"output": str(output), "replay_count": len(rows), "live_results": results}


def refusal_followup(prior, execute=False, *, revised=False):
    """Replay archived dev snapshots through a separately pinned report version."""
    cases = CASES if revised else ("attribution-q12",)
    workflow = "research_v3" if revised else "research_v2"
    if not execute:
        return {"mode": "dry_run", "new_runs": len(cases) * 2,
                "max_model_calls": len(cases) * 8, "workflow": workflow}
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Live calls disabled under offline tests")
    prior = Path(prior).resolve()
    if not prior.is_relative_to((ROOT / "data/evaluation/a08-production").resolve()):
        raise ValueError("Only isolated production validation archives may be replayed")
    _, tasks, approval = approved_dataset(DATASET, APPROVAL)
    if not set(cases) <= {t.id for t in tasks if t.split == "dev"}:
        raise ValueError("Follow-up cases are not approved dev")
    protected = {
        p: feedback_trial.digest(p)
        for case in cases for p in (prior / case).rglob("*") if p.is_file()
    }
    live = Settings()
    client = get_llm_client(live)
    if not isinstance(client, LangChainChatClient):
        raise ValueError("Configured real model required")
    client = replace(client, max_tokens=8192, timeout=120, max_retries=0, thinking_enabled=False)
    output = (
        ROOT
        / "data/evaluation/a08-production"
        / (f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}-{workflow}-followup")
    )
    output.mkdir(parents=True, exist_ok=False)
    write_json(
        output / "config.json",
        {
            "code": code_fingerprint(),
            "parent": str(prior),
            "execute": True,
            "dataset_freeze_sha256": approval["freeze_sha256"],
            "cases": cases,
            "workflow": workflow,
            "purpose": "evidence alignment revision" if revised else "explicit refusal rule",
            "same_snapshots": True,
            "new_ranking_calls": 0,
            "max_live_calls": len(cases) * 8,
            "model": client.model,
            "output_tokens": 8192,
            "timeout_seconds": 120,
            "sdk_retries": 0,
            "thinking": "disabled",
            "spending_mode": "observe",
        },
    )
    cache = {}
    for request_path in (
        p for case in cases
        for p in (prior / case).glob("live-*/generation/call-*/request.json")
    ):
        request = json.loads(request_path.read_text(encoding="utf-8"))
        response_path = request_path.with_name("response.json")
        if "Research input:" in request["prompt"] or not response_path.exists():
            continue
        key = sha(json.dumps([request["system_prompt"], request["prompt"]], ensure_ascii=False))
        cache[key] = json.loads(response_path.read_text(encoding="utf-8"))["text"]
    results = []
    print(f"Follow-up output: {output}", flush=True)
    for case, fmt in product(cases, ("research-v1", "inline-v1")):
        source = prior / case / f"live-{fmt}"
        snapshot = load_snapshot(source / "archive/snapshot.json")
        folder = output / case / f"live-{fmt}"
        folder.mkdir(parents=True)
        feedback_trial.backup_archive(source / "knowledge.db", folder / "knowledge.db")
        repo = KnowledgeRepository(str(folder / "knowledge.db"))
        settings = isolated_settings(folder, live)
        llm = RecordedClient(client, folder / "generation", cache)
        service = AgentRunService(repo, settings=settings, llm=llm)
        worker = KnowledgeWorker(
            repo,
            ingestion_service=None,
            report_service=None,
            agent_runtime=AgentRuntime(
                service, checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path)
            ),
            lease_seconds=600,
        )
        agent_run = service.create_run(
            snapshot.project.project_id,
            snapshot.task.task_id,
            AgentRunCreateRequest(
                workflow=workflow,
                token_budget=1048576,
                context_snapshot_id=snapshot.snapshot_id,
            ),
        )
        parent = json.loads((source / "archive/run.json").read_text(encoding="utf-8"))
        row = {"task_id": case, "format": fmt, "run_id": agent_run.id, "status": "queued",
               "parent_run_id": parent["id"], "workflow": workflow,
               "human_recheck": "pending"}
        results.append(row)
        write_json(output / "live-results.json", results)
        try:
            if not worker.run_once():
                raise ValueError("Missing queued job")
        except Exception as exc:
            row["execution_error_type"] = type(exc).__name__
        finally:
            row.update(feedback_trial.archive_run(service, agent_run.id, folder / "archive"))
            if row["snapshot_sha256"] != snapshot.package_sha256:
                raise ValueError("Follow-up snapshot changed")
            with repo.database.connect() as db:
                attempts = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM research_generation_attempts WHERE run_id=?", (agent_run.id,)
                    )
                ]
            write_json(folder / "attempts.json", attempts)
            write_json(output / "live-results.json", results)
            unchanged = {str(p): feedback_trial.digest(p) == digest
                         for p, digest in protected.items()}
            write_json(output / "isolation.json", {"protected_unchanged": unchanged})
            if not all(unchanged.values()):
                raise ValueError("Original evaluation archive changed")
        print(f"{case}/{fmt}: {row['status']}", flush=True)
    return {"output": str(output), "live_results": results}


def neighbor_replay(execute=False):
    """Measure existing neighbor groups after reranking; no new model calls or defaults."""
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    dev = [t for t in tasks if t.split == "dev"]
    if not execute:
        return {"mode": "dry_run", "snapshots": len(dev) * 2, "new_model_calls": 0}
    output = ROOT / "data/evaluation/a08-production" / (
        f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}-neighbor-replay"
    )
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", {
        "code": code_fingerprint(), "dataset_freeze_sha256": approval["freeze_sha256"],
        "prior": str(PRIOR), "ranks": str(RANKS), "new_model_calls": 0,
        "reading_format": "inline-v1", "context_tokens": 16000,
        "gate": {"recall_min_gain": .05, "mrr_min_delta": -.02},
        "holdout": "not_used", "default_changed": False,
    })
    protected = {p: feedback_trial.digest(p) for p in (PRIOR / "knowledge.db",)}
    feedback_trial.backup_archive(PRIOR / "knowledge.db", output / "knowledge.db")
    repo = KnowledgeRepository(str(output / "knowledge.db"))
    mapping = json.loads((PRIOR / "source-map.json").read_text(encoding="utf-8"))["chunks"]
    spans = {s.id: s.model_dump() for s in corpus.spans}
    rows, queries = [], []
    print(f"Neighbor replay output: {output}", flush=True)
    for task in dev:
        old = load_snapshot(PRIOR / task.id / "finite-v1/snapshot.json")
        plan = QueryPlan.model_validate(old.retrieval_audit.parameters["planning_attempt"])
        queries.append({"task_id": task.id, "question": plan.queries[0],
                        "plan": plan.model_dump(), "human_review": "pending"})
        builder = ContextBuilderService(repo, query_planner=ReplayPlanner(plan),
                                        evidence_reranker=ArchivedRanker(RANKS / task.id))
        for neighbors in ("off", "adjacent-v1"):
            row = {"task_id": task.id, "neighbors": neighbors, "status": "pending"}
            rows.append(row)
            write_json(output / "results.json", rows)
            units = []
            try:
                package = builder.build_context(ContextBuildRequest(
                    task_id=old.task.task_id, max_tokens=16000, query_planning="finite-v1",
                    evidence_reranking="llm-v1", reading_format="inline-v1",
                    context_neighbors=neighbors,
                ))
                if package.task != old.task or package.project != old.project:
                    raise ValueError("Task or project changed")
                if package.token_usage.used > 16000:
                    raise ValueError("Snapshot exceeds budget")
                selected = {b.claim.claim_id for b in package.knowledge.claim_bundles}
                groups = package.retrieval_audit.parameters.get("context_neighbors", {}).get(
                    "groups", []
                )
                for group in groups:
                    members = set(group["claim_ids"])
                    if (members & selected and not members <= selected) or (
                        group["selected"] and not set(group["context_claim_ids"]) <= selected
                    ):
                        raise ValueError("Incomplete selected neighbor group")
                for bundle in package.knowledge.claim_bundles:
                    for chunk in bundle.chunks:
                        source = mapping[chunk.chunk_id]
                        if (source["source_id"] not in task.allowed_source_ids
                                or source["claim_id"] != bundle.claim.claim_id
                                or source["text_sha256"] != chunk.content_sha256):
                            raise ValueError("Source scope, identity or content mismatch")
                # Only score after production selection has finished.
                units = [[mapping[c.chunk_id] for c in b.chunks]
                         for b in package.knowledge.claim_bundles]
                row.update(status="ok", used=package.token_usage.used)
                write_json(output / task.id / neighbors / "snapshot.json",
                           package.model_dump(mode="json"))
            except Exception as exc:
                row.update(status="failed", error_type=type(exc).__name__)
            gold = [spans[s] for s in task.expectation.required_evidence_span_ids]
            universe = [[m] for m in mapping.values() if m["source_id"] in task.allowed_source_ids]
            row.update(span_recall=span_recall(gold, units),
                       mrr=retrieval_metrics(gold, units, universe)["mrr@10"])
            write_json(output / "results.json", rows)
        print(f"{task.id}: {[r['status'] for r in rows[-2:]]}", flush=True)
    summary = {}
    for neighbors in ("off", "adjacent-v1"):
        subset = [r for r in rows if r["neighbors"] == neighbors]
        summary[neighbors] = {
            "attempted": len(subset), "failed": sum(r["status"] != "ok" for r in subset),
            **{key: mean(r[key] for r in subset if r[key] is not None)
               for key in ("span_recall", "mrr")},
        }
    summary["gate_passed"] = (
        summary["adjacent-v1"]["span_recall"] - summary["off"]["span_recall"] >= .05
        and summary["adjacent-v1"]["mrr"] - summary["off"]["mrr"] >= -.02
        and all(r["status"] == "ok" for r in rows)
    )
    write_json(output / "summary.json", summary)
    write_json(output / "query-review.json", queries)
    unchanged = {str(p): feedback_trial.digest(p) == digest for p, digest in protected.items()}
    write_json(output / "isolation.json", {"protected_unchanged": unchanged})
    if not all(unchanged.values()):
        raise ValueError("Original selection archive changed")
    return {"output": str(output), **summary}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--refusal-from")
    group.add_argument("--revision-from")
    group.add_argument("--neighbor-replay", action="store_true")
    args = parser.parse_args()
    result = (
        refusal_followup(args.refusal_from or args.revision_from, args.execute,
                         revised=bool(args.revision_from))
        if args.refusal_from or args.revision_from
        else neighbor_replay(args.execute) if args.neighbor_replay else run(args.execute)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
