"""Frozen new-paper comparison through real Qdrant, ContextBuilder and Worker.

Preparation downloads public PDFs and encodes locally; execution is explicitly
bounded and separate from offline tests. Never tunes or repeats an existing run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.models import AgentRunCreateRequest
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.benchmarking import feedback_trial
from app.benchmarking.passage_dataset import import_passages
from app.benchmarking.reading_trial import RecordedClient
from app.benchmarking.store_validation import settings_for
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.retrieval import QdrantContextCandidateRetriever
from app.context.service import ContextBuilderService
from app.knowledge.projector import QdrantKnowledgeIndexer
from app.knowledge.query import QdrantKnowledgeSearch
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LangChainChatClient, get_llm_client
from app.retrieval.coverage import CoreCoverageReranker
from app.worker import KnowledgeWorker

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data/evaluation/resume-closeout"
CASES = ROOT / "benchmarks/research/closeout-v1/cases.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprints():
    return {
        str(p.relative_to(ROOT)): digest(p)
        for p in sorted((ROOT / "app").rglob("*.py"))
        if "benchmarking" not in p.parts
    }


def guard(variable):
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv(variable) == "0":
        raise ValueError("Real execution disabled in offline tests")


def prepare():
    guard("RUN_STORE_INTEGRATION")
    from pypdf import PdfReader

    cases = read(CASES)
    if len(cases["sources"]) != 5 or len(cases["cases"]) != 20:
        raise ValueError("Expected the frozen five-paper, twenty-question plan")
    output = OUTPUT / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    config = {
        "schema": "resume-closeout-v1",
        "cases_sha256": digest(CASES),
        "production_code": fingerprints(),
        "collection": f"resume_eval_{uuid4().hex}",
        "prepared": False,
        "max_calls": 180,
        "per_run_calls": 4,
        "context_tokens": 16000,
        "run_tokens": 1048576,
        "workflows": ["research_v4", "research_v7"],
        "cases": cases["cases"],
        "human_labels": None,
        "semantic_success_rate": None,
    }
    write(output / "config.json", config)
    print(f"Preparing {output}", flush=True)
    sources = []
    for source in cases["sources"]:
        path = output / "papers" / f"{source['id']}.pdf"
        path.parent.mkdir(exist_ok=True)
        request = urllib.request.Request(
            source["pdf_url"], headers={"User-Agent": "ResearchProject/1.0"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            path.write_bytes(response.read(30_000_000))
        if not path.read_bytes().startswith(b"%PDF"):
            raise ValueError("Source did not return a PDF")
        reader = PdfReader(path)
        text, pages = "", []
        for number, page in enumerate(reader.pages, 1):
            content = (page.extract_text() or "") + "\n"
            pages.append({"start": len(text), "end": len(text) + len(content), "pdf_page": number})
            text += content
        if len(text.strip()) < 1000:
            raise ValueError("Insufficient extracted text; do not silently evaluate an empty paper")
        sources.append(
            {
                **source,
                "pdf_path": str(path.relative_to(output)),
                "pdf_sha256": digest(path),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "pdf_pages": len(pages),
                "pages": pages,
                "text": text,
                "license": "ACL Anthology publication; retained locally for evaluation",
            }
        )
        write(output / "papers" / f"{source['id']}.json", sources[-1])
        print(f"Downloaded/extracted {source['id']}: {len(pages)} pages", flush=True)
    corpus = SimpleNamespace(
        sources=[
            SimpleNamespace(**{**s, "pages": [SimpleNamespace(**p) for p in s["pages"]]})
            for s in sources
        ]
    )
    seed = output / "seed"
    seed.mkdir()
    settings = settings_for(seed, config["collection"])
    repo = KnowledgeRepository(settings.knowledge_db_path)
    mapping = import_passages(repo, corpus, output)
    write(seed / "source-map.json", mapping)
    chunks = repo.list_projection_chunks()
    QdrantKnowledgeIndexer(settings).index(chunks)
    config.update(
        prepared=True,
        chunks=len(chunks),
        sources=[{k: v for k, v in s.items() if k not in {"text", "pages"}} for s in sources],
        seed_files={name: digest(seed / name) for name in ["knowledge.db", "source-map.json"]},
    )
    write(output / "config.json", config)
    print(f"Prepared {len(chunks)} chunks; no LLM calls", flush=True)
    return {"output": str(output), "chunks": len(chunks)}


def execute(output):
    guard("RUN_LIVE_LLM_INTEGRATION")
    guard("RUN_STORE_INTEGRATION")
    output = Path(output).resolve(strict=True)
    if output.parent != OUTPUT.resolve():
        raise ValueError("Execution requires this tool's prepared archive")
    config = read(output / "config.json")
    if not config["prepared"] or config["cases_sha256"] != digest(CASES):
        raise ValueError("Preparation is incomplete or questions changed")
    if config["production_code"] != fingerprints():
        raise ValueError(
            "Candidate changed after freezing; existing set cannot be reused for tuning"
        )
    if (output / "results.json").exists():
        raise ValueError("This evaluation was already attempted; outcomes must not be overwritten")
    live = Settings()
    client = get_llm_client(live)
    if not isinstance(client, LangChainChatClient):
        raise ValueError("Configured real model required")
    client = replace(client, max_tokens=8192, timeout=120, max_retries=0, thinking_enabled=False)
    write(
        output / "execution.json",
        {
            "provider": client.provider_name,
            "model": client.model,
            "max_calls": 180,
            "output_tokens": 8192,
            "timeout": 120,
            "ranking_timeout": 90,
            "sdk_retries": 0,
            "thinking": False,
            "spending": "observe; unknown stays unknown",
        },
    )
    seed = output / "seed"
    protected = {seed / name: value for name, value in config["seed_files"].items()}
    real_db = Path(live.knowledge_db_path).resolve()
    if real_db.exists():
        protected[real_db] = digest(real_db)
    mapping = read(seed / "source-map.json")
    rows = [
        {"case": c["id"], "workflow": w, "status": "not_attempted", "human_review": None}
        for c in config["cases"]
        for w in config["workflows"]
    ]
    write(output / "results.json", rows)
    try:
        for index, case in enumerate(config["cases"]):
            folder = output / case["id"]
            folder.mkdir()
            affected = [r for r in rows if r["case"] == case["id"]]
            try:
                if config["production_code"] != fingerprints():
                    raise ValueError("Candidate changed during validation")
                feedback_trial.backup_archive(seed / "knowledge.db", folder / "knowledge.db")
                settings = settings_for(folder, config["collection"])
                repo = KnowledgeRepository(settings.knowledge_db_path)
                mem = repo.memory_repository
                project = mem.create_project(
                    name=case["id"],
                    goal="仅依据允许论文回答",
                    domain="research",
                    metadata={"evaluation_only": True},
                )
                mem.replace_project_knowledge_scopes(
                    project.id,
                    [mapping["sources"][s]["scope"] for s in case["source_ids"]],
                    expected_project_revision=project.revision,
                )
                task = mem.create_workspace_task(
                    project_id=project.id,
                    title=case["question"][:200],
                    goal=case["question"],
                    priority="high",
                    metadata={"expected_output": "中文直接回答；逐条引用；保留限定语及证据缺口。"},
                )
                ranker = RecordedClient(replace(client, timeout=90), folder / "ranking", {})
                builder = ContextBuilderService(
                    repo,
                    vector_retriever=QdrantContextCandidateRetriever(
                        QdrantKnowledgeSearch(settings)
                    ),
                    coverage_reranker=CoreCoverageReranker(ranker),
                )
                package = builder.build_context(
                    ContextBuildRequest(
                        task_id=task.id,
                        max_tokens=16000,
                        reading_format="inline-v1",
                        enable_vector_candidates=True,
                        retrieval_strategy="dense-v1",
                        evidence_reranking="coverage-v2",
                    )
                )
                if any("vector_unavailable" in n for n in package.diagnostics.notices):
                    raise ValueError("Real vector retrieval unavailable")
                for bundle in package.knowledge.claim_bundles:
                    for chunk in bundle.chunks:
                        entry = mapping["chunks"][chunk.chunk_id]
                        if (
                            entry["source_id"] not in case["source_ids"]
                            or entry["text_sha256"] != chunk.content_sha256
                            or entry["claim_id"] != bundle.claim.claim_id
                        ):
                            raise ValueError("Snapshot identity or scope violation")
                write(folder / "snapshot.json", package.model_dump(mode="json"))
                cache = {}
                for row in affected[:: 1 if index % 2 == 0 else -1]:
                    arm = folder / row["workflow"]
                    arm.mkdir()
                    current, service, recorded = None, None, None
                    started = time.perf_counter()
                    try:
                        feedback_trial.backup_archive(folder / "knowledge.db", arm / "knowledge.db")
                        arm_settings = settings_for(arm, config["collection"])
                        arm_repo = KnowledgeRepository(arm_settings.knowledge_db_path)
                        recorded = RecordedClient(client, arm / "generation", cache)
                        service = AgentRunService(arm_repo, settings=arm_settings, llm=recorded)
                        current = service.create_run(
                            project.id,
                            task.id,
                            AgentRunCreateRequest(
                                context_snapshot_id=package.snapshot_id,
                                workflow=row["workflow"],
                                max_steps=10,
                                max_tool_calls=3,
                                token_budget=1048576,
                                create_memory_proposal=False,
                            ),
                        )
                        row.update(status="queued", run_id=current.id)
                        write(output / "results.json", rows)
                        worker = KnowledgeWorker(
                            arm_repo,
                            ingestion_service=None,
                            report_service=None,
                            agent_runtime=AgentRuntime(
                                service,
                                checkpoint_factory=AgentCheckpointFactory(
                                    arm_settings.agent_checkpoint_path
                                ),
                            ),
                            lease_seconds=600,
                        )
                        if not worker.run_once():
                            raise ValueError("Expected queued research run")
                    except Exception as exc:
                        row.update(status="exception", error_type=type(exc).__name__)
                    finally:
                        if current:
                            row.update(
                                feedback_trial.archive_run(service, current.id, arm / "archive")
                            )
                        row["paid_calls"] = recorded.paid if recorded else 0
                        row["elapsed_seconds"] = time.perf_counter() - started
                        write(output / "results.json", rows)
                    print(f"{case['id']} {row['workflow']}: {row['status']}", flush=True)
            except Exception as exc:
                for row in affected:
                    if row["status"] == "not_attempted":
                        row.update(status="preparation_failed", error_type=type(exc).__name__)
                write(output / "results.json", rows)
                print(f"{case['id']}: preparation failed ({type(exc).__name__})", flush=True)
    finally:
        isolation = {str(p): p.exists() and digest(p) == value for p, value in protected.items()}
        write(output / "isolation.json", isolation)
        if not all(isolation.values()):
            raise ValueError("Protected data changed")
    return {"output": str(output), "runs": len(rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--execute", type=Path)
    args = parser.parse_args()
    result = (
        prepare()
        if args.prepare
        else execute(args.execute)
        if args.execute
        else {"mode": "dry_run", "cases": 20, "runs": 40, "max_model_calls": 180}
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
