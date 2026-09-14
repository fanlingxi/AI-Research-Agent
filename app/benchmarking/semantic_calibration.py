"""Blind annotation export and explicit, subset-only judge calibration."""

from collections import Counter

from app.agent.semantic_review import fingerprint

VERDICTS = {"supported", "contradicted", "insufficient_evidence", "cannot_determine"}


def annotation_pack(records: list[dict]) -> dict:
    cases = []
    task_ids = [record["task_id"] for record in records]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("Duplicate observation task")
    for record in records:
        review = record.get("review", {})
        for finding in review.get("findings", []):
            content = {
                "case_id": f"{record['task_id']}/{finding['finding_id']}",
                "snapshot_sha256": review["snapshot_sha256"],
                "draft_sha256": review["draft_sha256"],
                "assertion": finding["assertion"],
                "citations": finding["citations"],
            }
            cases.append(
                {
                    **content,
                    "input_sha256": fingerprint(content),
                    "label": None,
                    "reviewer": None,
                    "note": None,
                }
            )
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate observation finding")
    return {"version": "a05-human-labels-v1", "scope": "finding_support_only", "cases": cases}


def calibrate(records: list[dict], labels: dict) -> dict:
    expected = {c["case_id"]: c for c in annotation_pack(records)["cases"]}
    if labels.get("version") != "a05-human-labels-v1":
        raise ValueError("Unknown annotation version")
    seen, human = set(), {}
    for case in labels["cases"]:
        key = case["case_id"]
        if key in seen or key not in expected:
            raise ValueError("Duplicate or unknown annotation case")
        seen.add(key)
        if any(
            case.get(k) != expected[key][k]
            for k in ("input_sha256", "snapshot_sha256", "draft_sha256", "assertion", "citations")
        ):
            raise ValueError("Annotation input changed")
        if case.get("label") is None:
            continue
        if (
            case["label"] not in VERDICTS
            or not str(case.get("reviewer") or "").strip()
            or not str(case.get("note") or "").strip()
        ):
            raise ValueError("Human label requires a valid category, reviewer and rationale")
        human[key] = case
    confusion, disagreements = Counter(), []
    matched = unavailable = predicted_support = gold_support = false_support = missed_support = 0
    for record in records:
        for finding in record.get("review", {}).get("findings", []):
            key = f"{record['task_id']}/{finding['finding_id']}"
            if key not in human:
                continue
            gold = human[key]["label"]
            predicted = finding["verdict"] if finding["status"] == "reviewed" else "unavailable"
            confusion[(gold, predicted)] += 1
            unavailable += predicted == "unavailable"
            matched += gold == predicted
            predicted_support += predicted == "supported"
            gold_support += gold == "supported"
            false_support += predicted == "supported" and gold != "supported"
            missed_support += gold == "supported" and predicted != "supported"
            if gold != predicted:
                disagreements.append({"case_id": key, "human": gold, "judge": predicted})
    return {
        "planned_runs": len(records),
        "run_status_counts": dict(Counter(r["status"] for r in records)),
        "total_findings": len(expected),
        "human_labeled": len(human),
        "unlabeled": len(expected) - len(human),
        "judge_unavailable_on_labeled": unavailable,
        "agreement_on_labeled": matched / len(human) if human else None,
        "false_support_count": false_support if human else None,
        "false_support_rate": false_support / predicted_support if predicted_support else None,
        "missed_support_count": missed_support if human else None,
        "missed_support_rate": missed_support / gold_support if gold_support else None,
        "confusion": [
            {"human": h, "judge": j, "count": n} for (h, j), n in sorted(confusion.items())
        ],
        "disagreements": disagreements,
        "publication_gate_enabled": False,
        "annotation_provenance": labels.get("provenance", {"mode": "unspecified"}),
        "label_counts": dict(Counter(case["label"] for case in human.values())),
        "uncovered_categories": sorted(VERDICTS - {case["label"] for case in human.values()}),
        "limitations": [
            "Only labeled findings; no full-report success estimate.",
            "Unlabeled cases and failed runs remain in denominators.",
            "Same-model judgments are not independent; human labels may also be wrong.",
            "No inter-annotator agreement is measured.",
        ],
    }
