"""Offline v8/v10 validation replay. Reads recorded responses; never creates a client.

Snapshot hashes are checked on the original JSON, including compact-v1 fields from
newer context builders. Only the task/evidence view is needed by the validators.
The two report slots follow runtime order; a passing slot 0 prevents use of slot 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from app.agent.research_workflow import (
    ResearchPlan,
    TaskAnalysis,
    _canonical_json,
    _strip_json_fence,
    validate_research_draft,
)
from app.agent.structured_research import (
    REPORT_SYSTEM_PROMPT,
    AnchoredDirectResearchWorkflow,
    SharedEvidenceResearchWorkflow,
)
from app.benchmarking.holdout_v2_score import packet
from app.error_redaction import redact_error


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_digest(request):
    # RecordedClient's digest is distinct from the generation-attempt ledger digest.
    return digest(json.dumps([request["system_prompt"], request["prompt"]], ensure_ascii=False))


def _view(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _view(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_view(item) for item in value]
    return value


def load_snapshot(path):
    raw = read(path)
    hashed = {**raw, "package_sha256": ""}
    if raw["package_schema_version"] != "1.2" and raw.get("reading_format", "legacy") == "legacy":
        hashed.pop("reading_format", None)
    if raw["package_schema_version"] == "1.0":
        hashed.pop("retrieval_audit", None)
    if digest(_canonical_json(hashed)) != raw["package_sha256"]:
        raise ValueError(f"Snapshot fingerprint mismatch: {path}")
    return raw, _view({"task": raw["task"], "knowledge": raw["knowledge"]})


def _paid_calls(arm):
    records = read(arm / "generation/calls.json")
    folders = sorted((arm / "generation").glob("call-*"),
                     key=lambda p: int(p.name.removeprefix("call-")))
    if len(folders) != sum(c["kind"] == "live" for c in records):
        raise ValueError(f"Recorded call directory count mismatch: {arm}")
    paid, index = {}, 0
    for position, record in enumerate(records):
        if record["kind"] == "exact_prompt_replay":
            continue
        if record["kind"] != "live":
            raise ValueError(f"Unknown call kind: {arm}")
        folder = folders[index]
        index += 1
        request = read(folder / "request.json")
        if request_digest(request) != record["prompt_sha256"]:
            raise ValueError(f"Request digest mismatch: {folder}")
        response = (read(folder / "response.json")["text"]
                    if record["status"] == "returned" else None)
        if response is not None and not isinstance(response, str):
            raise ValueError(f"Invalid recorded response: {folder}")
        paid[position] = {"request": request, "response": response,
                          "status": record["status"], "source": str(folder)}
    return records, paid


def load_case_calls(case, arms):
    """Resolve replay entries only against verified paid responses from the same case."""
    loaded = {arm.name: _paid_calls(arm) for arm in arms}
    cache = {}
    for records, paid in loaded.values():
        for position, call in paid.items():
            if call["status"] == "returned":
                cache.setdefault(records[position]["prompt_sha256"], []).append(call)
    resolved = {}
    for name, (records, paid) in loaded.items():
        calls = []
        for position, record in enumerate(records):
            if position in paid:
                call = paid[position]
            else:
                candidates = cache.get(record["prompt_sha256"], [])
                if not candidates or len({c["response"] for c in candidates}) != 1:
                    raise ValueError(f"Missing or ambiguous exact replay source: {case / name}")
                call = candidates[0]
            calls.append({**call, "kind": record["kind"],
                          "prompt_sha256": record["prompt_sha256"]})
        resolved[name] = calls
    return resolved


def report_slots(calls, raw, package):
    if len(calls) not in (3, 4):
        raise ValueError("Expected analysis, plan, and one or two report slots")
    state = {}
    for call, key, schema in zip(calls[:2], ("task_analysis", "research_plan"),
                                 (TaskAnalysis, ResearchPlan), strict=True):
        if call["status"] != "returned":
            raise ValueError("Cannot replay a report without returned preparation calls")
        state[key] = schema.model_validate(
            json.loads(_strip_json_fence(call["response"]))
        ).model_dump(mode="json")
    workflow = AnchoredDirectResearchWorkflow.__new__(AnchoredDirectResearchWorkflow)
    slots, base_prompt = [], None
    for slot, call in enumerate(calls[2:]):
        request = call["request"]
        prompt = request["prompt"]
        try:
            payload = json.loads(prompt.split("\nResearch input: ", 1)[1]
                                 .split("\nRequired JSON schema: ", 1)[0])
        except (ValueError, IndexError) as exc:
            raise ValueError("Missing report input in recorded request") from exc
        if (payload["context_sha256"] != raw["package_sha256"]
                or payload["context_snapshot_id"] != raw["snapshot_id"]
                or payload["task"] != raw["task"]):
            raise ValueError("Snapshot fingerprint/task differs from report request")
        workflow._research_input = lambda package, payload=payload: payload
        expected = workflow._report_prompt(state, package)
        if request["system_prompt"] != REPORT_SYSTEM_PROMPT:
            raise ValueError("Recorded report system prompt differs from v8")
        if slot == 0:
            if prompt != expected:
                raise ValueError("Recorded report prompt differs from v8")
            base_prompt = expected
        elif (expected != base_prompt
              or not prompt.startswith(base_prompt + "\nPrevious attempt invalid: ")
              or not prompt.endswith("Produce a corrected complete object within the limits.")):
            raise ValueError("Repair request does not continue slot 0")
        slots.append({**call, "slot": slot})
    return slots


def simulate(slots, package, workflow_class):
    workflow = workflow_class.__new__(workflow_class)
    attempted, errors = [], []
    for call in slots:
        slot = call["slot"]
        attempted.append(slot)
        if call["status"] != "returned":
            errors.append({"slot": slot, "reason": "Generation failed or unsettled"})
            break  # Runtime never repairs a transport failure.
        try:
            report = workflow.parse_report(json.loads(_strip_json_fence(call["response"])))
            draft = workflow.prepare_report(report, package)
            validation = validate_research_draft(draft, package)
            if report.answer_status != "insufficient_evidence" and not validation.passed:
                raise ValueError("Citation IDs or bundle ownership are invalid")
        except (ValueError, TypeError) as exc:
            errors.append({"slot": slot, "reason": redact_error(exc, max_length=None)})
            continue
        return {"status": "completed", "used_slot": slot, "attempted_slots": attempted,
                "markdown": draft.markdown, "errors": errors}
    return {"status": "needs_review", "used_slot": None, "attempted_slots": attempted,
            "markdown": None, "errors": errors}


def _tasks(archive, tasks_path):
    # holdout-v4's original label file lives beside runs/. A portable archive can
    # carry tasks.json, or the caller can supply the same frozen label file explicitly.
    if tasks_path is None:
        tasks_path = archive / "tasks.json"
        if not tasks_path.is_file():
            tasks_path = archive.parent.parent / "tasks-final.json"
    data = read(tasks_path)
    tasks = data["tasks"] if isinstance(data, dict) else data
    return {task["id"]: task for task in tasks}, {
        "path": str(tasks_path.resolve()),
        "sha256": hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
        "provenance": "ai-consensus (not human-verified)",
    }


def replay(archive, out, *, tasks_path=None):
    archive, out = Path(archive).resolve(strict=True), Path(out).resolve()
    if out == archive or out.is_relative_to(archive) or archive.is_relative_to(out):
        raise ValueError("Replay output must be separate from the original archive")
    if out.exists():
        raise ValueError("Replay output must be a new directory")
    tasks, labels = _tasks(archive, Path(tasks_path) if tasks_path else None)
    baseline_path = archive / "results.json"
    baseline = ({(r["case"], r["workflow"]): r for r in read(baseline_path)}
                if baseline_path.is_file() else None)
    results, packets = [], {}
    # Validate everything before creating output; corruption leaves no partial replay.
    for case in sorted(archive.iterdir()):
        arms = sorted(p.parent.parent for p in case.glob("*/generation/calls.json"))
        snapshot_arms = {p.stem.removeprefix("snapshot-") for p in case.glob("snapshot-*.json")}
        if snapshot_arms != {arm.name for arm in arms}:
            raise ValueError(f"Missing snapshot or call manifest: {case}")
        if not arms:
            continue
        calls_by_arm = load_case_calls(case, arms)
        for arm in arms:
            raw, package = load_snapshot(case / f"snapshot-{arm.name}.json")
            slots = report_slots(calls_by_arm[arm.name], raw, package)
            old = simulate(slots, package, AnchoredDirectResearchWorkflow)
            new = simulate(slots, package, SharedEvidenceResearchWorkflow)
            if baseline is not None:
                original = baseline.get((case.name, arm.name))
                if (original is None or original["status"] != old["status"]
                        or original["snapshot_sha256"] != raw["package_sha256"]):
                    raise ValueError(f"v8 replay does not match the archived run: {arm}")
                if old["status"] == "completed":
                    artifact = read(arm / "archive/output.json")
                    if artifact["rendered_text"] != old["markdown"]:
                        raise ValueError(f"v8 replay does not match the archived report: {arm}")
            changed = old["markdown"] != new["markdown"]
            row = {"case": case.name, "arm": arm.name, "snapshot_sha256": raw["package_sha256"],
                   "v8": old, "v10": new, "body_changed": changed,
                   "calls": [{k: c[k] for k in ("kind", "prompt_sha256", "source")}
                             for c in calls_by_arm[arm.name]]}
            results.append(row)
            if new["status"] == "completed" and (old["status"] != "completed" or changed):
                packets[f"{case.name}__{arm.name}.json"] = packet(tasks[case.name], new["markdown"])
    if not results:
        raise ValueError("No recorded runs found")
    if baseline is not None and set(baseline) != {(r["case"], r["arm"]) for r in results}:
        raise ValueError("Replay run set does not match the archived results")
    for path in (archive / "judge-packets").glob("*.json"):
        original = read(path)
        case_id = path.name.split("__", 1)[0]
        if packet(tasks[case_id], original["report"]) != original:
            raise ValueError(f"Task labels do not match the archived judge packet: {path}")
    summary = {
        "schema": "research-v10-offline-replay-v1", "archive": str(archive),
        "new_model_calls": 0, "labels": labels, "runs": len(results),
        "archived_v8_verified": baseline is not None,
        "v8_states": dict(Counter(r["v8"]["status"] for r in results)),
        "v10_states": dict(Counter(r["v10"]["status"] for r in results)),
        "transitions": dict(Counter(r["v8"]["status"] + " -> " + r["v10"]["status"]
                                    for r in results)),
        "body_changed_runs": [f"{r['case']}/{r['arm']}" for r in results if r["body_changed"]],
        "newly_completed": sum(r["v8"]["status"] != "completed"
                               and r["v10"]["status"] == "completed" for r in results),
        "completed_body_changed": sum(r["v8"]["status"] == r["v10"]["status"] == "completed"
                                      and r["body_changed"] for r in results),
        "judge_packets": len(packets),
    }
    out.mkdir(parents=True, exist_ok=False)
    for row in results:
        folder = out / row["case"] / row["arm"]
        write(folder / "result.json", row)
        if row["v10"]["markdown"] is not None:
            (folder / "report-v10.md").write_text(row["v10"]["markdown"], encoding="utf-8")
    for name, value in packets.items():
        write(out / "judge-packets" / name, value)
    write(out / "results.json", results)
    write(out / "summary.json", summary)
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
