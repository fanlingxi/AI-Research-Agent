"""Explicit S2 dev-only feedback revision trial against a read-only A02 backup."""

import argparse
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.feedback_models import (
    FeedbackDecision,
    FeedbackRecheck,
    FeedbackRequest,
    FeedbackRerunRequest,
)
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.benchmarking.live import (
    APPROVAL,
    DATASET,
    POLICY,
    ROOT,
    code_fingerprint,
    isolated_settings,
    write_json,
)
from app.benchmarking.live_budget import BudgetedLLM, SpendingLedger
from app.benchmarking.live_dataset import approved_dataset
from app.config.settings import Settings
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import get_llm_client
from app.worker import KnowledgeWorker

SOURCE = ROOT / "data/evaluation/a02/20260913T091229Z-ecb30fd6"
OUTPUT_ROOT = ROOT / "data/evaluation/s2"
CASES = [
    {"task_id": "evaluation-q1", "category": "incomplete", "finding_index": None,
     "note": "原报告仅覆盖部分研究问题，其余维度未获得充分原文支持。",
     "revision": "逐项处理问题要求的所有维度；每项分别列出本次引用能够支持的定义与结论。"
                 "缺少证据的部分明确标为无法回答，不能借其他段落的引用补足。"},
    {"task_id": "evaluation-q8", "category": "execution", "finding_index": None,
     "note": "原运行在ResearchPlan JSON解析时失败，未产出报告。",
     "revision": "严格遵守系统提供的JSON schema，规划步骤使用要求的枚举及字段。"
                 "最终按问题逐项报告有证据支持的内容，缺证据处明确说明不足。"},
    {"task_id": "attribution-q12", "category": "unsupported", "finding_index": 2,
     "note": "A05监督审核发现原结论第二条引用不能支持所列的全部研究方向。",
     "revision": "每条作者报告须逐项由该条自己的引用直接支持；将复合结论拆开核验。"
                 "引用未列出的研究方向不要写成作者已报告的事实，也不能用相邻结论的引用替代。"
                 "继续遵守原问题的资料范围，区分作者报告与自己的推论。"},
]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup_archive(source: Path, target: Path):
    if target.exists() or source.resolve() == target.resolve():
        raise ValueError("A trial requires a new, separate database")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)


def archive_run(service, run_id, directory):
    run = service.get_run(run_id)
    snapshot = service.context_builder.snapshot_repository.get(run.context_snapshot_id)
    write_json(directory / "run.json", run.model_dump(mode="json"))
    write_json(directory / "snapshot.json", snapshot.model_dump(mode="json"))
    write_json(directory / "events.json", [
        e.model_dump(mode="json") for e in service.repository.list_events(run_id)
    ])
    try:
        output = service.repository.get_output(run_id)
    except KeyError:
        output = None
    if output:
        write_json(directory / "output.json", output.model_dump(mode="json"))
        (directory / "report.md").write_text(output.rendered_text, encoding="utf-8")
    return {"run_id": run.id, "status": run.status, "snapshot_sha256": snapshot.package_sha256,
            "output_sha256": output.output_sha256 if output else None,
            "context_token_budget": snapshot.token_usage.budget,
            "run_token_budget": run.token_budget}


def revise(service, case, parent_id):
    parent = service.get_run(parent_id)
    evidence = []
    if case["finding_index"] is not None:
        finding = service.repository.get_output(parent_id).structured["draft"]["findings"][
            case["finding_index"] - 1]
        evidence = finding["evidence_ids"]
    feedback = service.feedback.create(parent_id, FeedbackRequest(
        idempotency_key="s2-issue-v1", category=case["category"], note=case["note"],
        reporter="Codex辅助诊断；最终效果待用户核验", finding_index=case["finding_index"],
        evidence_ids=evidence,
    ))
    feedback = service.feedback.decide(parent_id, feedback["id"], FeedbackDecision(
        expected_revision=feedback["revision"], decision="accepted",
        reviewer="Codex辅助诊断", note="依据原失败记录或已归档监督审核受理，进行一次修订对照。",
    ))
    memory = service.knowledge_repository.memory_repository
    task = memory.get_workspace_task(parent.task_id)
    memory.update_workspace_task(task.id, expected_revision=task.revision,
                                 goal=task.goal + "\n\n本次修订要求：" + case["revision"])
    child = service.feedback.rerun(parent_id, feedback["id"], FeedbackRerunRequest(
        idempotency_key="s2-revision-v1", expected_revision=feedback["revision"],
        resolution_note=case["revision"],
    ))
    return child


def authorized_live(policy):
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Paid execution is disabled in offline tests")
    live = Settings()
    age = datetime.now(UTC).date() - datetime.fromisoformat(policy["verified_at"]).date()
    if not 0 <= age.days <= 1:
        raise ValueError("Verify official prices before executing")
    if (live.llm_provider != "deepseek" or not live.deepseek_api_key
            or live.deepseek_model != policy["request_model"]
            or live.deepseek_base_url.rstrip("/") != "https://api.deepseek.com"):
        raise ValueError("Expected the authorized official DeepSeek configuration")
    return live


