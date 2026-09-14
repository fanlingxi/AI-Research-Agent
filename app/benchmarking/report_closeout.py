"""One bounded feedback revision for three approved dev reports, in new database copies."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.feedback_models import FeedbackDecision, FeedbackRequest, FeedbackRerunRequest
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
from app.benchmarking.live_dataset import approved_dataset
from app.benchmarking.reading_trial import CASES, RecordedClient
from app.config.settings import Settings
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LangChainChatClient, get_llm_client
from app.worker import KnowledgeWorker

CORRECTIONS = {
    "evaluation-q1": (
        "只直接回答原问题的三个维度及各自检查对象，控制在3至4条简短结论；"
        "不展开公式、提示原文、API、模型型号或实验数字。"
        "每条只包含本条引用完整支持的定义，不能借另一条的引用补足。"
        "不重复概述，不列未被询问的实现缺失清单。"
    ),
    "evaluation-q8": (
        "只直接回答原问题，用4至5条简短结论组织：输入概述至多一次、"
        "原文明确的第一至第三阶段按顺序说明，必要时再用一条说明置信区间的机制。"
        "不要把输入当阶段，不在同一条中重复后续阶段；阶段细节只引用完整支持该条的原文。"
        "需要续文时从本次已审核证据选择相应片段；引用跨多个bundle时分条，不能借引。"
        "略去原问题不要求的数学公式、实验比较与95% alpha等含糊符号。"
        "不因未被询问的公式或超参数缺失而称原问题无法完整回答。"
    ),
    "attribution-q12": (
        "继续遵守原允许范围；如无法回答，仅在证据局限中用一句自然中文说明："
        "所需原文不在本次允许范围，无法据现有允许材料确定请求的数值。"
        "不要输出无关论文背景、内部权限字段、项目ID、论文全文不存在某事实的断言，"
        "也不要给权限说明添加论文引用。不虚构任何样本数。"
    ),
}


def run(prior, execute=False):
    if not execute:
        return {"mode": "dry_run", "cases": CASES, "new_runs": 3, "max_model_calls": 12}
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_LIVE_LLM_INTEGRATION") == "0":
        raise ValueError("Live calls disabled under offline tests")
    prior = Path(prior).resolve()
    if not prior.is_relative_to((ROOT / "data/evaluation/a08-production").resolve()):
        raise ValueError("Only isolated production validation archives may be revised")
    _, tasks, approval = approved_dataset(DATASET, APPROVAL)
    if not set(CASES) <= {t.id for t in tasks if t.split == "dev"}:
        raise ValueError("Only approved dev cases may be revised")
    parents = {}
    for case in CASES:
        source = prior / case / "live-inline-v1"
        parent = json.loads((source / "archive/run.json").read_text(encoding="utf-8"))
        if parent["status"] != "completed" or parent["plugin"]["workflow_key"] != "research_v3":
            raise ValueError("Expected completed evidence-aligned parent run")
        parents[case] = parent
    protected = {p: feedback_trial.digest(p) for case in CASES
                 for p in (prior / case / "live-inline-v1").rglob("*") if p.is_file()}
    live = Settings()
    client = get_llm_client(live)
    if not isinstance(client, LangChainChatClient):
        raise ValueError("Configured real model required")
    client = replace(client, max_tokens=8192, timeout=120, max_retries=0, thinking_enabled=False)
    output = ROOT / "data/evaluation/a08-production" / (
        f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}-feedback-closeout"
    )
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "config.json", {
        "code": code_fingerprint(), "dataset_freeze_sha256": approval["freeze_sha256"],
        "parent": str(prior), "corrections": CORRECTIONS, "model": client.model,
        "max_live_calls": 12, "context_tokens": 262144, "run_tokens": 1048576,
        "output_tokens": 8192, "timeout_seconds": 120, "sdk_retries": 0,
        "thinking": "disabled", "spending_mode": "observe", "human_recheck": "pending",
        "comparison": "task feedback plus capacity change; not a causal or holdout estimate",
    })
    rows = []
    print(f"Feedback closeout: {output}", flush=True)
    for case in CASES:
        source = prior / case / "live-inline-v1"
        folder = output / case
        folder.mkdir()
        feedback_trial.backup_archive(source / "knowledge.db", folder / "knowledge.db")
        repo = KnowledgeRepository(str(folder / "knowledge.db"))
        settings = isolated_settings(folder, live)
        llm = RecordedClient(client, folder / "generation", {})
        service = AgentRunService(repo, settings=settings, llm=llm)
        parent_id = parents[case]["id"]
        original = feedback_trial.archive_run(service, parent_id, folder / "original")
        feedback = service.feedback.create(parent_id, FeedbackRequest(
            idempotency_key="a08-closeout-feedback-v1", category="other",
            note=CORRECTIONS[case], reporter="Codex辅助复检；新语义结果待用户确认",
        ))
        feedback = service.feedback.decide(parent_id, feedback["id"], FeedbackDecision(
            expected_revision=feedback["revision"], decision="accepted", reviewer="Codex辅助复检",
            note="依据已确认修改要求与新报告观察受理；不将辅助意见标为独立金标。",
        ))
        memory = repo.memory_repository
        task = memory.get_workspace_task(parents[case]["task_id"])
        memory.update_workspace_task(
            task.id, expected_revision=task.revision,
            goal=task.goal + "\n\n本次修订要求：" + CORRECTIONS[case],
        )
        row = {"task_id": case, "parent_run_id": parent_id, "status": "preparing",
               "feedback_id": feedback["id"], "human_recheck": "pending"}
        rows.append(row)
        write_json(output / "results.json", rows)
        child = None
        try:
            child = service.feedback.rerun(parent_id, feedback["id"], FeedbackRerunRequest(
                idempotency_key="a08-closeout-child-v1", expected_revision=feedback["revision"],
                resolution_note=CORRECTIONS[case], token_budget=1048576, context_max_tokens=262144,
            ))
            worker = KnowledgeWorker(
                repo, ingestion_service=None, report_service=None,
                agent_runtime=AgentRuntime(service, checkpoint_factory=AgentCheckpointFactory(
                    settings.agent_checkpoint_path)), lease_seconds=600,
            )
            if not worker.run_once():
                raise ValueError("Expected queued child run")
        except Exception as exc:
            row.update(status="exception", execution_error_type=type(exc).__name__)
        finally:
            if child:
                row.update(feedback_trial.archive_run(service, child.id, folder / "revised"))
            write_json(folder / "history.json", service.feedback.list(parent_id))
            after = feedback_trial.archive_run(service, parent_id, folder / "original-after")
            row["parent_preserved"] = after == original
            write_json(output / "results.json", rows)
            unchanged = {
                str(p): feedback_trial.digest(p) == expected for p, expected in protected.items()
            }
            write_json(output / "isolation.json", {"protected_unchanged": unchanged})
            if after != original or not all(unchanged.values()):
                raise ValueError("Original parent or archive changed")
        print(f"{case}: {row['status']}", flush=True)
    return {"output": str(output), "results": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="prior", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.prior, args.execute), ensure_ascii=False, indent=2))
