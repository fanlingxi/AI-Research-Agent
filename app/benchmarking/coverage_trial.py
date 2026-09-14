"""Explicit three-arm A08 diagnosis, preserving old snapshots and all attempts."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.models import AgentRunCreateRequest
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.benchmarking import feedback_trial
from app.benchmarking.embedding_trial import error_details, write_json
from app.benchmarking.live import ROOT, code_fingerprint, isolated_settings
from app.benchmarking.reading_trial import RecordedClient
from app.benchmarking.selection_options import sha
from app.config.settings import Settings
from app.context.models import ContextBuildRequest, ContextPackage
from app.context.retrieval import QdrantContextCandidateRetriever
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LangChainChatClient, get_llm_client
from app.retrieval.coverage import CoreCoverageReranker, CoverageReranker
from app.retrieval.neural_embeddings import LocalEmbeddingProvider
from app.retrieval.semantic_projection import SemanticProjection
from app.worker import KnowledgeWorker

PRIOR = ROOT / "data/evaluation/a08-new-questions/20260914T054425Z-791f6700"
ARMS = ("original-v5", "coverage-v4", "coverage-v5")


def exact_cache(folder):
    result = {}
    for path in (folder / "generation").glob("call-*/request.json"):
        request = feedback_trial.read(path)
        response = path.with_name("response.json")
        if "Research input:" not in request["prompt"] and response.exists():
            key = sha(json.dumps([request["system_prompt"], request["prompt"]], ensure_ascii=False))
            result[key] = feedback_trial.read(response)["text"]
    return result


def snapshot_diagnosis(package, original):
    previous = {s.item_id: s for s in original.retrieval_audit.selections}
    return {
        "tokens": package.token_usage.model_dump(),
        "selected": [
            {
                "claim_id": b.claim.claim_id,
                "old_rank": previous[b.claim.claim_id].rank,
                "old_selected": previous[b.claim.claim_id].selected,
                "chunk_ids": [c.chunk_id for c in b.chunks],
            }
            for b in package.knowledge.claim_bundles
        ],
        "reranking": package.retrieval_audit.parameters.get("reranking"),
    }


def run(*, execute=False, refine=False):
    arms = ("original-v6", "coverage-v4", "coverage-v6") if refine else ARMS
    if not execute:
        return {
            "mode": "dry_run",
            "arms": arms,
            "runs": 9,
            "max_new_calls": 39,
            "scope": "seen questions and corpus; human labels remain unset",
        }
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Live execution disabled in offline tests")
    prior_config = feedback_trial.read(PRIOR / "config.json")
    cases = prior_config["questions"]["cases"]
    if len(cases) != 3:
        raise ValueError("Exactly three frozen cases are required")
    profile = prior_config["profile"]
    chunking = Path(prior_config["from_run"])
    parent_config = feedback_trial.read(chunking / "config.json")
    seed = Path(parent_config["from_run"]) / profile["chunking"]
    live = Settings()
    client = get_llm_client(live)
    if not isinstance(client, LangChainChatClient):
        raise ValueError("Real model configuration required")
    client = replace(client, max_tokens=8192, timeout=120, max_retries=0, thinking_enabled=False)
    output = (
        ROOT
        / "data/evaluation/a08-coverage"
        / (f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
    )
    output.mkdir(parents=True, exist_ok=False)
    protected_paths = [
        *PRIOR.rglob("*.json"),
        *PRIOR.rglob("knowledge.db"),
        *seed.rglob("*.json"),
        *seed.rglob("*.npy"),
        seed / "knowledge.db",
        Path(live.knowledge_db_path).resolve(),
    ]
    protected = {p: feedback_trial.digest(p) for p in protected_paths if p.is_file()}
    config = {
        "schema": "a08-coverage-trial-v1",
        "from_run": str(PRIOR),
        "profile": profile,
        "cases": cases,
        "arms": arms,
        "code": code_fingerprint(),
        "max_new_calls": 39,
        "max_ranking_calls": 3,
        "per_run_calls": 4,
        "context_tokens": 16000,
        "run_tokens": 1048576,
        "output_tokens": 8192,
        "timeout_seconds": {"ranking": 90, "generation": 120},
        "sdk_retries": 0,
        "provider": client.provider_name,
        "model": client.model,
        "human_labels": None,
        "spending": "observe; unknown remains unknown",
        "protected": {str(p): d for p, d in protected.items()},
    }
    write_json(output / "config.json", config)
    rows = [
        {"case": c["id"], "arm": arm, "status": "not_attempted", "human_review": None}
        for c in cases
        for arm in arms
    ]
    write_json(output / "results.json", rows)
    print(f"Coverage trial: {output}", flush=True)
    provider = LocalEmbeddingProvider(profile["model"], device="cuda")
    try:
        for case in cases:
            source = PRIOR / case["id"]
            original = ContextPackage.model_validate(
                feedback_trial.read(source / "archive/snapshot.json")
            )
            cache = exact_cache(source)
            selected_snapshot = None
            for arm in arms:
                row = next(r for r in rows if r["case"] == case["id"] and r["arm"] == arm)
                folder = output / case["id"] / arm
                folder.mkdir(parents=True)
                current = None
                row["status"] = "preparing"
                write_json(output / "results.json", rows)
                try:
                    copied_from = (
                        output / case["id"] / "coverage-v4"
                        if arm in {"coverage-v5", "coverage-v6"}
                        else source
                    )
                    feedback_trial.backup_archive(
                        copied_from / "knowledge.db", folder / "knowledge.db"
                    )
                    repo = KnowledgeRepository(str(folder / "knowledge.db"))
                    settings = isolated_settings(folder, live)
                    if arm == "coverage-v4":
                        projection = SemanticProjection(
                            repo, provider, seed / f"index-{profile['model']}"
                        )
                        ranking_client = RecordedClient(
                            replace(client, timeout=90), folder / "ranking", {}
                        )
                        builder = ContextBuilderService(
                            repo,
                            vector_retriever=QdrantContextCandidateRetriever(projection),
                            coverage_reranker=(
                                CoreCoverageReranker(ranking_client)
                                if refine
                                else CoverageReranker(ranking_client)
                            ),
                        )
                        package = builder.build_context(
                            ContextBuildRequest(
                                task_id=original.task.task_id,
                                max_tokens=16000,
                                reading_format="inline-v1",
                                enable_vector_candidates=True,
                                retrieval_strategy=profile["strategy"],
                                evidence_reranking="coverage-v2" if refine else "coverage-v1",
                            )
                        )
                        if any("vector_unavailable" in n for n in package.diagnostics.notices):
                            raise ValueError("Vector projection failed")
                        selected_snapshot = package.snapshot_id
                        row["ranking_paid_calls"] = ranking_client.paid
                    else:
                        snapshot_id = (
                            original.snapshot_id
                            if arm.startswith("original-")
                            else selected_snapshot
                        )
                        if not snapshot_id:
                            raise ValueError("No coverage snapshot from the preceding arm")
                        package = ContextBuilderService(repo).snapshot_repository.get(snapshot_id)
                    write_json(folder / "selection.json", snapshot_diagnosis(package, original))
                    llm = RecordedClient(client, folder / "generation", cache)
                    service = AgentRunService(repo, settings=settings, llm=llm)
                    current = service.create_run(
                        original.project.project_id,
                        original.task.task_id,
                        AgentRunCreateRequest(
                            context_snapshot_id=package.snapshot_id,
                            workflow="research_v4"
                            if arm == "coverage-v4"
                            else "research_v6"
                            if refine
                            else "research_v5",
                            max_steps=10,
                            max_tool_calls=3,
                            token_budget=1048576,
                            create_memory_proposal=False,
                        ),
                    )
                    row.update(status="queued", run_id=current.id, snapshot_id=package.snapshot_id)
                    write_json(output / "results.json", rows)
                    worker = KnowledgeWorker(
                        repo,
                        ingestion_service=None,
                        report_service=None,
                        agent_runtime=AgentRuntime(
                            service,
                            checkpoint_factory=AgentCheckpointFactory(
                                settings.agent_checkpoint_path
                            ),
                        ),
                        lease_seconds=600,
                    )
                    if not worker.run_once():
                        raise ValueError("Expected production Worker run")
                except Exception as exc:
                    row.update(status="exception", **error_details(exc))
                finally:
                    if current:
                        row.update(
                            feedback_trial.archive_run(service, current.id, folder / "archive")
                        )
                        row["generation_paid_calls"] = llm.paid
                    write_json(output / "results.json", rows)
                print(f"{case['id']} {arm}: {row['status']}", flush=True)
    finally:
        isolation = {
            str(p): p.is_file() and feedback_trial.digest(p) == d for p, d in protected.items()
        }
        write_json(output / "isolation.json", isolation)
        if not all(isolation.values()):
            raise ValueError("Protected evidence or production data changed")
    return {"output": str(output), "results": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--refine", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(execute=args.execute, refine=args.refine), ensure_ascii=False))