def run(execute=False):
    _, tasks, approval = approved_dataset(DATASET, APPROVAL)
    dev = {t.id for t in tasks if t.split == "dev"}
    if not {c["task_id"] for c in CASES}.issubset(dev):
        raise ValueError("Only approved development cases are allowed")
    policy = read(POLICY)
    if not execute:
        return {"mode": "dry_run", "cases": CASES, "max_calls": 12,
                "max_cost_upper_cny": 4.718592, "source": str(SOURCE)}
    live = authorized_live(policy)
    ledger = SpendingLedger(ROOT / "data/evaluation/a02/cumulative-budget.sqlite", policy)
    before = ledger.report()
    if before["stopped_for_unknown"]:
        raise ValueError("Reconcile unknown usage before executing")
    output = OUTPUT_ROOT / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    settings = isolated_settings(output, live)
    protected = [SOURCE / "knowledge.db", SOURCE / "summary.json"]
    production = Path(live.knowledge_db_path).resolve()
    if production.exists():
        protected.append(production)
    fingerprints = {str(p): digest(p) for p in protected}
    backup_archive(SOURCE / "knowledge.db", Path(settings.knowledge_db_path))
    repo = KnowledgeRepository(settings.knowledge_db_path)
    with repo._connect() as connection:
        if connection.execute(
            "SELECT 1 FROM knowledge_jobs WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone():
            raise ValueError("Historical pending jobs require explicit isolation")
    delegate = get_llm_client(settings)
    delegate.max_tokens = policy["max_output_tokens"]
    delegate.max_retries = 0
    delegate.timeout = policy["request_timeout_seconds"]
    delegate.thinking_enabled = False
    llm = BudgetedLLM(delegate, ledger)
    service = AgentRunService(repo, settings=settings, llm=llm)
    runtime = AgentRuntime(service, checkpoint_factory=AgentCheckpointFactory(
        settings.agent_checkpoint_path))
    worker = KnowledgeWorker(repo, ingestion_service=None, report_service=None,
                             agent_runtime=runtime, lease_seconds=180)
    rows = {r["task_id"]: r for r in read(SOURCE / "summary.json")["results"]}
    results = [{"task_id": c["task_id"], "status": "not_attempted"} for c in CASES]
    config = {"version": "s2-feedback-trial-v1", "cases": CASES, "approval": approval,
              "policy": policy, "code": code_fingerprint(), "protected": fingerprints,
              "max_calls": 12, "max_cost_upper_cny": 4.718592,
              "limitations": ["One selected dev attempt per case; no causal quality estimate.",
                              "Current production snapshot capacity may differ from baseline.",
                              "Human rechecks remain pending; no automatic knowledge promotion."]}
    write_json(output / "config.json", config)
    write_json(output / "budget-before.json", before)
    print(f"S2 output: {output}", flush=True)
    try:
        for index, case in enumerate(CASES):
            if ledger.report()["stopped_for_unknown"]:
                break
            result = results[index]
            result["status"] = "preparing"
            folder = output / case["task_id"]
            parent_id = rows[case["task_id"]]["run_id"]
            child = None
            started = time.monotonic()
            try:
                result["original"] = archive_run(service, parent_id, folder / "original")
                expected = rows[case["task_id"]]["snapshot_sha256"]
                if result["original"]["snapshot_sha256"] != expected:
                    raise ValueError("Historical snapshot identity mismatch")
                child = revise(service, case, parent_id)
                llm.begin_attempt(f"s2/{output.name}/{case['task_id']}")
                llm.audit_directory = folder / "model-responses"
                write_json(output / "results.json", results)
                if not worker.run_once():
                    raise ValueError("Expected queued revision job")
                result["status"] = service.get_run(child.id).status
            except Exception as exc:
                result.update(status="exception", error_type=type(exc).__name__)
            finally:
                if child:
                    result["revised"] = archive_run(service, child.id, folder / "revised")
                write_json(folder / "history.json", service.feedback.list(parent_id))
                current = archive_run(service, parent_id, folder / "original-after")
                if current != result.get("original"):
                    raise ValueError("Original run output or snapshot changed")
                result.update(elapsed_seconds=round(time.monotonic() - started, 3),
                              human_recheck="pending", original_preserved=True)
                write_json(output / "results.json", results)
            print(f"{case['task_id']}: {result['status']}", flush=True)
    finally:
        after = ledger.report()
        write_json(output / "budget-after.json", after)
        write_json(output / "isolation.json", {
            "protected_unchanged": {p: digest(Path(p)) == sha for p, sha in fingerprints.items()},
            "new_calls": len(after["calls"]) - len(before["calls"]),
            "new_cost_upper_cny": round(
                after["known_cost_upper_cny"] - before["known_cost_upper_cny"], 6),
        })
    return {"output": str(output), "results": results}


def followup(prior: Path, execute=False):
    """One explicit capacity-only retry after a recorded token-exhaustion attempt."""
    prior = prior.resolve()
    if (not prior.is_relative_to(OUTPUT_ROOT.resolve())
            or read(prior / "config.json")["cases"] != CASES):
        raise ValueError("Expected this managed S2 trial")
    previous = read(prior / "results.json")
    if [r["task_id"] for r in previous] != [c["task_id"] for c in CASES]:
        raise ValueError("Unexpected trial cases")
    if not execute:
        return {"mode": "dry_run", "max_calls": 12, "token_budget": 131072}
    policy = read(POLICY)
    live = authorized_live(policy)
    ledger = SpendingLedger(ROOT / "data/evaluation/a02/cumulative-budget.sqlite", policy)
    before = ledger.report()
    if before["stopped_for_unknown"]:
        raise ValueError("Reconcile unknown usage before executing")
    output = prior / "capacity-followup"
    output.mkdir(exist_ok=False)
    settings = isolated_settings(prior, live)
    repo = KnowledgeRepository(settings.knowledge_db_path)
    delegate = get_llm_client(settings)
    delegate.max_tokens, delegate.max_retries = policy["max_output_tokens"], 0
    delegate.timeout, delegate.thinking_enabled = policy["request_timeout_seconds"], False
    llm = BudgetedLLM(delegate, ledger)
    service = AgentRunService(repo, settings=settings, llm=llm)
    worker = KnowledgeWorker(repo, ingestion_service=None, report_service=None,
                             agent_runtime=AgentRuntime(service, checkpoint_factory=
                                 AgentCheckpointFactory(settings.agent_checkpoint_path)),
                             lease_seconds=180)
    write_json(output / "config.json", {"version": "s2-capacity-followup-v1",
               "code": code_fingerprint(), "max_calls": 12, "max_cost_upper_cny": 4.718592,
               "token_budget": 131072, "policy": policy,
               "change": "Only explicit total run budget; same revised task, scope and strategy."})
    results = [{"task_id": c["task_id"], "status": "not_attempted"} for c in CASES]
    write_json(output / "budget-before.json", before)
    print(f"S2 followup: {output}", flush=True)
    try:
        for index, old in enumerate(previous):
            if ledger.report()["stopped_for_unknown"]:
                break
            result = results[index]
            parent_id = old["original"]["run_id"]
            failed = service.get_run(old["revised"]["run_id"])
            if (failed.status != "failed"
                    or failed.error_message != "AgentRun has exhausted its token budget."):
                raise ValueError("Only the recorded exhausted attempt may be retried here")
            history = service.feedback.list(parent_id)
            link = next(x for x in history["links"] if x["child_run_id"] == failed.id)
            service.feedback.recheck(parent_id, link["id"], FeedbackRecheck(
                decision="unresolved", reviewer="Codex工程复检",
                note="运行因总Token上限耗尽而失败、无正式报告；仅确认客观执行失败，语义未评分。",
            ))
            feedback = next(x for x in service.feedback.list(parent_id)["feedback"]
                            if x["id"] == link["feedback_id"])
            child = service.feedback.rerun(parent_id, feedback["id"], FeedbackRerunRequest(
                idempotency_key="s2-capacity-followup-v1", expected_revision=feedback["revision"],
                resolution_note="任务要求保持首轮修订，仅将新运行总Token上限由16000调整为131072。",
                token_budget=131072,
            ))
            result.update(status="queued", run_id=child.id, parent_run_id=parent_id)
            write_json(output / "results.json", results)
            folder = output / old["task_id"]
            try:
                llm.begin_attempt(f"s2/{prior.name}/capacity-followup/{old['task_id']}")
                llm.audit_directory = folder / "model-responses"
                if not worker.run_once():
                    raise ValueError("Expected revision job")
                result["status"] = service.get_run(child.id).status
            finally:
                result["revised"] = archive_run(service, child.id, folder / "revised")
                current = archive_run(service, parent_id, folder / "original-after")
                if current != old["original"]:
                    raise ValueError("Original run changed")
                write_json(folder / "history.json", service.feedback.list(parent_id))
                result["human_recheck"] = "pending"
                write_json(output / "results.json", results)
            print(f"{old['task_id']}: {result['status']}", flush=True)
    finally:
        after = ledger.report()
        write_json(output / "budget-after.json", after)
        write_json(output / "isolation.json", {"protected_unchanged": {
            p: digest(Path(p)) == sha for p, sha in read(prior / "config.json")["protected"].items()
        }, "new_calls": len(after["calls"]) - len(before["calls"]),
            "new_cost_upper_cny": round(
                after["known_cost_upper_cny"] - before["known_cost_upper_cny"], 6)})
    return {"output": str(output), "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--capacity-followup", type=Path)
    args = parser.parse_args()
    result = (followup(args.capacity_followup, args.execute)
              if args.capacity_followup else run(args.execute))
    print(json.dumps(result, ensure_ascii=False, indent=2))
