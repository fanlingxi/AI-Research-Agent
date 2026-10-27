"""Two-judge agreement, explicit arbitration and stratified D039-B metrics."""

from __future__ import annotations

import math
import re

from .align import EXCLUDED
from .common import PARSERS, PROVENANCE, read, write
from .packets import validate_answer

THRESHOLDS = {
    "table_cell_accuracy": (">=", 0.95),
    "formula_equivalence": (">=", 0.90),
    "front_matter_f1": (">=", 0.95),
    "margin_exclusion_recall": (">=", 0.98),
    "references_exclusion_recall": (">=", 0.98),
    "body_false_exclusion_rate": ("<=", 0.005),
    "paragraph_boundary_f1": (">=", 0.90),
    "warm_p95_seconds_per_page": ("<=", 15),
    "peak_memory_bytes": ("<=", 16 * 1024**3),
    "peak_vram_bytes": ("<=", 4 * 1024**3),
}


def ratio(a, b):
    return a / b if b else None


def f1(tp, fp, fn):
    return ratio(2 * tp, 2 * tp + fp + fn)


def binary_f1(truth, predicted):
    pairs = list(zip(truth, predicted, strict=True))
    return f1(
        sum(a and b for a, b in pairs),
        sum(not a and b for a, b in pairs),
        sum(a and not b for a, b in pairs),
    )


def percentile95(values):
    if not values:
        return None
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def gates(metrics, complete=True, baseline=False):
    checks = {}
    for name, (operator, threshold) in THRESHOLDS.items():
        if baseline and name in {
            "table_cell_accuracy",
            "formula_equivalence",
            "warm_p95_seconds_per_page",
            "peak_memory_bytes",
            "peak_vram_bytes",
        }:
            continue
        value = metrics.get(name)
        checks[name] = (
            None
            if value is None
            else value >= threshold
            if operator == ">="
            else value <= threshold
        )
    status = (
        "incomplete"
        if not complete or any(v is None for v in checks.values())
        else "pass"
        if all(checks.values())
        else "fail"
    )
    return {"status": status, "checks": checks, "adopt": False}


def atoms(packet, answer):
    if answer is None:
        return {}
    if packet["kind"] == "lines":
        return {f"line/{r['id']}": r for r in answer["lines"]}
    result = {}
    for alias, system in answer["systems"].items():
        for name in ("tables", "formulas"):
            for row in system[name]:
                result[f"{alias}/{name}/{row['id']}"] = row
        for name in ("missed_tables", "missed_formulas"):
            result[f"{alias}/{name}"] = system[name]
    return result


def empty_answer(packet):
    if packet["kind"] == "lines":
        return {"packet_id": packet["packet_id"], "lines": []}
    return {
        "packet_id": packet["packet_id"],
        "systems": {
            alias: {"tables": [], "formulas": [], "missed_tables": [], "missed_formulas": []}
            for alias in ("X", "Y")
        },
    }


def expected_atoms(packet):
    if packet["kind"] == "lines":
        return [f"line/{r['id']}" for r in packet["lines"]]
    result = []
    for alias, blocks in packet["systems"].items():
        result.extend(f"{alias}/{b['type']}s/{b['id']}" for b in blocks)
        result.extend(f"{alias}/{name}" for name in ("missed_tables", "missed_formulas"))
    return result


def assemble(packet, values):
    answer = empty_answer(packet)
    for key, value in values.items():
        if packet["kind"] == "lines":
            answer["lines"].append(value)
        else:
            alias, name, *rest = key.split("/")
            if rest:
                answer["systems"][alias][name].append(value)
            else:
                answer["systems"][alias][name] = value
    return answer


