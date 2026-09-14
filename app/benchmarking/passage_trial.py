"""Bounded dev reports from newly reviewed passages through the production Worker."""

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
from app.benchmarking.live import (
    APPROVAL,
    DATASET,
    ROOT,
    code_fingerprint,
    isolated_settings,
    write_json,
)
from app.benchmarking.live_dataset import approved_dataset, task_input
from app.benchmarking.passage_dataset import import_passages
from app.benchmarking.reading_trial import CASES, RecordedClient
from app.benchmarking.report_closeout import CORRECTIONS
from app.benchmarking.selection_options import sha
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.knowledge.evidence_passages import PASSAGE_VERSION
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LangChainChatClient, get_llm_client
from app.worker import KnowledgeWorker


def run(execute=False, replay_from=None):
    if not execute:
        return {
            "mode": "dry_run",
            "cases": CASES,
            "new_runs": 3,
            "max_model_calls": 12,
            "passage_version": PASSAGE_VERSION,
            "holdout": "not_run",
        }
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Live execution disabled in offline tests")
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    tasks = {t.id: t for t in tasks if t.split == "dev"}
    if not set(CASES) <= tasks.keys():
        raise ValueError("Only approved dev cases allowed")
    replay = Path(replay_from).resolve() if replay_from else None
    if replay:
        allowed_root = (ROOT / "data/evaluation/a08-passages").resolve()
        if replay.parent != allowed_root or not replay.is_dir():
            raise ValueError("Replay requires a managed passage trial")
        if any(not p.resolve().is_relative_to(replay) for p in replay.rglob("*")):
            raise ValueError("Replay archive escapes its managed directory")
        config = feedback_trial.read(replay / "config.json")
        if (
            config.get("schema") != "a08-passage-trial-v1"
            or config.get("dataset_freeze_sha256") != approval["freeze_sha256"]
        ):
            raise ValueError("Replay version or dataset mismatch")
    workflow = "research_v4" if replay else "research_v3"
    live = Settings()
    client = get_llm_client(live)
    if not isinstance(client, LangChainChatClient):
        raise ValueError("Configured real model required")
    client = replace(client, max_tokens=8192, timeout=120, max_retries=0, thinking_enabled=False)
    # Snapshot existing historical evidence and the production database before any work.
    prior = ROOT / "data/evaluation/a08-production/20260914T022937Z-e5b3154e-feedback-closeout"
    protected = {p: feedback_trial.digest(p) for p in prior.rglob("*") if p.is_file()}
    if replay:
        protected.update({p: feedback_trial.digest(p) for p in replay.rglob("*") if p.is_file()})
    production = Path(live.knowledge_db_path).resolve()
    if production.exists():
        protected[production] = feedback_trial.digest(production)
    output = (
        ROOT
        / "data/evaluation/a08-passages"
        / (f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
    )
    output.mkdir(parents=True, exist_ok=False)
    rows = [
        {"task_id": task_id, "status": "not_attempted", "human_recheck": None} for task_id in CASES
    ]
    write_json(
        output / "config.json",
        {
            "schema": "a08-passage-trial-v1",
            "code": code_fingerprint(),
            "dataset_freeze_sha256": approval["freeze_sha256"],
            "passage_version": PASSAGE_VERSION,
            "cases": CASES,
            "model": client.model,
            "provider": client.provider_name,
            "max_model_calls": 12,
            "per_run_calls": 4,
            "timeout_seconds": 120,
            "output_tokens": 8192,
            "sdk_retries": 0,
            "thinking": "disabled",
            "context_tokens": 262144,
            "run_tokens": 1048576,
            "spending_mode": "observe",
            "workflow": workflow,
            "same_snapshots_from": str(replay) if replay else None,
            "corrections": CORRECTIONS,
            "holdout": "not_run",
            "comparison": (
                "diagnostic only; new chunk identities and selection, not a causal estimate"
            ),
        },
    )
    write_json(output / "results.json", rows)
    print(f"Passage trial: {output}", flush=True)
    try:
        seed = output / "seed.db"
        if replay:
            mapping = feedback_trial.read(replay / "source-map.json")
        else:
            mapping = import_passages(KnowledgeRepository(str(seed)), corpus, DATASET)
        write_json(output / "source-map.json", mapping)
        print(f"Passage map: {len(mapping['chunks'])} entries", flush=True)
        for row in rows:
            task_id = row["task_id"]
            folder = output / task_id
            folder.mkdir()
            row["status"] = "preparing"
            write_json(output / "results.json", rows)
            current = None
            try:
                source = replay / task_id if replay else None
                feedback_trial.backup_archive(
                    source / "knowledge.db" if source else seed, folder / "knowledge.db"
                )
                repo = KnowledgeRepository(str(folder / "knowledge.db"))
                settings = isolated_settings(folder, live)
                cache = {}
                if source:
                    for path in (source / "generation").glob("call-*/request.json"):
                        request = feedback_trial.read(path)
                        response = path.with_name("response.json")
                        if "Research input:" not in request["prompt"] and response.exists():
                            key = sha(
                                json.dumps(
                                    [request["system_prompt"], request["prompt"]],
                                    ensure_ascii=False,
                                )
                            )
                            cache[key] = feedback_trial.read(response)["text"]
                llm = RecordedClient(client, folder / "generation", cache)
                service = AgentRunService(repo, settings=settings, llm=llm)
                inputs = task_input(tasks[task_id])
                if source:
                    parent = feedback_trial.read(source / "archive/run.json")
                    package = service.context_builder.snapshot_repository.get(
                        parent["context_snapshot_id"]
                    )
                    archived = feedback_trial.read(source / "archive/snapshot.json")
                    if package.package_sha256 != archived["package_sha256"]:
                        raise ValueError("Replay snapshot and parent archive mismatch")
                    row["comparison_run_id"] = parent["id"]
                else:
                    memory = repo.memory_repository
                    project = memory.create_project(
                        name=f"A08 passages {task_id}",
                        goal="仅依据授权论文回答问题",
                        domain="research",
                        metadata={"evaluation_only": True},
                    )
                    memory.replace_project_knowledge_scopes(
                        project.id,
                        [mapping["sources"][s]["scope"] for s in inputs["allowed_source_ids"]],
                        expected_project_revision=project.revision,
                    )
                    task = memory.create_workspace_task(
                        project_id=project.id,
                        title=inputs["question"][:200],
                        goal=inputs["question"] + "\n\n本次修订要求：" + CORRECTIONS[task_id],
                        priority="high",
                        metadata={"expected_output": "\n".join(inputs["report_requirements"])},
                    )
                    package = ContextBuilderService(repo).build_context(
                        ContextBuildRequest(
                            task_id=task.id,
                            max_tokens=262144,
                            reading_format="inline-v1",
                            enable_vector_candidates=False,
                            enable_graph_candidates=False,
                        )
                    )
                for bundle in package.knowledge.claim_bundles:
                    for chunk in bundle.chunks:
                        source = mapping["chunks"][chunk.chunk_id]
                        if (
                            source["source_id"] not in inputs["allowed_source_ids"]
                            or source["text_sha256"] != chunk.content_sha256
                            or source["claim_id"] != bundle.claim.claim_id
                        ):
                            raise ValueError("Snapshot scope, identity or text mismatch")
                quotes = [e.quote for b in package.knowledge.claim_bundles for e in b.evidence]
                row.update(
                    selected_bundles=len(package.knowledge.claim_bundles),
                    quotes_over_600=sum(len(q) > 600 for q in quotes),
                    max_quote_chars=max(map(len, quotes), default=0),
                    snapshot_tokens=package.token_usage.used,
                )
                current = service.create_run(
                    package.project.project_id,
                    package.task.task_id,
                    AgentRunCreateRequest(
                        context_snapshot_id=package.snapshot_id,
                        workflow=workflow,
                        max_steps=10,
                        max_tool_calls=3,
                        token_budget=1048576,
                        create_memory_proposal=False,
                    ),
                )
                row.update(run_id=current.id, status="queued")
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
                row.update(status="exception", error_type=type(exc).__name__)
            finally:
                if current:
                    row.update(feedback_trial.archive_run(service, current.id, folder / "archive"))
                    row["paid_calls"] = llm.paid
                write_json(output / "results.json", rows)
            print(f"{task_id}: {row['status']}", flush=True)
    except Exception as exc:
        write_json(output / "setup-error.json", {"error_type": type(exc).__name__})
        raise
    finally:
        unchanged = {
            str(p): p.exists() and feedback_trial.digest(p) == digest
            for p, digest in protected.items()
        }
        write_json(output / "isolation.json", {"protected_unchanged": unchanged})
        if not all(unchanged.values()):
            raise ValueError("Protected production or historical files changed")
    return {"output": str(output), "results": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--replay-from")
    args = parser.parse_args()
    print(json.dumps(run(args.execute, args.replay_from), ensure_ascii=False, indent=2))
