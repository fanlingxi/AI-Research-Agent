"""Explicit A05 archive observation entry. Prepare is offline; --execute spends budget."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agent.repository import _sha256 as output_fingerprint
from app.agent.research_workflow import ResearchDraft
from app.agent.semantic_review import SYSTEM, VERSION, SemanticReviewService, fingerprint
from app.benchmarking.experiments import ExperimentReadService
from app.benchmarking.live import EVALUATION_ROOT, POLICY, ROOT, code_fingerprint, write_json
from app.benchmarking.live_budget import BudgetedLLM, SpendingLedger
from app.benchmarking.semantic_calibration import annotation_pack, calibrate
from app.config.settings import Settings
from app.context.models import ContextPackage
from app.llms.provider import get_llm_client

DEFAULT_EXPERIMENT = "a02--20260913T091229Z-ecb30fd6"
DEFAULT_TASKS = ["evaluation-q1", "evaluation-q12", "attribution-q12"]


def load_case(reader, experiment_id, task_id):
    kind, directory, _, _, _, tasks, rows = reader._load(experiment_id)
    if kind != "a02" or task_id not in tasks or tasks[task_id]["split"] != "dev":
        raise ValueError("A05 pilot only accepts existing A02 development tasks")
    row = next((r for r in rows if r["task_id"] == task_id), None)
    if row is None:
        raise ValueError("Task is not part of this experiment")
    if row.get("status") != "completed":
        return {
            "task_id": task_id,
            "status": "source_not_completed",
            "source_status": row.get("status"),
        }, None
    folder = directory / row["folder"]
    files = {
        name: reader._read(reader.root, folder / name)
        for name in ("run.json", "output.json", "snapshot.json")
    }
    run, output = files["run.json"], files["output.json"]
    package = ContextPackage.model_validate(files["snapshot.json"])
    expected = row.get("snapshot_sha256")
    if (
        run["context_snapshot_id"] != package.snapshot_id
        or run["context_sha256"] != expected
        or output["run_id"] != run["id"]
        or package.task.task_id != run["task_id"]
        or output["structured"]["context_sha256"] != expected
        or output["structured"]["context_snapshot_id"] != package.snapshot_id
    ):
        raise ValueError("Run/output/snapshot identity mismatch")
    if (
        output_fingerprint({k: output[k] for k in ("output_type", "structured", "rendered_text")})
        != output["output_sha256"]
    ):
        raise ValueError("Output fingerprint mismatch")
    # Reuse the frozen-paper source mapping checks already exercised by A07a.
    if reader.task_detail(experiment_id, task_id)["variants"][0]["issues"]:
        raise ValueError("Frozen source archive could not be verified")
    draft = ResearchDraft.model_validate(output["structured"]["draft"])
    if draft.markdown != output["rendered_text"]:
        raise ValueError("Structured and rendered report differ")
    return {
        "task_id": task_id,
        "status": "prepared",
        "run_id": run["id"],
        "output_id": output["id"],
        "output_sha256": output["output_sha256"],
        "input_files_sha256": fingerprint(files),
    }, (draft, package, expected)


def render_report(records):
    names = {
        "supported": "支持",
        "contradicted": "矛盾",
        "insufficient_evidence": "证据不足",
        "cannot_determine": "无法判断",
    }
    lines = [
        "# A05 证据支持观察",
        "",
        "仅复核结构化结论与其引用，不代表全文正确、回答完整或人工验收。模型判断不改变正式报告。",
        "",
    ]
    for record in records:
        lines += [f"## {record['task_id']} · {record['status']}", ""]
        for finding in record.get("review", {}).get("findings", []):
            lines += [
                f"### {finding['finding_id']} · {names[finding['verdict']]} · {finding['status']}",
                "",
                finding["assertion"],
                "",
                finding["reason"],
                "",
            ]
            for cite in finding["citations"]:
                lines += [
                    f"原文：{cite['title']} · 版本 {cite['source_version']} · "
                    f"页 {cite['page_start']}–{cite['page_end']} · {cite['evidence_id']}",
                    "",
                    "> " + cite["quote"].replace("\n", "\n> "),
                    "",
                ]
    return "\n".join(lines)


def observe(reader, experiment_id, task_ids, output, llm=None, model="not_called"):
    if not 1 <= len(task_ids) <= 3 or len(set(task_ids)) != len(task_ids):
        raise ValueError("Pilot requires one to three distinct tasks")
    # Unique directories only: retry means a new observation, never overwrite old evidence.
    output.mkdir(parents=True, exist_ok=False)
    records = [{"task_id": task, "status": "not_attempted"} for task in task_ids]
    service = SemanticReviewService()
    write_json(
        output / "config.json",
        {
            "version": VERSION,
            "experiment_id": experiment_id,
            "tasks": task_ids,
            "model": model,
            "execute": llm is not None,
            "system_sha256": fingerprint(SYSTEM),
            "code": code_fingerprint(),
            "max_model_calls": len(task_ids),
            "mode": "observation_only",
        },
    )
    write_json(output / "results.json", records)
    try:
        for index, task_id in enumerate(task_ids):
            try:
                record, inputs = load_case(reader, experiment_id, task_id)
                records[index] = record
                if inputs is not None:
                    if llm is not None:
                        if isinstance(llm, BudgetedLLM):
                            if llm.ledger.report()["stopped_for_unknown"]:
                                record["status"] = "budget_blocked"
                                continue
                            record["attempt_id"] = f"a05/{output.name}/{task_id}"
                            llm.begin_attempt(record["attempt_id"])
                            llm.audit_directory = output / task_id / "responses"
                        record["review"] = service.prepare(*inputs)
                        record["status"] = "reviewing"
                        write_json(output / "results.json", records)
                        review = service.review(*inputs, llm, model=model)
                    else:
                        review = service.prepare(*inputs)
                    after, _ = load_case(reader, experiment_id, task_id)
                    if after.get("input_files_sha256") != record["input_files_sha256"]:
                        record["status"] = "input_changed"
                        record.pop("review", None)
                        write_json(output / task_id / "rejected-review.json", review)
                    else:
                        record.update(review=review, status=review["status"])
            except Exception as exc:
                records[index]["status"] = "input_failed"
                records[index]["error"] = type(exc).__name__
            finally:
                write_json(output / "results.json", records)
    finally:
        labels = annotation_pack(records)
        write_json(output / "human-labels.json", labels)
        write_json(output / "calibration.json", calibrate(records, labels))
        if isinstance(llm, BudgetedLLM):
            budget = llm.ledger.report()
            for record in records:
                calls = [c for c in budget["calls"] if c["attempt_id"] == record.get("attempt_id")]
                record["model_calls"] = len(calls)
                record["actual_cost_cny"] = None
                record["cost_upper_cny"] = (
                    sum(c["cost_upper_micro_cny"] for c in calls) / 1_000_000
                    if calls and all(c["status"] == "settled" for c in calls)
                    else None
                )
            write_json(output / "results.json", records)
            write_json(output / "budget.json", budget)
        (output / "review.md").write_text(render_report(records), encoding="utf-8")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--tasks", nargs="+", default=DEFAULT_TASKS)
    parser.add_argument(
        "--execute", action="store_true", help="Explicit budgeted model observation (max 3 calls)"
    )
    parser.add_argument("--calibrate", type=Path, help="Existing A05 results.json; no model call")
    parser.add_argument("--labels", type=Path)
    args = parser.parse_args()
    if args.calibrate:
        if not args.labels or args.execute:
            parser.error("Calibration requires --labels and cannot execute models")
        records = json.loads(args.calibrate.read_text(encoding="utf-8"))
        labels = json.loads(args.labels.read_text(encoding="utf-8"))
        result = calibrate(records, labels)
        path = args.calibrate.parent / f"calibration-{uuid4().hex[:8]}.json"
        write_json(path, result)
        print(path)
        return
    llm, model = None, "not_called"
    if args.execute:
        if not 1 <= len(args.tasks) <= 3 or len(set(args.tasks)) != len(args.tasks):
            parser.error("At most three distinct development tasks per pilot")
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        settings = Settings()
        if (
            settings.llm_provider != "deepseek"
            or not settings.deepseek_api_key
            or settings.deepseek_model != policy["request_model"]
            or settings.deepseek_base_url.rstrip("/") != "https://api.deepseek.com"
        ):
            raise ValueError("Authorized official model configuration required")
        delegate = get_llm_client(settings)
        delegate.max_tokens, delegate.max_retries = 4096, 0
        delegate.timeout, delegate.thinking_enabled = 60, False
        delegate.temperature = 0
        ledger = SpendingLedger(EVALUATION_ROOT / "cumulative-budget.sqlite", policy)
        if ledger.report()["stopped_for_unknown"]:
            raise ValueError("Unknown earlier usage blocks new model calls")
        llm, model = BudgetedLLM(delegate, ledger), policy["request_model"]
    output = (
        ROOT
        / "data/evaluation/a05"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    )
    records = observe(ExperimentReadService(), args.experiment, args.tasks, output, llm, model)
    print(json.dumps({"output": str(output), "statuses": [r["status"] for r in records]}))


if __name__ == "__main__":
    main()
