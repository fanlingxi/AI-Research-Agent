"""Explicit, sequential real-model evaluation through the production Worker.

Run ``python -m app.benchmarking.live --help``. No network on import or dry run.
The Phase 6 fixture runner and its network guard remain independent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.models import AgentRunCreateRequest
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.benchmarking.live_budget import BudgetedLLM, SpendingLedger
from app.benchmarking.live_dataset import approved_dataset, import_papers, task_input
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import get_llm_client
from app.worker import KnowledgeWorker

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "benchmarks/research/papers-v1"
APPROVAL = ROOT / "docs/improvement/a02/papers-approval.json"
POLICY = ROOT / "docs/improvement/a02-budget-v2/run-policy.json"
EVALUATION_ROOT = ROOT / "data/evaluation/a02"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def code_fingerprint() -> dict:
    files = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ("app", "scripts") for p in sorted((ROOT / folder).rglob("*.py"))
        if "__pycache__" not in p.parts
    }
    return {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        "tracked_diff_sha256": hashlib.sha256(subprocess.check_output(
            ["git", "diff", "--binary", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        )).hexdigest(),
        "files": files,
        "source_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
    }


def isolated_settings(root: Path, live: Settings) -> Settings:
    return Settings(
        _env_file=None, llm_provider=live.llm_provider, deepseek_api_key=live.deepseek_api_key,
        deepseek_model=live.deepseek_model, deepseek_base_url=live.deepseek_base_url,
        llm_temperature=0.2, knowledge_db_path=str(root / "knowledge.db"),
        agent_checkpoint_path=str(root / "checkpoints.db"),
        knowledge_vault_path=str(root / "vault"), qdrant_url="http://127.0.0.1:9",
        neo4j_uri="bolt://127.0.0.1:9", embedding_provider="hash",
    )


def seeded_repository(output: Path, corpus, approval: dict):
    """Reuse one sealed input database so UUID tie-breaking is held constant."""
    seed = EVALUATION_ROOT / "paper-seed-v1"
    manifest_path = seed / "manifest.json"
    identity = {
        "dataset_freeze_sha256": approval["freeze_sha256"],
        "importer_sha256": hashlib.sha256(
            (ROOT / "app/benchmarking/live_dataset.py").read_bytes()
        ).hexdigest(),
    }
    if not seed.exists():
        seed.mkdir(exist_ok=False)
        repository = KnowledgeRepository(str(seed / "knowledge.db"))
        mapping = import_papers(repository, corpus, DATASET)
        write_json(seed / "source-map.json", mapping)
        write_json(manifest_path, {
            **identity, "files": {
                name: hashlib.sha256((seed / name).read_bytes()).hexdigest()
                for name in ("knowledge.db", "source-map.json")
            },
        })
    if not manifest_path.exists():
        raise ValueError("Incomplete seed retained; do not silently rebuild evaluation identities")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if any(manifest.get(k) != v for k, v in identity.items()):
        raise ValueError("Seed importer or dataset version changed; create an explicit new seed")
    for name, digest in manifest["files"].items():
        if hashlib.sha256((seed / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Sealed evaluation seed changed")
    target = output / "knowledge.db"
    if target.exists():
        raise ValueError("Evaluation output database already exists")
    uri = (seed / "knowledge.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
    write_json(output / "seed-manifest.json", manifest)
    mapping = json.loads((seed / "source-map.json").read_text(encoding="utf-8"))
    return KnowledgeRepository(str(target)), mapping


def span_coverage(task, corpus, package, mapping) -> dict:
    """Exact gold-span coverage by selected source intervals, computed post-snapshot."""
    intervals = [
        mapping["chunks"][c.chunk_id]
        for b in package.knowledge.claim_bundles for c in b.chunks
    ]
    gold = {s.id: s for s in corpus.spans}
    coverage = {}
    for sid in task.expectation.required_evidence_span_ids:
        span = gold[sid]
        end = span.start
        for item in sorted(intervals, key=lambda i: i["start"]):
            if item["source_id"] == span.source_id and item["start"] <= end:
                end = max(end, item["end"])
        coverage[sid] = end >= span.end
    return {
        "required_span_count": len(coverage), "covered_span_count": sum(coverage.values()),
        "span_recall": sum(coverage.values()) / len(coverage) if coverage else None,
        "required_span_coverage": coverage,
        "selected_bundle_count": len(package.knowledge.claim_bundles),
    }


def execute_task(repository, context, service, worker, task, corpus, mapping, llm, output,
                 retrieval_strategy="legacy"):
    inputs = task_input(task)
    attempt_id = f"{output.parent.name}/{output.name}"
    result = {
        "attempt_id": attempt_id, "task_id": task.id, "split": task.split,
        "status": "preparing", "semantic_success": None, "human_review": "pending",
        "started_at": datetime.now(UTC).isoformat(),
    }
    write_json(output / "result.json", result)
    started = time.monotonic()
    run = None
    try:
        memory = repository.memory_repository
        project = memory.create_project(
            name=f"A02 {task.id}", goal="仅依据授权论文完成研究任务。", domain="research",
            metadata={"evaluation_only": True},
        )
        memory.replace_project_knowledge_scopes(
            project.id, [mapping["sources"][sid]["scope"] for sid in inputs["allowed_source_ids"]],
            expected_project_revision=project.revision,
        )
        work = memory.create_workspace_task(
            project_id=project.id, title=inputs["question"][:200], goal=inputs["question"],
            priority="high", metadata={"expected_output": "\n".join(inputs["report_requirements"])},
        )
        result.update(project_id=project.id, workspace_task_id=work.id)
        package = context.build_context(ContextBuildRequest(
            project_id=project.id, task_id=work.id,
            max_tokens=llm.ledger.policy.get("context_max_tokens", 6000),
            enable_vector_candidates=False, enable_graph_candidates=False,
            retrieval_strategy=retrieval_strategy,
        ))
        allowed = {mapping["sources"][sid]["document_id"] for sid in inputs["allowed_source_ids"]}
        if any(d.document_id not in allowed for b in package.knowledge.claim_bundles
               for d in b.documents):
            raise ValueError("Snapshot source scope violation")
        write_json(output / "snapshot.json", package.model_dump(mode="json"))
        result.update(snapshot_id=package.snapshot_id, snapshot_sha256=package.package_sha256,
                      retrieval=span_coverage(task, corpus, package, mapping))
        run = service.create_run(project.id, work.id, AgentRunCreateRequest(
            context_snapshot_id=package.snapshot_id, workflow="research", max_steps=10,
            max_tool_calls=1, token_budget=llm.ledger.policy.get("run_token_budget", 16000),
            create_memory_proposal=False,
        ))
        result["run_id"] = run.id
        write_json(output / "result.json", result)
        llm.begin_attempt(attempt_id)
        llm.audit_directory = output / "model-responses"
        if not worker.run_once():
            raise RuntimeError("Expected queued production AgentRun job")
    except KeyboardInterrupt:
        result["status"] = "cancelled"
        if run is not None:
            service.cancel_run(run.id)
        raise
    except Exception as exc:
        result.update(status="exception", error_type=type(exc).__name__)
    finally:
        if run is not None:
            final = service.get_run(run.id)
            result["run_status"] = final.status
            if result["status"] != "cancelled":
                result["status"] = final.status
            write_json(output / "run.json", final.model_dump(mode="json"))
            write_json(output / "events.json", [
                e.model_dump(mode="json") for e in service.repository.list_events(run.id)
            ])
            write_json(output / "tools.json", [
                t.model_dump(mode="json") for t in service.repository.list_tool_calls(run.id)
            ])
            try:
                artifact = service.repository.get_output(run.id)
            except KeyError:
                artifact = None
            if artifact:
                write_json(output / "output.json", artifact.model_dump(mode="json"))
                (output / "report.md").write_text(artifact.rendered_text, encoding="utf-8")
                result["citation_structure_passed"] = artifact.validation.get("passed")
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(output / "result.json", result)
    return result


def summarize(results: list[dict], ledger_report: dict) -> dict:
    counts = dict(Counter(r["status"] for r in results))
    return {
        "planned_count": len(results), "status_counts": counts,
        "attempted_count": sum(r["status"] != "not_attempted" for r in results),
        "completed_count": counts.get("completed", 0),
        "completion_rate_over_planned": counts.get("completed", 0) / len(results),
        "semantic_success_rate": None, "semantic_review": "pending_human",
        "budget": ledger_report, "results": results,
    }


def run(args) -> int:
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    SpendingLedger.validate_policy(policy)
    selected = [t for t in tasks if args.split == "all" or t.split == args.split]
    if args.task:
        requested = set(args.task)
        selected = [t for t in selected if t.id in requested]
        if {t.id for t in selected} != requested:
            raise ValueError("Unknown task or task outside selected split")
    if not selected:
        raise ValueError("No selected tasks")
    if not args.execute:
        print(json.dumps({"mode": "dry_run", "tasks": [t.id for t in selected],
                          "approval": approval["approval_id"], "policy": policy},
                         ensure_ascii=False, indent=2))
        return 0
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Live entry is disabled in offline test environments")
    price_age = datetime.now(UTC).date() - datetime.fromisoformat(policy["verified_at"]).date()
    if not 0 <= price_age.days <= 1:
        raise ValueError("Price verification expired; verify official prices before execution")
    live = Settings()
    if (live.llm_provider != "deepseek" or not live.deepseek_api_key
            or live.deepseek_model != policy["request_model"]
            or live.deepseek_base_url.rstrip("/") != "https://api.deepseek.com"):
        raise ValueError("Expected the authorized official DeepSeek configuration")
    EVALUATION_ROOT.mkdir(parents=True, exist_ok=True)
    ledger = SpendingLedger(EVALUATION_ROOT / "cumulative-budget.sqlite", policy)
    if ledger.report()["stopped_for_unknown"]:
        raise ValueError("Unknown earlier usage must be reconciled before further paid execution")
    output = EVALUATION_ROOT / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    settings = isolated_settings(output, live)
    if Path(settings.knowledge_db_path).resolve() == Path(live.knowledge_db_path).resolve():
        raise ValueError("Evaluation must not use the configured production database")
    delegate = get_llm_client(settings)
    delegate.max_tokens = policy["max_output_tokens"]
    delegate.max_retries = 0
    delegate.timeout = policy["request_timeout_seconds"]
    delegate.thinking_enabled = False
    llm = BudgetedLLM(delegate, ledger)
    write_json(output / "config.json", {
        "version": "a02-live-v2", "policy": policy, "approval": approval,
        "code": code_fingerprint(), "dataset_freeze": json.loads(
            (DATASET / "freeze.json").read_text(encoding="utf-8")),
        "strategy": f"{getattr(args, 'retrieval_strategy', 'legacy')}; vector=false; graph=false; "
                    f"context={policy['context_max_tokens']}",
        "run_token_budget": policy["run_token_budget"],
        "importer": "verbatim-600-v1; all pages; no gold-based selection",
        "thinking": "disabled", "temperature": 0.2, "judge": None,
        "production_database": str(Path(live.knowledge_db_path).resolve()),
    })
    results = [dict(task_id=t.id, split=t.split, status="not_attempted") for t in selected]
    write_json(output / "summary.json", summarize(results, ledger.report()))
    print(f"Evaluation output: {output}", flush=True)
    started = time.monotonic()
    try:
        repository, mapping = seeded_repository(output, corpus, approval)
        write_json(output / "source-map.json", mapping)
        context = ContextBuilderService(repository)
        service = AgentRunService(repository, context_builder=context, settings=settings, llm=llm)
        runtime = AgentRuntime(service, checkpoint_factory=AgentCheckpointFactory(
            settings.agent_checkpoint_path))
        # Only AgentRun jobs are enqueued. No ingestion/report/projection drain:
        # run_once prioritizes the queued AgentRun over optional projections.
        worker = KnowledgeWorker(repository, ingestion_service=None, report_service=None,
                                 agent_runtime=runtime, lease_seconds=180)
        for index, task in enumerate(selected):
            if ledger.report()["stopped_for_unknown"]:
                break
            if time.monotonic() - started > policy["session_deadline_seconds"]:
                break
            attempt = output / f"{index + 1:02d}-{task.id}"
            results[index] = execute_task(
                repository, context, service, worker, task, corpus, mapping, llm, attempt,
                retrieval_strategy=getattr(args, "retrieval_strategy", "legacy"),
            )
            write_json(output / "summary.json", summarize(results, ledger.report()))
            print(f"{task.id}: {results[index]['status']}", flush=True)
    except KeyboardInterrupt:
        print("Evaluation interrupted; existing results and reservations retained", flush=True)
    except Exception as exc:
        write_json(output / "session-error.json", {"error_type": type(exc).__name__})
        raise
    finally:
        # Reload durable attempt rows, including an interrupted current task.
        for index, task in enumerate(selected):
            result_file = output / f"{index + 1:02d}-{task.id}" / "result.json"
            if result_file.exists():
                results[index] = json.loads(result_file.read_text(encoding="utf-8"))
        write_json(output / "summary.json", summarize(results, ledger.report()))
    return 0 if all(r["status"] == "completed" for r in results) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Allow budgeted paid model calls")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    parser.add_argument("--task", action="append", help="Restrict to named tasks; repeatable")
    parser.add_argument("--retrieval-strategy", choices=("legacy", "bm25-v1", "hybrid-v1"),
                        default="legacy", help="Context strategy; paid entry keeps vector disabled")
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
