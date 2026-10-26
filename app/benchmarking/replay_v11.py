"""Offline v8/v10/v11 replay with exact per-version repair request verification.

Reads verified archive responses only. Never creates a model client or writes to
the archive. An unavailable or mismatching repair request is not an exact result.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from app.agent.research_workflow import _strip_json_fence, validate_research_draft
from app.agent.structured_research import (
    REPORT_SYSTEM_PROMPT,
    AnchoredDirectResearchWorkflow,
    NumericSupportResearchWorkflow,
    SharedEvidenceResearchWorkflow,
    report_error_diagnostic,
)
from app.benchmarking import replay_v10 as archive_tools
from app.benchmarking.holdout_v2_score import packet

VERSIONS = {"v8": AnchoredDirectResearchWorkflow, "v10": SharedEvidenceResearchWorkflow,
            "v11": NumericSupportResearchWorkflow}


def simulate(slots, package, workflow_class):
    workflow = workflow_class.__new__(workflow_class)
    attempted, errors = [], []
    prior_error = None
    base_prompt = slots[0]["request"]["prompt"]
    result = {"status": "needs_review", "answer_status": None, "used_slot": None,
              "attempted_slots": attempted, "markdown": None, "errors": errors,
              "omitted": [], "numeric_audit": None, "exact": True}
    for slot in range(2):
        if slot >= len(slots):
            # Slot 0 failed locally but the archived version needed no repair.
            result.update(status="unavailable_repair", exact=False)
            return result
        call = slots[slot]
        if slot:
            request = {"system_prompt": REPORT_SYSTEM_PROMPT, "prompt": base_prompt + (
                f"\nPrevious attempt invalid: {prior_error}. "
                "Produce a corrected complete object within the limits."
            )}
            expected = archive_tools.request_digest(request)
            recorded = call["prompt_sha256"]
            if expected != recorded:
                result.update(status="inexact_repair", exact=False,
                              repair_request={"expected_sha256": expected,
                                              "archived_sha256": recorded})
                return result
        attempted.append(slot)
        if call["status"] != "returned":
            errors.append({"slot": slot, "reason": "Generation failed or unsettled"})
            return result
        try:
            report = workflow.parse_report(json.loads(_strip_json_fence(call["response"])))
            draft = workflow.prepare_report(report, package)
            validation = validate_research_draft(draft, package)
            if draft.answer_status != "insufficient_evidence" and not validation.passed:
                prior_error = "Citation IDs or bundle ownership are invalid"
            else:
                audit = getattr(draft, "numeric_audit", None)
                result.update(status="completed", answer_status=draft.answer_status,
                              used_slot=slot, markdown=draft.markdown, numeric_audit=audit,
                              omitted=audit["omitted"] if audit else [])
                return result
        except (ValueError, TypeError) as exc:
            prior_error = report_error_diagnostic(exc)
        errors.append({"slot": slot, "reason": prior_error})
    return result


def replay(archive, out, *, tasks_path=None):
    archive, out = Path(archive).resolve(strict=True), Path(out).resolve()
    if out == archive or out.is_relative_to(archive) or archive.is_relative_to(out):
        raise ValueError("Replay output must be separate from the original archive")
    if out.exists():
        raise ValueError("Replay output must be a new directory")
    tasks, labels = archive_tools._tasks(archive, Path(tasks_path) if tasks_path else None)
    baseline_path = archive / "results.json"
    baseline = ({(r["case"], r["workflow"]): r for r in archive_tools.read(baseline_path)}
                if baseline_path.is_file() else None)
    results, packets = [], {}
    # Preserve v10 snapshot, call, prompt, run-set and frozen-label verification.
    # Verify everything before creating output so corruption leaves no partial run.
    for case in sorted(archive.iterdir()):
        arms = sorted(p.parent.parent for p in case.glob("*/generation/calls.json"))
        snapshot_arms = {p.stem.removeprefix("snapshot-") for p in case.glob("snapshot-*.json")}
        if snapshot_arms != {arm.name for arm in arms}:
            raise ValueError(f"Missing snapshot or call manifest: {case}")
        if not arms:
            continue
        calls_by_arm = archive_tools.load_case_calls(case, arms)
        for arm in arms:
            raw, package = archive_tools.load_snapshot(case / f"snapshot-{arm.name}.json")
            calls = calls_by_arm[arm.name]
            slots = archive_tools.report_slots(calls, raw, package)
            versions = {name: simulate(slots, package, cls) for name, cls in VERSIONS.items()}
            old, new = versions["v10"], versions["v11"]
            if baseline is not None:
                original = baseline.get((case.name, arm.name))
                v8 = versions["v8"]
                if (original is None or original["snapshot_sha256"] != raw["package_sha256"]
                        or v8["exact"] and original["status"] != v8["status"]):
                    raise ValueError(f"v8 replay does not match the archived run: {arm}")
                if v8["status"] == "completed":
                    artifact = archive_tools.read(arm / "archive/output.json")
                    if artifact["rendered_text"] != v8["markdown"]:
                        raise ValueError(f"v8 replay does not match the archived report: {arm}")
            comparable = old["exact"] and new["exact"]
            changed = comparable and old["markdown"] != new["markdown"]
            row = {"case": case.name, "arm": arm.name, "snapshot_sha256": raw["package_sha256"],
                   **versions, "body_changed": changed, "comparison_exact": comparable,
                   "calls": [{k: c[k] for k in ("kind", "prompt_sha256", "source")}
                             for c in calls]}
            results.append(row)
            if changed and new["status"] == "completed":
                packets[f"{case.name}__{arm.name}.json"] = packet(tasks[case.name], new["markdown"])
    if not results:
        raise ValueError("No recorded runs found")
    if baseline is not None and set(baseline) != {(r["case"], r["arm"]) for r in results}:
        raise ValueError("Replay run set does not match the archived results")
    for path in (archive / "judge-packets").glob("*.json"):
        original = archive_tools.read(path)
        case_id = path.name.split("__", 1)[0]
        if packet(tasks[case_id], original["report"]) != original:
            raise ValueError(f"Task labels do not match the archived judge packet: {path}")
    summary = {
        "schema": "research-v11-offline-replay-v1", "archive": str(archive),
        "new_model_calls": 0, "labels": labels, "runs": len(results),
        "archived_v8_verified": baseline is not None and all(r["v8"]["exact"] for r in results),
        **{f"{name}_states": dict(Counter(r[name]["status"] for r in results))
           for name in VERSIONS},
        **{f"{name}_answer_states": dict(Counter(r[name]["answer_status"] for r in results
                                               if r[name]["status"] == "completed"))
           for name in VERSIONS},
        "answer_transitions": dict(Counter(
            f"{r['v10']['answer_status']} -> {r['v11']['answer_status']}"
            for r in results if r["comparison_exact"])),
        "numeric_omitted": sum(len(r["v11"]["omitted"]) for r in results),
        "affected_reports": [f"{r['case']}/{r['arm']}" for r in results if r["v11"]["omitted"]],
        "body_changed_runs": [f"{r['case']}/{r['arm']}" for r in results if r["body_changed"]],
        "inexact_repair": {name: sum(r[name]["status"] == "inexact_repair" for r in results)
                           for name in VERSIONS},
        "unavailable_repair": {name: sum(r[name]["status"] == "unavailable_repair" for r in results)
                               for name in VERSIONS},
        "judge_packets": len(packets),
    }
    out.mkdir(parents=True, exist_ok=False)
    for row in results:
        folder = out / row["case"] / row["arm"]
        archive_tools.write(folder / "result.json", row)
        for name in VERSIONS:
            if row[name]["markdown"] is not None:
                (folder / f"report-{name}.md").write_text(row[name]["markdown"], encoding="utf-8")
    for name, value in packets.items():
        archive_tools.write(out / "judge-packets" / name, value)
    archive_tools.write(out / "results.json", results)
    archive_tools.write(out / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--tasks", type=Path, help="Frozen task labels (optional archive override)")
    args = parser.parse_args()
    print(json.dumps(replay(args.archive, args.out, tasks_path=args.tasks),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
