"""D040 denominators include missed tables; no implicit consensus or absent labels."""

from __future__ import annotations

import math

from .io import read
from .packets import validate


def proportion(success, total):
    if total == 0:
        return {"success": success, "total": total, "rate": None, "ci95": None}
    z = 1.959963984540054
    p, den = success / total, 1 + z * z / total
    center = (p + z * z / (2 * total)) / den
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total**2)) / den
    return {
        "success": success,
        "total": total,
        "rate": p,
        "ci95": [max(0, center - half), min(1, center + half)],
    }


def summarize(counts):
    return {
        "verified_fraction": proportion(counts["verified"], counts["true"]),
        "verified_cell_accuracy": proportion(counts["correct_cells"], counts["cells"]),
        "verified_whole_table_accuracy": proportion(
            counts["whole"], counts["verified"] + counts["verified_false_positives"]
        ),
        "captioned_detection_recall": proportion(
            counts["captioned_detected"], counts["captioned_true"]
        ),
        "false_detection_fraction": proportion(counts["false"], counts["detected"]),
        "counts": counts,
    }


def score(roots, judge, provenance):
    if not judge or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in judge
    ):
        raise ValueError("Judge name must be a simple directory suffix")
    totals, by_language = {}, {}
    seen = set()
    for root in roots:
        mapping = read(root / "private/blind-map.json")
        index = read(root / "packets/index.json")
        expected = set(index["packets"])
        if {p.stem for p in (root / f"judge-{judge}").glob("*.json")} != expected:
            raise ValueError("Judge directory must contain exactly the packet answers")
        for key in index["packets"]:
            if key in seen:
                raise ValueError("Duplicate page across scored runs")
            seen.add(key)
            packet = read(root / "packets" / f"{key}.json")
            answer = validate(packet, read(root / f"judge-{judge}" / f"{key}.json"))
            for alias, tables in answer["systems"].items():
                engine = mapping[key][alias]["engine"]
                counts = dict.fromkeys(
                    (
                        "true",
                        "verified",
                        "cells",
                        "correct_cells",
                        "whole",
                        "captioned_true",
                        "captioned_detected",
                        "false",
                        "detected",
                        "missed",
                        "verified_false_positives",
                    ),
                    0,
                )
                counts["true"] = counts["missed"] = len(answer["missed_tables"])
                counts["captioned_true"] = sum(t["captioned"] for t in answer["missed_tables"])
                for table in tables:
                    result = read(root / mapping[key][alias]["tables"][table["id"]])["grid"]
                    counts["detected"] += 1
                    if not table["is_table"]:
                        counts["false"] += 1
                        counts["verified_false_positives"] += result["verified"]
                        if result["verified"]:
                            # Do not let false verified tables disappear from accuracy metrics.
                            counts["cells"] += max(1, len(result["cells"]))
                        continue
                    counts["true"] += 1
                    counts["captioned_true"] += table["captioned"]
                    counts["captioned_detected"] += table["captioned"]
                    if result["verified"]:
                        counts["verified"] += 1
                        n = len(table["cells"]) + table["extra_cells"]
                        correct = sum(
                            c["content_correct"] and c["placement_correct"] for c in table["cells"]
                        )
                        counts["cells"] += n
                        counts["correct_cells"] += correct
                        counts["whole"] += correct == n
                for store, name in (
                    (totals, engine),
                    (by_language.setdefault(packet["language"], {}), engine),
                ):
                    aggregate = store.setdefault(name, dict.fromkeys(counts, 0))
                    for field, value in counts.items():
                        aggregate[field] += value
    return {
        "label_provenance": provenance,
        "judge": judge,
        "pages": len(seen),
        "interval": "Wilson 95%; cell interval treats cells as independent, descriptive only",
        "decision": "development-only; no automatic adoption",
        "engines": {k: summarize(v) for k, v in totals.items()},
        "languages": {
            lang: {k: summarize(v) for k, v in engines.items()}
            for lang, engines in by_language.items()
        },
    }
