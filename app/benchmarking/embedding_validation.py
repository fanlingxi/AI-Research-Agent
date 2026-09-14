"""Explicit, bounded new-question reports after the embedding/chunking comparison."""

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
from app.benchmarking.embedding_trial import OUTPUT_ROOT, error_details, write_json
from app.benchmarking.live import APPROVAL, DATASET, ROOT, code_fingerprint, isolated_settings
from app.benchmarking.live_dataset import approved_dataset
from app.benchmarking.reading_trial import RecordedClient
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.retrieval import QdrantContextCandidateRetriever
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LangChainChatClient, get_llm_client
from app.retrieval.neural_embeddings import LocalEmbeddingProvider
from app.retrieval.semantic_projection import SemanticProjection
from app.worker import KnowledgeWorker

QUESTIONS = ROOT / "docs/improvement/a08-embedding/new-questions.json"


def run(*, execute=False, from_run=None):
    if not execute:
        return {
            "mode": "dry_run",
            "cases": 3,
            "max_model_calls": 12,
            "scope": "new questions on seen papers; human review required",
        }
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Live execution disabled in offline tests")
    previous = Path(from_run or "").resolve()
    if previous.parent != OUTPUT_ROOT.resolve():
        raise ValueError("Validation requires a managed chunking comparison")
    config = feedback_trial.read(previous / "config.json")
    summary = feedback_trial.read(previous / "summary.json")
    profile = summary.get("validation_profile")
    if config["stage"] != "chunking" or not profile:
        raise ValueError("Complete model and chunking comparisons are required")
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    if config["dataset_freeze_sha256"] != approval["freeze_sha256"]:
        raise ValueError("Dataset changed before validation")
    if config.get("new_questions_sha256") != feedback_trial.digest(QUESTIONS):
        raise ValueError("New questions changed after the comparison was frozen")
    questions = feedback_trial.read(QUESTIONS)
    cases = questions["cases"]
    if len(cases) != 3 or len({c["id"] for c in cases}) != 3:
        raise ValueError("Validation is limited to three unique questions")
    known = {s.id for s in corpus.sources}
    for case in cases:
        if not case["source_ids"] or not set(case["source_ids"]) <= known:
            raise ValueError("Question has no valid authorized source range")
        if case["question"] in {t.question for t in tasks}:
            raise ValueError("Question is an existing dev/holdout item")
    source = previous
    if profile["chunking"] == "sentence-2400-v2":
        source = Path(config["from_run"]).resolve()
        if source.parent != OUTPUT_ROOT.resolve():
            raise ValueError("Sentence comparison is outside the managed domain")
    seed = source / profile["chunking"]
    mapping = feedback_trial.read(seed / "source-map.json")
    live = Settings()
    client = get_llm_client(live)
    if not isinstance(client, LangChainChatClient):
        raise ValueError("A configured real model is required")
    client = replace(client, max_tokens=8192, timeout=120, max_retries=0, thinking_enabled=False)
    protected = {
        p: feedback_trial.digest(p)
        for p in (
            seed / "knowledge.db",
            seed / "source-map.json",
            previous / "config.json",
            previous / "summary.json",
            previous / "results.json",
            QUESTIONS,
            Path(live.knowledge_db_path).resolve(),
        )
        if p.exists()
    }
    output = (
        ROOT
        / "data/evaluation/a08-new-questions"
        / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    )
    output.mkdir(parents=True, exist_ok=False)
    rows = [{"task_id": c["id"], "status": "not_attempted", "human_recheck": None} for c in cases]
    write_json(
        output / "config.json",
        {
            "schema": "a08-new-question-validation-v1",
            "code": code_fingerprint(),
            "profile": profile,
            "from_run": str(previous),
            "questions": questions,
            "fingerprints": {str(p): d for p, d in protected.items()},
            "model": client.model,
            "provider": client.provider_name,
            "max_model_calls": 12,
            "per_run_calls": 4,
            "sdk_retries": 0,
            "timeout_seconds": 120,
            "output_tokens": 8192,
            "context_tokens": 16000,
            "run_tokens": 1048576,
            "workflow": "research_v4",
            "spending": "observe; unknown costs remain unknown",
            "human_labels": None,
            "accuracy_improvement": "not_estimated",
        },
    )
    write_json(output / "results.json", rows)
    print(f"New-question validation: {output}", flush=True)
    provider = LocalEmbeddingProvider(profile["model"], device=config["device"])
    try:
        for case, row in zip(cases, rows, strict=True):
            folder = output / case["id"]
            folder.mkdir()
            current = None
            row["status"] = "preparing"
            write_json(output / "results.json", rows)
            try:
                feedback_trial.backup_archive(seed / "knowledge.db", folder / "knowledge.db")
                repo = KnowledgeRepository(str(folder / "knowledge.db"))
                projection = SemanticProjection(repo, provider, seed / f"index-{profile['model']}")
                builder = ContextBuilderService(
                    repo, vector_retriever=QdrantContextCandidateRetriever(projection)
                )
                mem = repo.memory_repository
                project = mem.create_project(
                    name=case["id"],
                    goal="仅依据授权论文回答问题",
                    domain="research",
                    metadata={"evaluation_only": True},
                )
                scopes = [mapping["sources"][key]["scope"] for key in case["source_ids"]]
                mem.replace_project_knowledge_scopes(
                    project.id, scopes, expected_project_revision=project.revision
                )
                task = mem.create_workspace_task(
                    project_id=project.id,
                    title=case["question"][:200],
                    goal=case["question"],
                    priority="high",
                    metadata={
                        "expected_output": (
                            "中文作答，每个事实引用原文证据；"
                            "区分作者报告、推论与证据不足。"
                        )
                    },
                )
                package = builder.build_context(
                    ContextBuildRequest(
                        task_id=task.id,
                        max_tokens=16000,
                        reading_format="inline-v1",
                        enable_vector_candidates=True,
                        retrieval_strategy=profile["strategy"],
                    )
                )
                if any("vector_unavailable" in n for n in package.diagnostics.notices):
                    raise ValueError("Validation vector model failed")
                for bundle in package.knowledge.claim_bundles:
                    for chunk in bundle.chunks:
                        span = mapping["chunks"][chunk.chunk_id]
                        if (
                            span["source_id"] not in case["source_ids"]
                            or span["text_sha256"] != chunk.content_sha256
                            or span["claim_id"] != bundle.claim.claim_id
                        ):
                            raise ValueError("Snapshot source identity or scope changed")
                settings = isolated_settings(folder, live)
                llm = RecordedClient(client, folder / "generation", {})
                service = AgentRunService(repo, settings=settings, llm=llm)
                current = service.create_run(
                    project.id,
                    task.id,
                    AgentRunCreateRequest(
                        context_snapshot_id=package.snapshot_id,
                        workflow="research_v4",
                        max_steps=10,
                        max_tool_calls=3,
                        token_budget=1048576,
                        create_memory_proposal=False,
                    ),
                )
                row.update(
                    status="queued", run_id=current.id, snapshot_tokens=package.token_usage.used
                )
                write_json(output / "results.json", rows)
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
                if not worker.run_once():
                    raise ValueError("Expected queued production run")
            except Exception as exc:
                row.update(status="exception", **error_details(exc))
            finally:
                if current:
                    row.update(feedback_trial.archive_run(service, current.id, folder / "archive"))
                    row["paid_calls"] = llm.paid
                write_json(output / "results.json", rows)
            print(f"{case['id']}: {row['status']}", flush=True)
    finally:
        unchanged = {
            str(p): p.exists() and feedback_trial.digest(p) == d for p, d in protected.items()
        }
        write_json(output / "isolation.json", unchanged)
        if not all(unchanged.values()):
            raise ValueError("Protected evidence or production data changed")
    return {"output": str(output), "results": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--from-run")
    args = parser.parse_args()
    print(json.dumps(run(execute=args.execute, from_run=args.from_run), ensure_ascii=False))
