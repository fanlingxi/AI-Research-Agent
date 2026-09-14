"""Explicit dev-only reading-view trial through the production Worker."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.models import AgentRunCreateRequest
from app.agent.research_workflow import ResearchWorkflow
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.benchmarking import feedback_trial
from app.benchmarking.context_neighbors import PRIOR, load_snapshot
from app.benchmarking.live import (
    APPROVAL,
    DATASET,
    ROOT,
    code_fingerprint,
    isolated_settings,
    write_json,
)
from app.benchmarking.live_dataset import approved_dataset
from app.benchmarking.selection_options import reading_view, restore_view, sha
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.domain_plugins.registry import DomainPluginRegistry
from app.domain_plugins.research.plugin import ResearchDomainPlugin
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LangChainChatClient, LLMClient, get_llm_client
from app.worker import KnowledgeWorker

CASES = ("evaluation-q1", "evaluation-q8", "attribution-q12")


class ReadingWorkflow(ResearchWorkflow):
    def _research_input(self, package):
        payload = super()._research_input(package)
        view, sidecar = reading_view(payload, slim=True)
        if restore_view(view, sidecar) != payload:
            raise ValueError("Reading prototype changed source data")
        view["instructions"] = (
            'Resolve each {"$text": n} using texts[n]. These are exact original strings, '
            "not summaries. All claim and evidence IDs and citation rules remain unchanged."
        )
        return view


class ReadingPlugin(ResearchDomainPlugin):
    def __init__(self, compact, inline=False):
        self.compact = compact
        self.inline = inline
        name = "inline" if inline else "compact" if compact else "original"
        self.manifest = self.manifest.model_copy(
            deep=True, update={"version": f"a08-reading-{name}-experiment-v1"}
        )
        for spec in self.manifest.workflows:
            spec.checkpoint_namespace = f"a08_reading_{name}"
            spec.version = f"a08-reading-{name}-v1"

    def build_workflow(self, service, pin):
        if self.inline:
            return InlineReadingWorkflow(service)
        return ReadingWorkflow(service) if self.compact else ResearchWorkflow(service)


class InlineReadingWorkflow(ResearchWorkflow):
    def _research_input(self, package):
        payload = super()._research_input(package)
        view, sidecar = reading_view(payload, slim=True)
        if restore_view(view, sidecar) != payload:
            raise ValueError("Reading prototype changed source data")
        return restore_view(view)


class RecordedClient(LLMClient):
    def __init__(self, delegate, directory, cache):
        self.delegate, self.directory, self.cache = delegate, directory, cache
        self.provider_name, self.model = delegate.provider_name, delegate.model
        self.calls, self.paid = [], 0
        self.last_usage = {}

    def invoke(self, prompt, system_prompt=None):
        self.last_usage = {}
        key = sha(json.dumps([system_prompt, prompt], ensure_ascii=False))
        replayable = "Research input:" not in prompt
        if replayable and key in self.cache:
            self.last_usage = {"input_tokens": 0, "output_tokens": 0}
            self.calls.append(
                {
                    "kind": "exact_prompt_replay",
                    "prompt_sha256": key,
                    "usage": None,
                    "new_model_calls": 0,
                }
            )
            write_json(self.directory / "calls.json", self.calls)
            return self.cache[key]
        if self.paid >= 4:
            raise ValueError("Per-run model attempt limit reached")
        self.paid += 1
        record = {
            "kind": "live",
            "status": "pending",
            "prompt_sha256": key,
            "usage": None,
            "new_model_calls": 1,
            "cost_cny": None,
        }
        self.calls.append(record)
        folder = self.directory / f"call-{self.paid}"
        write_json(folder / "request.json", {"prompt": prompt, "system_prompt": system_prompt})
        write_json(self.directory / "calls.json", self.calls)
        started = time.perf_counter()
        try:
            response = self.delegate.invoke(prompt, system_prompt)
            self.last_usage = dict(self.delegate.last_usage)
            if self.delegate.last_usage_complete:
                record["usage"] = self.last_usage
            record["status"] = "returned"
            write_json(folder / "response.json", {"text": response})
            if replayable:
                self.cache[key] = response
            return response
        except Exception as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            record["latency_seconds"] = time.perf_counter() - started
            write_json(self.directory / "calls.json", self.calls)


def run(execute=False, inline_from=None):
    _, tasks, approval = approved_dataset(DATASET, APPROVAL)
    if not set(CASES) <= {t.id for t in tasks if t.split == "dev"}:
        raise ValueError("Only approved dev cases are allowed")
    if not execute:
        return {"cases": CASES, "new_runs": 6, "max_model_calls": 24, "mode": "dry_run"}
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Live execution disabled in offline tests")
    live = Settings()
    client = get_llm_client(live)
    if not isinstance(client, LangChainChatClient):
        raise ValueError("Configured live model required")
    client = replace(client, max_tokens=8192, timeout=120, max_retries=0, thinking_enabled=False)
    if inline_from:
        inline_from = Path(inline_from).resolve()
        if not inline_from.is_relative_to((ROOT / "data/evaluation/a08-reading").resolve()):
            raise ValueError("Follow-up must use an archived reading experiment")
        previous = json.loads((inline_from / "results.json").read_text(encoding="utf-8"))
        if len(previous) != 6 or any(r["status"] == "queued" for r in previous):
            raise ValueError("Original six-run trial must finish first")
    output = (
        ROOT
        / "data/evaluation/a08-reading"
        / (f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
    )
    output.mkdir(parents=True, exist_ok=False)
    write_json(
        output / "config.json",
        {
            "code": code_fingerprint(),
            "cases": CASES,
            "dataset_freeze_sha256": approval["freeze_sha256"],
            "prior": str(PRIOR),
            "provider": client.provider_name,
            "model": client.model,
            "output_tokens": 8192,
            "timeout_seconds": 120,
            "thinking": "disabled",
            "max_model_calls": 12 if inline_from else 24,
            "inline_from": str(inline_from) if inline_from else None,
            "context_budget": 262144,
            "run_budget": 1048576,
            "spending_mode": "observe",
            "human_semantic_acceptance": "pending",
            "holdout": "not_run",
        },
    )
    print(f"Reading output: {output}", flush=True)
    rows = []
    for task_id in CASES:
        seed = output / task_id / "seed.db"
        seed.parent.mkdir(parents=True, exist_ok=True)
        cache = {}
        if inline_from:
            feedback_trial.backup_archive(inline_from / task_id / "seed.db", seed)
            package = load_snapshot(inline_from / task_id / "original/archive/snapshot.json")
            for request_path in (inline_from / task_id / "original").glob("call-*/request.json"):
                request_data = json.loads(request_path.read_text(encoding="utf-8"))
                response_path = request_path.parent / "response.json"
                if "Research input:" not in request_data["prompt"] and response_path.exists():
                    key = sha(
                        json.dumps(
                            [request_data["system_prompt"], request_data["prompt"]],
                            ensure_ascii=False,
                        )
                    )
                    cache[key] = json.loads(response_path.read_text(encoding="utf-8"))["text"]
        else:
            feedback_trial.backup_archive(PRIOR / "knowledge.db", seed)
            repo = KnowledgeRepository(str(seed))
            original = load_snapshot(PRIOR / task_id / "off/snapshot.json")
            package = ContextBuilderService(repo).build_context(
                ContextBuildRequest(task_id=original.task.task_id, max_tokens=262144)
            )
        for variant in ("inline",) if inline_from else ("original", "compact"):
            folder = output / task_id / variant
            folder.mkdir(parents=True)
            settings = isolated_settings(folder, live)
            feedback_trial.backup_archive(seed, folder / "knowledge.db")
            isolated = KnowledgeRepository(str(folder / "knowledge.db"))
            with isolated.database.connect() as db:
                if db.execute(
                    "SELECT 1 FROM knowledge_jobs WHERE status IN ('queued','running')"
                ).fetchone():
                    raise ValueError("Pending historical jobs require isolation")
            llm = RecordedClient(client, folder, cache)
            registry = DomainPluginRegistry(
                [ReadingPlugin(variant == "compact", variant == "inline")]
            )
            service = AgentRunService(
                isolated, settings=settings, llm=llm, plugin_registry=registry
            )
            worker = KnowledgeWorker(
                isolated,
                ingestion_service=None,
                report_service=None,
                agent_runtime=AgentRuntime(
                    service,
                    checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path),
                ),
                lease_seconds=600,
            )
            current = service.create_run(
                package.project.project_id,
                package.task.task_id,
                AgentRunCreateRequest(
                    context_snapshot_id=package.snapshot_id,
                    workflow="research",
                    max_steps=10,
                    max_tool_calls=3,
                    token_budget=1048576,
                    create_memory_proposal=False,
                ),
            )
            row = {
                "task_id": task_id,
                "variant": variant,
                "run_id": current.id,
                "snapshot_sha256": package.package_sha256,
                "status": "queued",
            }
            rows.append(row)
            write_json(output / "results.json", rows)
            try:
                if not worker.run_once():
                    raise ValueError("Expected queued production job")
            except Exception as exc:
                row["error_type"] = type(exc).__name__
            finally:
                row.update(feedback_trial.archive_run(service, current.id, folder / "archive"))
                row["status"] = service.get_run(current.id).status
                row["paid_calls"] = llm.paid
                row["human_semantic_acceptance"] = "pending"
                write_json(output / "results.json", rows)
            print(f"{task_id}/{variant}: {row['status']}", flush=True)
    return {"output": str(output), "results": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--inline-from", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.execute, args.inline_from), ensure_ascii=False, indent=2))