def agree(packet, left, right, resolutions=None):
    """Missing judgments are disagreements; a single judge never becomes consensus."""
    resolutions = resolutions or {}
    for answer in (left, right):
        if answer is not None:
            validate_answer(packet, answer)
    a, b = atoms(packet, left), atoms(packet, right)
    accepted, conflicts = {}, []
    for key in expected_atoms(packet):
        full_key = f"{packet['packet_id']}/{key}"
        av, bv = a.get(key), b.get(key)

        # Examples are audit evidence, not part of the table accuracy label.
        def comparable(value):
            if isinstance(value, dict) and "examples" in value:
                return {k: v for k, v in value.items() if k != "examples"}
            return value

        if av is not None and bv is not None and comparable(av) == comparable(bv):
            accepted[key] = av
        else:
            resolved = full_key in resolutions
            conflicts.append(
                {
                    "id": full_key,
                    "paper": packet["paper"],
                    "page": packet.get("page")
                    or next(r["page"] for r in packet["lines"] if key == f"line/{r['id']}"),
                    "item": key,
                    "left": av,
                    "right": bv,
                    "resolved": resolved,
                }
            )
            if resolved:
                accepted[key] = resolutions[full_key]
    # Validate each arbitration value against its corresponding expected record even
    # when other records are unresolved, using a complete synthetic carrier.
    for key in accepted:
        value = accepted[key]
        if key.startswith("line/"):
            from .packets import validate_line

            validate_line(value)
            if value["id"] != key.removeprefix("line/"):
                raise ValueError("Arbitration changed line id")
        elif "/tables/" in key or "/formulas/" in key:
            from .packets import validate_formula, validate_table

            (validate_table if "/tables/" in key else validate_formula)(value)
            if value["id"] != key.split("/")[-1]:
                raise ValueError("Arbitration changed visual object id")
        else:
            # Reuse strict missing-object validation with no displayed blocks.
            alias, name = key.split("/")
            carrier = empty_answer(packet)
            carrier["systems"][alias][name] = value
            validate_answer({**packet, "systems": {"X": [], "Y": []}}, carrier)
    answer = assemble(packet, accepted)
    if len(accepted) == len(expected_atoms(packet)):
        validate_answer(packet, answer)
    return answer, conflicts


def line_metrics(gold, predictions):
    rows = [
        (r, predictions.get(r["id"], {"label": "unaligned", "paragraph_start": False}))
        for r in gold
    ]
    first = [(g, p) for g, p in rows if g["page"] == 1]
    body = [(g, p) for g, p in rows if g["label"] == "body"]
    margins = [(g, p) for g, p in rows if g["label"] in {"header", "footer", "page_number"}]
    refs = [(g, p) for g, p in rows if g["label"] == "references"]
    return {
        "front_matter_f1": binary_f1(
            [g["label"] == "front_matter" for g, p in first],
            [p["label"] == "front_matter" for g, p in first],
        ),
        "margin_exclusion_recall": ratio(
            sum(p["label"] in EXCLUDED for g, p in margins), len(margins)
        ),
        "references_exclusion_recall": ratio(
            sum(p["label"] in EXCLUDED for g, p in refs), len(refs)
        ),
        "body_false_exclusion_rate": ratio(
            sum(p["label"] in EXCLUDED | {"unaligned"} for g, p in body), len(body)
        ),
        "paragraph_boundary_f1": binary_f1(
            [g["label"] == "body" and g["paragraph_start"] for g, p in rows],
            [p["label"] == "body" and p["paragraph_start"] for g, p in rows],
        ),
        "scored_lines": len(rows),
        "unaligned_lines": sum(p["label"] == "unaligned" for g, p in rows),
    }


def visual_metrics(systems):
    cells = errors = formulas = equivalent = 0
    for system in systems:
        cells += sum(t["total_cells"] for t in system["tables"] + system["missed_tables"])
        errors += sum(t["error_cells"] for t in system["tables"])
        errors += sum(t["total_cells"] for t in system["missed_tables"])
        formulas += len(system["formulas"]) + len(system["missed_formulas"])
        equivalent += sum(f["equivalent"] for f in system["formulas"])
    return {
        "table_cell_accuracy": ratio(cells - errors, cells),
        "formula_equivalence": ratio(equivalent, formulas),
        "table_cells": cells,
        "error_cells": errors,
        "formulas": formulas,
    }


