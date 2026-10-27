"""Offline D040 span scoring: every prepared line must have exactly one owner."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path

from app.tools.pdf_tools import parse_pdf_source

from .align import EXCLUDED, LINE_TYPES
from .common import digest, read, write
from .packets import validate_line
from .score import binary_f1, ratio

THRESHOLDS = {
    "front_matter_f1": (">=", 0.95),
    "margin_exclusion_recall": (">=", 0.95),
    "references_exclusion_recall": (">=", 0.98),
    "body_false_exclusion_rate": ("<=", 0.005),
    "paragraph_boundary_f1": (">=", 0.85),
}
NAMES = dict(zip(THRESHOLDS, ["首页前置信息 F1", "页眉页脚排除召回", "参考文献排除召回",
                            "正文误排除率", "段落边界 F1"], strict=True))
VERSIONS = ("pypdf-v3", "pypdf-v4")


def span_predictions(pages, blocks):
    """No fuzzy matching, dropped lines, or arbitrary overlap tie breakers."""
    spans = defaultdict(list)
    for index, block in enumerate(blocks):
        for span in block.spans:
            if span.end <= span.start:
                raise ValueError("Empty or reversed block span")
            spans[span.page].append((span, index, block))
    result = {}
    for page in pages:
        for line in page["lines"]:
            if line["page"] != page["page"] or line["end"] <= line["start"]:
                raise ValueError(f"Invalid prepared span: {line['id']}")
            overlaps = [(span, index, block) for span, index, block in spans[page["page"]]
                        if span.start < line["end"] and span.end > line["start"]]
            if (len(overlaps) != 1 or overlaps[0][0].start > line["start"]
                    or overlaps[0][0].end < line["end"]):
                raise ValueError(f"Expected exactly one covering block: {line['id']}")
            _, index, block = overlaps[0]
            first = block.spans[0]
            begins = first.page == line["page"] and first.start == line["start"]
            label = LINE_TYPES[block.kind]
            if line["id"] in result:
                raise ValueError(f"Duplicate prepared line: {line['id']}")
            result[line["id"]] = {
                "label": label, "block": index,
                "paragraph_start": bool(label == "body" and begins
                                        and block.paragraph_start is not False),
            }
    return result


def gold_labels(pages, consensus, resolutions, packet):
    labels = {}
    expected = {line["id"] for page in pages for line in page["lines"]}
    for line in consensus.get(packet, {}).get("lines", []):
        if line["id"] in labels:
            raise ValueError(f"Duplicate consensus label: {line['id']}")
        labels[line["id"]] = line
    for key, value in resolutions.items():
        if key.startswith(f"{packet}/line/"):
            identifier = key.removeprefix(f"{packet}/line/")
            if value["id"] != identifier:
                raise ValueError("Arbitration changed line id")
            labels[identifier] = value
    if set(labels) != expected:
        raise ValueError(f"Incomplete or unknown gold lines for {packet}: "
                         f"missing={len(expected - labels.keys())}, "
                         f"unknown={len(labels.keys() - expected)}")
    for label in labels.values():
        validate_line(label)
    return [{**labels[line["id"]], "page": page["page"]}
            for page in pages for line in page["lines"]]


def wilson(successes, total):
    """Two-sided 95% Wilson score interval; no evidence yields null."""
    if not total:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(proportion * (1 - proportion) / total
                           + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def f1_interval(pairs):
    # F1 = 2J/(1+J), where J = TP/(TP+FP+FN). Transform the
    # Wilson interval for J rather than counting each TP as two independent trials.
    tp = sum(a and b for a, b in pairs)
    union = sum(a or b for a, b in pairs)
    interval = wilson(tp, union)
    return [2 * value / (1 + value) for value in interval] if interval else None


def metrics(gold, predicted):
    if {line["id"] for line in gold} != set(predicted):
        raise ValueError("Every gold line must have a span prediction")
    pairs = [(line, predicted[line["id"]]) for line in gold]
    first = [(g, p) for g, p in pairs if g["page"] == 1]
    body = [(g, p) for g, p in pairs if g["label"] == "body"]
    margins = [(g, p) for g, p in pairs if g["label"] in {"header", "footer", "page_number"}]
    refs = [(g, p) for g, p in pairs if g["label"] == "references"]
    return {
        "front_matter_f1": binary_f1([g["label"] == "front_matter" for g, p in first],
                                     [p["label"] == "front_matter" for g, p in first]),
        "margin_exclusion_recall": ratio(sum(p["label"] in EXCLUDED for g, p in margins),
                                          len(margins)),
        "references_exclusion_recall": ratio(sum(p["label"] in EXCLUDED for g, p in refs),
                                              len(refs)),
        # D040 is stricter than the older aligned-only metric: ANY non-body
        # prediction is a false exclusion, including caption/table/other.
        "body_false_exclusion_rate": ratio(sum(p["label"] != "body" for g, p in body), len(body)),
        "paragraph_boundary_f1": binary_f1([g["paragraph_start"] for g, p in body],
                                           [p["paragraph_start"] for g, p in body]),
        "wilson_95": {
            "front_matter_f1": f1_interval([
                (g["label"] == "front_matter", p["label"] == "front_matter") for g, p in first]),
            "margin_exclusion_recall": wilson(
                sum(p["label"] in EXCLUDED for g, p in margins), len(margins)),
            "references_exclusion_recall": wilson(
                sum(p["label"] in EXCLUDED for g, p in refs), len(refs)),
            "body_false_exclusion_rate": wilson(
                sum(p["label"] != "body" for g, p in body), len(body)),
            "paragraph_boundary_f1": f1_interval([
                (g["paragraph_start"], p["paragraph_start"]) for g, p in body]),
        },
        "scored_lines": len(gold), "unaligned_lines": 0,
        "denominators": {"first_page": len(first), "body": len(body), "margins": len(margins),
                         "references": len(refs)},
    }


def gates(values, baseline):
    checks = {}
    for name, (operator, threshold) in THRESHOLDS.items():
        value, old = values[name], baseline[name]
        if value is None or old is None:
            checks[name] = {"threshold": None, "non_regression": None, "adverse_delta_pp": None}
            continue
        adverse = old - value if operator == ">=" else value - old
        checks[name] = {"threshold": value >= threshold if operator == ">=" else value <= threshold,
                        "non_regression": adverse <= 0.01 + 1e-12,
                        "adverse_delta_pp": adverse * 100}
    complete = all(item["threshold"] is not None for item in checks.values())
    passed = all(item["threshold"] and item["non_regression"] for item in checks.values())
    return {"status": "incomplete" if not complete else "pass" if passed else "fail",
            "checks": checks}


def zoning_score(root, out, versions=VERSIONS):
    root, out = Path(root).resolve(), Path(out).resolve()
    if out == root or root in out.parents:
        raise ValueError("Zoning output must be outside the read-only trial root")
    versions = tuple(dict.fromkeys(versions))
    if not versions or set(versions) - set(VERSIONS):
        raise ValueError("Expected pypdf-v3 and/or pypdf-v4")
    manifest = read(root / "manifest.json")
    consensus = read(root / "consensus.json")
    arbitration = root / "arbitration.json"
    resolutions = read(arbitration).get("resolutions", {}) if arbitration.exists() else {}
    # Always compute the frozen baseline for the directional 1pp check.
    evaluated = tuple(dict.fromkeys(("pypdf-v3", *versions)))
    gold = defaultdict(list)
    predictions = {version: defaultdict(dict) for version in evaluated}
    errors = {version: defaultdict(list) for version in evaluated}
    for paper in manifest["papers"]:
        name, language = paper["paper"], paper["language"]
        if language not in {"zh", "en"}:
            raise ValueError(f"Unknown language: {language}")
        pages = read(root / "prepared" / f"{name}.json")
        labels = gold_labels(pages, consensus, resolutions, f"lines-{name}")
        gold[language].extend(labels)
        source = Path(paper["pdf"])
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise ValueError(f"Scoring requires a local PDF: {source}")
        if "sha256" in paper and digest(source) != paper["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {name}")
        for version in evaluated:
            parsed = parse_pdf_source(str(source), max_pages=paper["page_count"],
                                      derive_blocks=True, parser_version=version)
            texts = dict(zip(parsed.page_numbers, parsed.page_texts, strict=True))
            for page in pages:
                for line in page["lines"]:
                    if texts.get(page["page"], "")[line["start"]:line["end"]] != line["text"]:
                        raise ValueError(f"Prepared raw text changed: {line['id']}")
            predicted = span_predictions(pages, parsed.blocks)
            if set(predictions[version][language]) & set(predicted):
                raise ValueError("Duplicate line ids across papers")
            predictions[version][language].update(predicted)
            lines = {line["id"]: line for page in pages for line in page["lines"]}
            for label in labels:
                prediction = predicted[label["id"]]
                if (prediction["label"] != label["label"] or
                        (label["label"] == "body" and
                         prediction["paragraph_start"] != label["paragraph_start"])):
                    errors[version][language].append({"paper": name, **lines[label["id"]],
                                                       "gold": label, "prediction": prediction})
    arms = {version: {language: {"metrics": metrics(gold[language], predictions[version][language])}
                      for language in ("zh", "en")} for version in evaluated}
    for version in evaluated:
        for language in ("zh", "en"):
            arm = arms[version][language]
            arm["gate"] = gates(arm["metrics"], arms["pypdf-v3"][language]["metrics"])
            arm["error_counts"] = dict(Counter(
                f"{item['gold']['label']} -> {item['prediction']['label']}"
                for item in errors[version][language]
            ))
    result = {
        "provenance": "开发集已用于调参；AI 共识 + 仲裁标签，非人工；不能代表未见保留集。",
        "split": manifest.get("split"), "thresholds": THRESHOLDS,
        "requested_versions": versions, "arms": arms, "adopt": False,
        "note": "按全部原文行 span 评分；正文误排除含任何非 body 类；段落 F1 只计真值 body 行。"
                "1pp 检查按指标不利方向，零分母为 null，不判达标。",
    }
    if manifest.get("split") == "holdout-d040":
        result["provenance"] = "D040 保留集；仅使用本 root 的共识及仲裁标签；AI 标注，非人工。"
    result["interval_method"] = (
        "95% Wilson score; F1 uses monotone transform 2J/(1+J) of Wilson Jaccard interval; "
        "line-level intervals do not account for within-paper dependence. "
        "Gates use point estimates."
    )
    write(out / "score.json", result)
    write(out / "errors.json", errors)
    report = [result["provenance"], "", result["note"], "",
              "| 语言 | 指标 | pypdf-v3 | pypdf-v4 | v4 不利变化(pp) | 门槛/不退步 |",
              "|---|---|---:|---:|---:|---|"]

    def percent(value):
        return "无数据" if value is None else f"{value:.2%}"

    for language in ("zh", "en"):
        for key in THRESHOLDS:
            baseline = arms["pypdf-v3"][language]["metrics"][key]
            candidate = arms.get("pypdf-v4", {}).get(language)
            check = candidate["gate"]["checks"][key] if candidate else {}
            delta = check.get("adverse_delta_pp")
            report.append(f"| {language} | {NAMES[key]} | {percent(baseline)} | "
                          f"{percent(candidate['metrics'][key]) if candidate else '未请求'} | "
                          f"{f'{delta:+.2f}' if delta is not None else '无数据'} | "
                          f"{check.get('threshold')} / {check.get('non_regression')} |")
    report.extend(["", result["interval_method"], "",
                   "| Language | Version | Gate | Metric | 95% Wilson interval |",
                   "|---|---|---|---|---|"])
    for language in ("zh", "en"):
        for version in versions:
            arm = arms[version][language]
            for key, interval in arm["metrics"]["wilson_95"].items():
                bounds = "no data" if interval is None else " – ".join(map(percent, interval))
                report.append(f"| {language} | {version} | {arm['gate']['status']} | "
                              f"{key} | {bounds} |")
    report.extend(["", "门槛仅为开发集诊断，不作保留集采用结论。完整错误及行号见 errors.json。"])
    if manifest.get("split") == "holdout-d040":
        report[-1] = "D040 保留集门槛与不利方向 1pp 检查见上表；详见 errors.json。"
    (out / "score.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return result
