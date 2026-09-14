"""Whitelisted read-only A05 archive view; no live run or knowledge association."""

from app.benchmarking.experiments import ExperimentDataError, _safe_text
from app.benchmarking.semantic_calibration import VERDICTS, calibrate


def read_observation(reader):
    base = reader.semantic_archive
    # Keep the configured boundary unresolved so a directory symlink cannot widen it.
    records = reader._read(base, base / "results.json", optional=True)
    if records is None:
        return {"available": False, "records": [], "summary": None}
    try:
        labels = reader._read(base, base / "supervised-labels.json", optional=True)
        if labels is None:
            from app.benchmarking.semantic_calibration import annotation_pack

            labels = annotation_pack(records)
        summary = calibrate(records, labels)
        annotations = {case["case_id"]: case for case in labels["cases"]}
        projected = []
        for record in records:
            review = record.get("review", {})
            if review and (
                review["version"] != "finding-support-v1"
                or review["mode"] != "observation_only"
            ):
                raise ValueError("Unsupported observation version")
            findings = []
            for finding in review.get("findings", []):
                label = annotations.get(f"{record['task_id']}/{finding['finding_id']}", {})
                verdict = finding.get("verdict") if finding.get("status") == "reviewed" else None
                if verdict is not None and verdict not in VERDICTS:
                    raise ValueError("Unknown verdict")
                findings.append({
                    "id": _safe_text(finding["finding_id"]),
                    "assertion": _safe_text(finding["assertion"]),
                    "verdict": verdict,
                    "reason": _safe_text(finding.get("reason")),
                    "human_label": label.get("label"),
                    "human_note": _safe_text(label.get("note")),
                    "citations": [{
                        key: _safe_text(citation.get(key))
                        for key in (
                            "evidence_id", "source_id", "source_version", "title",
                            "page_start", "page_end", "quote",
                        )
                    } for citation in finding["citations"]],
                })
            projected.append({
                "task_id": _safe_text(record["task_id"]),
                "status": _safe_text(record["status"]),
                "run_id": _safe_text(record.get("run_id")),
                "snapshot_sha256": _safe_text(review.get("snapshot_sha256")),
                "output_sha256": _safe_text(record.get("output_sha256")),
                "findings": findings,
            })
        return {
            "available": True, "records": projected,
            "summary": {
                key: summary[key] for key in (
                    "planned_runs", "run_status_counts", "total_findings", "human_labeled",
                    "unlabeled", "judge_unavailable_on_labeled", "agreement_on_labeled",
                    "false_support_count", "label_counts", "uncovered_categories",
                )
            },
            "annotation_mode": _safe_text(summary["annotation_provenance"].get("mode")),
            "publication_gate_enabled": False,
        }
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ExperimentDataError("语义观察或标注内容无法匹配。") from exc
