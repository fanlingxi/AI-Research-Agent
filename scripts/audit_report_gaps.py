"""Read-only audit of physical generation requests and missing answer-part gaps.

Run with ``python -m scripts.audit_report_gaps --archive PATH``. Counts archived
call directories, not replay result rows or exact-prompt cache hits. No LLM or
database client is constructed; the only output is aggregate JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def response_object(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def audit(archive):
    archive = Path(archive).resolve(strict=True)
    groups = defaultdict(Counter)
    missing = []
    for path in sorted(archive.rglob("request.json")):
        if path.parent.parent.name != "generation":
            continue
        relative = path.relative_to(archive)
        arm = path.parent.parent.parent
        run_path = arm / "archive/run.json"
        run = read(run_path) if run_path.is_file() else {}
        workflow = run.get("plugin", {}).get("workflow_key", "unknown")
        batch = Path(*relative.parts[:-5]).as_posix()
        key = (batch, workflow, run.get("workflow_version", "unknown"))
        counts = groups[key]
        counts["generation_calls"] += 1
        prompt = read(path)["prompt"]
        marker = "Required JSON schema: "
        if marker not in prompt:
            continue
        schema = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]
        properties = schema.get("properties", {})
        if "title" not in properties:
            continue
        counts["report_calls"] += 1
        if "answer_parts" not in properties:
            continue
        counts["answer_parts_report_calls"] += 1
        part_ref = properties["answer_parts"]["items"]["$ref"]
        part_schema = schema["$defs"][part_ref.rsplit("/", 1)[1]]
        required = "gap" in part_schema.get("required", [])
        counts["gap_required_calls"] += int(required)
        response_path = path.with_name("response.json")
        if not response_path.is_file():
            counts["response_unavailable"] += 1
            continue
        try:
            value = response_object(read(response_path)["text"])
        except (ValueError, KeyError, IndexError, TypeError):
            counts["invalid_response_json"] += 1
            continue
        parts = value.get("answer_parts") if isinstance(value, dict) else None
        if not isinstance(parts, list):
            continue
        absent = [i for i, part in enumerate(parts)
                  if isinstance(part, dict) and "gap" not in part]
        if not absent:
            continue
        counts["missing_gap_calls"] += 1
        counts["missing_gap_fields"] += len(absent)
        findings_key = "findings" if "findings" in part_schema["properties"] else "finding_indices"
        missing.append({
            "response": response_path.relative_to(archive).as_posix(),
            "run": run_path.relative_to(archive).as_posix() if run else None,
            "batch": batch, "workflow": workflow, "version": key[2],
            "run_id": run.get("id"), "status": run.get("status"),
            "repair_count": run.get("repair_count"),
            "repair_request": "\nPrevious attempt invalid: " in prompt,
            "gap_required": required,
            "parts": [{"index": i, "findings_count": len(parts[i][findings_key])
                       if isinstance(parts[i].get(findings_key), list) else None}
                      for i in absent],
        })
    totals = sum(groups.values(), Counter())
    # Group by source run path, not run IDs which can repeat across archive copies.
    affected_runs = {row["run"] for row in missing}
    totals["affected_runs"] = len(affected_runs)
    return {
        "archive": str(archive),
        "counting_unit": "physical generation/call-*/request.json; no replay/cache rows",
        "totals": dict(totals),
        "groups": [{"batch": key[0], "workflow": key[1], "version": key[2], **counts}
                   for key, counts in sorted(groups.items())],
        "missing": missing,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.archive), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