def performance(rows):
    def peak(key):
        return (
            max(r[key] for r in rows)
            if rows and all(r.get(key) is not None for r in rows)
            else None
        )

    warm = [s for r in rows for s in r.get("warm_page_seconds", [])]
    return {
        "warm_p95_seconds_per_page": percentile95(warm),
        "warm_timed_pages": len(warm),
        "peak_memory_bytes": peak("peak_memory_bytes"),
        "peak_vram_bytes": peak("peak_vram_bytes"),
        "cold_start_seconds": [r.get("cold_start_seconds") for r in rows],
        "cold_inference_seconds": [
            t["seconds"] for r in rows for t in r.get("page_timings", []) if t.get("cold")
        ],
    }


def complete_timing(row, pages):
    timings = row.get("page_timings", [])
    return (
        len(timings) == pages
        and {t["page"] for t in timings} == set(range(1, pages + 1))
        and all(
            isinstance(t.get("seconds"), (int, float))
            and math.isfinite(t["seconds"])
            and t["seconds"] >= 0
            and not t.get("cache_hit", False)
            for t in timings
        )
    )


def score(root, line_judges, image_judges, arbitration=None):
    for pair in (line_judges, image_judges):
        if (
            len(pair) != 2
            or pair[0] == pair[1]
            or any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in pair)
        ):
            raise ValueError("Specify two distinct judge directory names per modality")
    resolutions = read(arbitration)["resolutions"] if arbitration else {}
    manifest, index = read(root / "manifest.json"), read(root / "packets/index.json")
    mapping = read(root / "private/blind-map.json")
    languages = {p["paper"]: p["language"] for p in manifest["papers"]}
    consensus, conflicts, packets = {}, [], {}
    known_keys = set()
    for kind, ids, judges in (
        ("lines", index["line_packets"], line_judges),
        ("images", index["image_packets"], image_judges),
    ):
        for packet_id in ids:
            packet = read(root / "packets" / kind / f"{packet_id}.json")
            known_keys.update(f"{packet_id}/{key}" for key in expected_atoms(packet))
            packets[packet_id] = packet
            answers = []
            for judge in judges:
                path = root / f"judge-{judge}" / f"{packet_id}.json"
                answers.append(read(path) if path.exists() else None)
            answer, disagreements = agree(packet, *answers, resolutions)
            consensus[packet_id] = answer
            conflicts.extend(disagreements)
    if set(resolutions) - known_keys:
        raise ValueError("Arbitration contains unknown item ids")
    write(root / "consensus.json", consensus)
    write(root / "disagreements.json", conflicts)
    report = [
        PROVENANCE,
        "",
        "仲裁：查看原页 PNG，将结论写入 arbitration.json 的 resolutions。",
        '格式：{"resolutions": {"下列完整编号": 与该条意见相同结构的最终值}}',
        "",
    ]
    import json

    for item in conflicts:
        report.extend(
            [
                f"- {item['id']} | {item['paper']} 第 {item['page']} 页 | "
                f"{'已仲裁' if item['resolved'] else '待仲裁'}",
                f"  左：{json.dumps(item['left'], ensure_ascii=False)}",
                f"  右：{json.dumps(item['right'], ensure_ascii=False)}",
                "",
            ]
        )
    (root / "arbitration.md").write_text("\n".join(report), encoding="utf-8")
    arms = {}
    for parser in PARSERS:
        arms[parser] = {}
        for language in ("zh", "en"):
            gold, predicted, systems, timing = [], {}, [], []
            expected_lines = 0
            complete_outputs = True
            complete_performance = True
            for paper in manifest["papers"]:
                if paper["language"] != language:
                    continue
                packet_id = f"lines-{paper['paper']}"
                locations = {r["id"]: r["page"] for r in packets[packet_id]["lines"]}
                expected_lines += len(locations)
                gold.extend(
                    {**r, "page": locations[r["id"]]} for r in consensus[packet_id]["lines"]
                )
                path = root / "aligned" / parser / f"{paper['paper']}.json"
                if path.exists():
                    predicted.update({r["id"]: r for p in read(path) for r in p["lines"]})
                else:
                    complete_outputs = False
                metrics_path = root / "raw" / parser / paper["paper"] / "metrics.json"
                record = read(metrics_path) if metrics_path.exists() else {}
                timing.append(record)
                complete_performance &= complete_timing(record, paper["page_count"])
            for packet_id in index["image_packets"]:
                if languages[packets[packet_id]["paper"]] != language or parser == "pypdf-v3":
                    continue
                for alias, entry in mapping[packet_id].items():
                    if entry["parser"] == parser:
                        systems.append(consensus[packet_id]["systems"][alias])
            unresolved = sum(
                not c["resolved"] for c in conflicts if languages[c["paper"]] == language
            )
            metrics = {
                **line_metrics(gold, predicted),
                **visual_metrics(systems),
                **performance(timing),
                "expected_lines": expected_lines,
                "unresolved_items": unresolved,
            }
            complete = not unresolved and complete_outputs and len(gold) == expected_lines
            arms[parser][language] = {
                "metrics": metrics,
                "gate": gates(
                    metrics,
                    complete=complete and (complete_performance or parser == "pypdf-v3"),
                    baseline=parser == "pypdf-v3",
                ),
                "complete_labels": complete,
                "complete_performance": complete_performance,
            }
    candidates = [
        name
        for name in ("mineru", "paddle")
        if all(arms[name][lang]["gate"]["status"] == "pass" for lang in ("zh", "en"))
    ]
    selected = None
    if len(candidates) == 1:
        selected = candidates[0]
    elif len(candidates) == 2:

        def mean(name, key):
            return sum(arms[name][lang]["metrics"][key] for lang in ("zh", "en")) / 2

        difference = abs(
            mean("mineru", "table_cell_accuracy") - mean("paddle", "table_cell_accuracy")
        )
        selected = (
            min(candidates, key=lambda n: mean(n, "warm_p95_seconds_per_page"))
            if difference < 0.01
            else max(candidates, key=lambda n: mean(n, "table_cell_accuracy"))
        )
    result = {
        "provenance": PROVENANCE,
        "thresholds": THRESHOLDS,
        "arms": arms,
        "candidate_only": selected,
        "adopt": False,
        "default_parser": "pypdf-v3",
        "selection_note": "中英分别过门槛后，等权平均表格准确率；差 <1pp 取平均 p95 更快者。"
        "开发集不作采用决定，缺失/零分母指标为 null，不判达标。",
    }
    write(root / "score.json", result)
    lines = [
        PROVENANCE,
        "",
        f"候选：{selected or '无（未达标或证据不完整）'}；默认保持 pypdf-v3。",
        "",
        "| 解析器 | 语言 | 门槛 | 指标 | 数值 |",
        "|---|---|---|---|---|",
    ]
    chinese = dict(
        zip(
            THRESHOLDS,
            [
                "表格单元格准确率",
                "公式等价率",
                "首页前置信息 F1",
                "页眉页脚排除召回",
                "参考文献排除召回",
                "正文误排除率",
                "段落边界 F1",
                "热运行 p95 秒/页",
                "峰值内存（字节）",
                "峰值显存（字节）",
            ],
            strict=True,
        )
    )
    for parser, langs in arms.items():
        for language, arm in langs.items():
            for key in THRESHOLDS:
                value = arm["metrics"][key]
                lines.append(
                    f"| {parser} | {language} | {arm['gate']['status']} | {chinese[key]} | "
                    f"{value if value is not None else '无数据/不适用'} |"
                )
    lines.extend(["", result["selection_note"], "", "分歧和原始意见见 arbitration.md。"])
    (root / "score.md").write_text("\n".join(lines), encoding="utf-8")
    return result
