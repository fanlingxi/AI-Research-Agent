"""S2 development-only context diagnosis and explicit real-Worker capacity trial.

Default: copy the archived database, preview existing strategies, and trace spans.
--execute additionally creates three new feedback runs; it never resumes old checkpoints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.feedback_models import FeedbackDecision, FeedbackRequest, FeedbackRerunRequest
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.benchmarking.feedback_trial import (
    CASES,
    archive_run,
    authorized_live,
    backup_archive,
    digest,
    read,
)
from app.benchmarking.live import (
    APPROVAL,
    DATASET,
    ROOT,
    code_fingerprint,
    isolated_settings,
    write_json,
)
from app.benchmarking.live_budget import BudgetedLLM, SpendingLedger
from app.benchmarking.live_dataset import approved_dataset
from app.context.models import ContextBuildRequest, ContextPackage, canonical_package_sha256
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import get_llm_client
from app.retrieval.ranking import query_terms
from app.worker import KnowledgeWorker

PRIOR = ROOT / "data/evaluation/s2/20260913T154436Z-d781f13e"
POLICY = ROOT / "docs/improvement/s2-expansion/run-policy.json"
SOURCE_MAP = ROOT / "data/evaluation/a02/paper-seed-v1/source-map.json"
CAPACITIES = (16000, 65536, 262144)
FOCUSED_REVISION = (
    "直接回答原问题，正文以问题要求的维度或阶段组织，不扩展罗列无关实验结果。"
    "总计最多12条findings，每条只表达其自身claim bundle中的原文能够支持的事实；"
    "跨片段事实拆成多条分别引用，不把其他片段支持的内容合并到单条finding。"
    "正文逐条呈现这些findings并包含每条的全部[cite:<evidence_id>]，不要遗漏引用。"
    "证据不足时明确说明，背景不能充当缺失阶段。正文不超过1500中文字。"
)


def preview_capacities(builder, package):
    """Compare current previews without pretending to replay an external projection."""
    if canonical_package_sha256(package) != package.package_sha256:
        raise ValueError("Archived snapshot integrity mismatch")
    audit = package.retrieval_audit
    if (audit is None or audit.strategy_id != "project-context-legacy-v1"
            or audit.parameters.get("vector_enabled") or audit.parameters.get("graph_enabled")):
        raise ValueError("This comparison requires an archived structured-only legacy snapshot")
    with builder.database.connect() as db:
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        current = builder._read_inputs_tx(db, ContextBuildRequest(task_id=package.task.task_id))
        if (current.task.revision != package.task.revision
                or current.memory.project.revision != package.project.revision):
            raise ValueError("Task or project changed since the archived snapshot")
    previews = []
    for strategy in ("legacy", "bm25-v1"):
        for capacity in CAPACITIES:
            previews.append(builder.preview_context(ContextBuildRequest(
                task_id=package.task.task_id, project_id=package.project.project_id,
                max_tokens=capacity, retrieval_strategy=strategy,
            )))
    return previews


def span_trace(package, mapping, spans):
    """Measure interval union after selection; expected answers never enter a preview."""
    traces = []
    for target in spans:
        pieces = []
        for selection in package.retrieval_audit.selections:
            for source in selection.sources:
                unit = mapping.get(source.chunk_id)
                if unit is None:
                    raise ValueError("Unmapped source chunk")
                if (unit["text_sha256"] != source.chunk_sha256
                        or unit["source_version"] != source.source_version
                        or unit["claim_id"] != selection.item_id):
                    raise ValueError("Source map version or identity mismatch")
                if (unit["source_id"] == target["source_id"]
                        and unit["source_version"] != target["source_version"]):
                    raise ValueError("Diagnostic target version mismatch")
                if (unit["source_id"] == target["source_id"]
                        and max(unit["start"], target["start"]) < min(unit["end"], target["end"])):
                    pieces.append({
                        "chunk_id": source.chunk_id, "start": unit["start"], "end": unit["end"],
                        "rank": selection.rank, "score": selection.score,
                        "selected": selection.selected, "reason": selection.reason,
                    })
        cursor = target["start"]
        for piece in sorted((p for p in pieces if p["selected"]), key=lambda p: p["start"]):
            if piece["start"] <= cursor:
                cursor = max(cursor, piece["end"])
        traces.append({"span_id": target["id"], "source_id": target["source_id"],
                       "start": target["start"], "end": target["end"],
                       "fully_covered": cursor >= target["end"], "pieces": pieces})
    return {"strategy": package.retrieval_audit.strategy_id,
            "capacity": package.token_usage.budget, "used": package.token_usage.used,
            "candidate_count": package.diagnostics.verified_claim_bundles,
            "selected_count": len(package.knowledge.claim_bundles), "spans": traces,
            "covered_spans": sum(t["fully_covered"] for t in traces),
            "required_spans": len(traces)}


def run(*, execute=False, output: Path | None = None, followup: Path | None = None):
    corpus, tasks, _ = approved_dataset(DATASET, APPROVAL)
    dev = {t.id: t for t in tasks if t.split == "dev"}
    if not {c["task_id"] for c in CASES}.issubset(dev):
        raise ValueError("Only approved development cases are allowed")
    policy = read(POLICY)
    # Check the paid entry before any copied-data side effects in offline tests.
    live = authorized_live(policy) if execute else None
    prior = followup.resolve() if followup else PRIOR
    if followup and not prior.is_relative_to((ROOT / "data/evaluation/s2-expansion").resolve()):
        raise ValueError("Follow-up requires a managed context expansion trial")
    archive = prior if followup else prior / "capacity-followup"
    previous = read(archive / "results.json")
    if followup:
        if read(prior / "config.json")["version"] != "s2-context-expansion-v1":
            raise ValueError("Unexpected follow-up archive")
        previous = [r for r in previous if r["status"] in {"failed", "needs_review"}]
    expected_cases = [c["task_id"] for c in CASES]
    selected_cases = [r["task_id"] for r in previous]
    if (not previous or len(set(selected_cases)) != len(selected_cases)
            or any(c not in expected_cases for c in selected_cases)
            or (not followup and selected_cases != expected_cases)):
        raise ValueError("Unexpected trial cases")
    output = output or ROOT / "data/evaluation/s2-expansion" / uuid4().hex
    output = output.resolve()
    if not output.is_relative_to((ROOT / "data/evaluation/s2-expansion").resolve()):
        raise ValueError("Use a new managed evaluation directory")
    output.mkdir(parents=True, exist_ok=False)
    protected = {str(p): digest(p) for p in (prior / "knowledge.db", SOURCE_MAP,
                 archive / "results.json")}
    protected.update(read(prior / "config.json")["protected"])
    backup_archive(prior / "knowledge.db", output / "knowledge.db")
    repo = KnowledgeRepository(str(output / "knowledge.db"))
    builder = ContextBuilderService(repo)
    mapping = read(SOURCE_MAP)["chunks"]
    manifest = read(SOURCE_MAP.parent / "manifest.json")
    if digest(SOURCE_MAP) != manifest["files"]["source-map.json"]:
        raise ValueError("Sealed source mapping changed")
    papers = {p.id: p for p in corpus.sources}
    for unit in mapping.values():
        paper = papers[unit["source_id"]]
        if (paper.version != unit["source_version"] or not 0 <= unit["start"] < unit["end"]
                <= len(paper.text) or hashlib.sha256(
                    paper.text[unit["start"]:unit["end"]].encode()).hexdigest()
                != unit["text_sha256"]):
            raise ValueError("Source mapping does not locate the frozen text")
    write_json(output / "config.json", {"version": "s2-context-expansion-v1", "policy": policy,
               "code": code_fingerprint(), "protected": protected, "execute": execute,
               "max_calls_this_trial": 4 * len(previous), "cases": selected_cases,
               "followup": str(followup) if followup else None,
               "task_refinement": FOCUSED_REVISION if followup else None,
               "change": ("Focused task requirements and larger byte reservation; same scope."
                          if followup else "Explicit capacity; same tasks and legacy ranking."),
               "limitations": ["Selected dev cases; not holdout or a causal quality estimate.",
                               "Human semantics pending; interval coverage is not correctness."]})
    diagnostics = []
    for row in previous:
        case = row["task_id"]
        path = archive / case / "revised/snapshot.json"
        old = ContextPackage.model_validate_json(path.read_bytes())
        stored = builder.snapshot_repository.get(old.snapshot_id)
        if stored.package_sha256 != row["revised"]["snapshot_sha256"]:
            raise ValueError("Trial result identity mismatch")
        if stored.package_sha256 != old.package_sha256:
            raise ValueError("Archive differs from stored snapshot")
        previews = preview_capacities(builder, old)
        # Resolve diagnostic targets only AFTER model-free production previews.
        task = dev[case]
        required = set(task.expectation.required_evidence_span_ids)
        targets = [s.model_dump() for s in corpus.spans if s.id in required]
        if any(t["source_id"] not in task.allowed_source_ids for t in targets):
            raise ValueError("Diagnostic target is outside task scope")
        query = "\n".join([old.task.title, old.task.goal, old.project.goal, old.project.domain])
        diagnostics.append({"task_id": case, "parent_run_id": row["run_id"],
                            "query_terms": query_terms(query),
                            "historical_inputs_match": old.retrieval_audit.parameters.get(
                                "input_sha256") == previews[0].retrieval_audit.parameters.get(
                                    "input_sha256"),
                            "historical_selection_reproduced": {
                                b.claim.claim_id for b in old.knowledge.claim_bundles
                            } == {b.claim.claim_id for b in previews[0].knowledge.claim_bundles},
                            "original": span_trace(old, mapping, targets),
                            "previews": [span_trace(p, mapping, targets) for p in previews]})
    write_json(output / "diagnosis.json", diagnostics)
    print(f"S2 expansion output: {output}", flush=True)
    results = [{"task_id": r["task_id"], "status": "not_attempted"} for r in previous]
    write_json(output / "results.json", results)
    ledger = None
    try:
        if execute:
            ledger = SpendingLedger(ROOT / "data/evaluation/a02/cumulative-budget.sqlite", policy)
            write_json(output / "budget-before.json", ledger.report())
            settings = isolated_settings(output, live)
            delegate = get_llm_client(settings)
            delegate.max_tokens, delegate.max_retries = policy["max_output_tokens"], 0
            delegate.timeout, delegate.thinking_enabled = policy["request_timeout_seconds"], False
            llm = BudgetedLLM(delegate, ledger)
            service = AgentRunService(repo, settings=settings, llm=llm)
            worker = KnowledgeWorker(repo, ingestion_service=None, report_service=None,
                agent_runtime=AgentRuntime(service, checkpoint_factory=AgentCheckpointFactory(
                    settings.agent_checkpoint_path)), lease_seconds=600)
            with repo.database.connect() as db:
                if db.execute("SELECT 1 FROM knowledge_jobs WHERE status IN ('queued','running')"
                              ).fetchone():
                    raise ValueError("Historical pending jobs require isolation")
            for row, result in zip(previous, results, strict=True):
                result["status"] = "preparing"
                write_json(output / "results.json", results)
                parent = row["run_id"]
                folder = output / row["task_id"]
                original = archive_run(service, parent, folder / "original")
                if followup:
                    task_id = service.get_run(parent).task_id
                    memory = repo.memory_repository
                    task = memory.get_workspace_task(task_id)
                    memory.update_workspace_task(task_id, expected_revision=task.revision,
                        goal=task.goal + "\n\n本次聚焦要求：" + FOCUSED_REVISION)
                feedback = service.feedback.create(parent, FeedbackRequest(
                    idempotency_key="context-expansion-v1", category="incomplete",
                    reporter="Codex辅助诊断；语义待用户验收",
                    note=("上次运行受输入大小或引用校验阻断；保持范围及容量，聚焦原问题和逐条引用。"
                          if followup else "原快照遗漏关键原文或运行超时；扩大新快照和运行容量。")))
                feedback = service.feedback.decide(parent, feedback["id"], FeedbackDecision(
                    expected_revision=feedback["revision"], decision="accepted",
                    reviewer="Codex辅助诊断", note="依据原失败及离线证据追踪，受理扩容试验。"))
                child = service.feedback.rerun(parent, feedback["id"], FeedbackRerunRequest(
                    expected_revision=feedback["revision"], idempotency_key="context-expansion-v1",
                    resolution_note=(FOCUSED_REVISION if followup
                        else "保持任务、范围和legacy排序，显式扩大新快照及运行Token上限。"),
                    token_budget=policy["run_token_budget"],
                    context_max_tokens=policy["context_max_tokens"]))
                result.update(status="queued", run_id=child.id, parent_run_id=parent)
                write_json(output / "results.json", results)
                try:
                    llm.begin_attempt(f"s2-expansion/{output.name}/{row['task_id']}")
                    llm.audit_directory = folder / "model-responses"
                    if not worker.run_once():
                        raise ValueError("Expected queued revision job")
                    result["status"] = service.get_run(child.id).status
                finally:
                    result["revised"] = archive_run(service, child.id, folder / "revised")
                    if archive_run(service, parent, folder / "original-after") != original:
                        raise ValueError("Original run output or snapshot changed")
                    result["human_recheck"] = "pending"
                    write_json(folder / "history.json", service.feedback.list(parent))
                    write_json(output / "results.json", results)
                print(f"{row['task_id']}: {result['status']}", flush=True)
    except Exception as exc:
        for result in results:
            if result["status"] in {"preparing", "queued"}:
                result.update(status="exception", error_type=type(exc).__name__)
        write_json(output / "results.json", results)
        raise
    finally:
        if ledger:
            write_json(output / "budget-after.json", ledger.report())
        unchanged = {p: digest(Path(p)) == sha for p, sha in protected.items()}
        write_json(output / "isolation.json", {"protected_unchanged": unchanged})
        if not all(unchanged.values()):
            raise ValueError("Protected archive changed")
    return {"output": str(output), "mode": "live" if execute else "offline",
            "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--followup", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(execute=args.execute, output=args.output, followup=args.followup),
                     ensure_ascii=False, indent=2))
