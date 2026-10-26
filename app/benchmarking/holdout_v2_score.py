"""Score a holdout-v2 A/B archive against the AI-consensus labels (D029).

Two judges grade each run's report independently against the frozen expectations:
the configured API model (DeepSeek) via ``--judge-api`` and a second judge whose verdicts
are dropped into ``judge-<name>/<case>__<arm>.json`` files (e.g. a Claude reviewer reading
``judge-packets/``). Disagreements count conservatively (not covered / violated / wrong).
Labels are AI-consensus, not human-verified; every summary says so.

Usage:
    python -m app.benchmarking.holdout_v2_score <run-dir> --packets
    python -m app.benchmarking.holdout_v2_score <run-dir> --judge-api     # paid calls
    python -m app.benchmarking.holdout_v2_score <run-dir> --summary
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from scripts.validate_holdout_v2 import DATASET, dataset_path

REFUSAL_OUTCOMES = {"scope_refusal", "abstain"}
JUDGE_SYSTEM = (
    "你是严格的研究报告评审。只依据给定的期望标签判断报告，不用自己的知识补全。"
    "只输出 JSON。"
)


def _tasks(run_dir: Path | None = None) -> dict[str, dict]:
    root = DATASET
    if run_dir is not None and (run_dir / "config.json").is_file():
        config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
        dataset = config.get("dataset", "v2")
        root = (DATASET.parent / "holdout-v4" if dataset in ("v4", "research-holdout-v4")
                else dataset_path(dataset))
    tasks = {}
    for name in ("tasks.dev.json", "tasks.holdout.json"):
        path = root / name
        if name == "tasks.dev.json" and not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for task in payload if isinstance(payload, list) else payload["tasks"]:
            tasks[task["id"]] = task
    return tasks


def _rows(run_dir: Path) -> list[dict]:
    return json.loads((run_dir / "results.json").read_text(encoding="utf-8"))


def _report(run_dir: Path, row: dict) -> str | None:
    path = run_dir / row["case"] / row["workflow"] / "archive" / "report.md"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def packet(task: dict, report: str) -> dict:
    exp = task["expectation"]
    return {
        "question": task["question"],
        "expected_outcome": exp["outcome"],
        "outcome_guide": {
            "answer": "应直接回答所有要点",
            "qualified_answer": "应回答但保留论文中的限定条件/不确定性",
            "needs_review": "只能部分回答，须明确指出无法确定的部分",
            "abstain": "证据不足，应明确说明无法回答，不能编造",
            "scope_refusal": "超出允许资料范围，应拒答或说明范围外，不能用外部知识作答",
        },
        "required_conclusions": [
            {"id": c["id"], "statement": c["statement"]} for c in exp["required_conclusions"]
        ],
        "forbidden_conclusions": exp["forbidden_conclusions"],
        "missing_information": exp.get("missing_information", []),
        "report": report,
        "instructions": (
            "逐条判断：required_conclusions 中每条是否被报告实质表达（covered，意思一致即可，"
            "不要求逐字）；forbidden_conclusions 中每条是否被报告断言（violated，报告明确否定"
            "或说明不能下结论则不算）；报告的整体处理是否符合 expected_outcome（outcome_ok）。"
            '输出 JSON：{"covered": {"<id>": true|false}, "violated": [true|false, ...],'
            ' "outcome_ok": true|false, "note": "一句话理由"}'
        ),
    }


def write_packets(run_dir: Path) -> int:
    tasks, out = _tasks(run_dir), run_dir / "judge-packets"
    out.mkdir(exist_ok=True)
    count = 0
    for row in _rows(run_dir):
        report = _report(run_dir, row)
        if report is None:
            continue
        data = packet(tasks[row["case"]], report)
        (out / f"{row['case']}__{row['workflow']}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        count += 1
    return count


def _parse(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("Judge returned no JSON")
    return json.loads(match.group(0))


def judge_api(run_dir: Path, max_calls: int = 120) -> dict:
    from dataclasses import replace

    from app.config.settings import Settings
    from app.llms.provider import LangChainChatClient, get_llm_client

    client = get_llm_client(Settings())
    if not isinstance(client, LangChainChatClient):
        raise ValueError("Configured real model required")
    client = replace(client, max_tokens=2048, timeout=120, max_retries=0, thinking_enabled=False)
    out = run_dir / "judge-api"
    out.mkdir(exist_ok=True)
    calls, failures = 0, 0
    for path in sorted((run_dir / "judge-packets").glob("*.json")):
        target = out / path.name
        if target.exists():
            continue
        if calls >= max_calls:
            break
        calls += 1
        try:
            verdict = _parse(client.invoke(path.read_text(encoding="utf-8"), JUDGE_SYSTEM))
            verdict["judge"] = f"{client.provider_name}:{client.model}"
            target.write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:  # recorded, never retried silently
            failures += 1
            (out / f"{path.stem}.error.txt").write_text(type(exc).__name__, encoding="utf-8")
    return {"calls": calls, "failures": failures}


def _verdicts(run_dir: Path, key: str, judges: list[str] | None = None) -> list[dict]:
    found = []
    for folder in sorted(run_dir.glob("judge-*")):
        if folder.name == "judge-packets":
            continue
        if judges is not None and folder.name.removeprefix("judge-") not in judges:
            continue
        path = folder / f"{key}.json"
        if path.is_file():
            found.append(json.loads(path.read_text(encoding="utf-8")))
    return found


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _case_costs(folder: Path) -> dict[str, dict]:
    """Attribute call costs, not benchmark wall time or replay savings, to each arm.

    RecordedClient writes calls.json only on invocation: absent ranking means no
    selection calls. Absent generation cannot establish a run's usage. Replay
    lookup stays within the case, matching the runner's cache lifetime.
    """
    ranking_path = folder / "ranking" / "calls.json"
    ranking = json.loads(ranking_path.read_text(encoding="utf-8")) if ranking_path.is_file() else []
    generation = {
        path.parent.parent.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*/generation/calls.json"))
    }
    originals = {}
    for call in [c for calls in generation.values() for c in calls]:
        if call.get("kind") == "live" and call.get("status") == "returned":
            originals.setdefault(call.get("prompt_sha256"), []).append(call)
    # Every snapshot build runs one selection (with its retries); the other builds of
    # the case replay that same prompt. Charge each arm the paid attempts once, never
    # the replays. A failed attempt costs its latency but no input tokens.
    selection = [c for c in ranking if c.get("kind") == "live"]
    selection_known = len({c.get("prompt_sha256") for c in selection}) <= 1
    selection = [
        {**c, "usage": {"input_tokens": 0}} if c.get("status") == "failed" else c
        for c in selection
    ]
    result = {}
    for name, calls in generation.items():
        tokens, latency, known = 0, 0, selection_known
        for call in calls + selection:
            if call.get("kind") == "exact_prompt_replay":
                matches = originals.get(call.get("prompt_sha256"), [])
                # Ambiguous origins must not silently select a cheaper paid call.
                costs = {(json.dumps(c.get("usage"), sort_keys=True), c.get("latency_seconds"))
                         for c in matches}
                call = matches[0] if len(costs) == 1 else {}
            usage = call.get("usage") or {}
            inputs, seconds = usage.get("input_tokens"), call.get("latency_seconds")
            if not all(_number(v) and v >= 0 for v in (inputs, seconds)):
                known = False
                continue
            tokens += inputs
            latency += seconds
        result[name] = {"usage_known": known, "input_tokens": tokens if known else None,
                        "latency_seconds": latency if known else None}
    return result


def gate_d035(arms: dict[str, dict]) -> dict | None:
    names = ("A0-standard", "A1-compact", "A2-32k", "A3-compact-cov3", "A4-compact-cov3-v9")
    if not set(names) <= arms.keys():
        return None
    baseline = arms[names[0]]
    checks, eligible = {}, []
    for name in names[1:]:
        candidate = arms[name]
        tests = {}
        for label, metric, direction, offset, factor in (
            ("completion_not_worse", "completion_rate", "ge", 0, 1),
            ("overclaim_not_worse", "overclaim_rate", "le", 0, 1),
            ("refusal_not_worse", "refusal_correctness", "ge", 0, 1),
            ("coverage_plus_5pp", "point_coverage", "ge", 0.05, 1),
            ("input_tokens_within_1_2x", "mean_input_tokens", "le", 0, 1.2),
            ("latency_within_1_2x", "mean_latency_seconds", "le", 0, 1.2),
        ):
            a, b = baseline.get(metric), candidate.get(metric)
            tests[label] = bool(
                _number(a) and _number(b)
                and (b >= a * factor + offset if direction == "ge" else b <= a * factor)
            )
        checks[name] = tests
        if all(tests.values()):
            eligible.append(name)
    selected = min(eligible, key=lambda n: (
        -arms[n]["point_coverage"], arms[n]["mean_input_tokens"], n,
    )) if eligible else None
    return {"checks": checks, "eligible": eligible, "selected": selected}


def gate_d036(arms: dict[str, dict]) -> dict | None:
    if not {"A0-standard", "A1-compact"} <= arms.keys():
        return None
    baseline, candidate = arms["A0-standard"], arms["A1-compact"]
    checks = {}
    for label, metric, direction, offset, factor in (
        ("completion_not_worse", "completion_rate", "ge", 0, 1),
        ("coverage_plus_10pp", "point_coverage", "ge", 0.10, 1),
        ("overclaim_not_worse", "overclaim_rate", "le", 0, 1),
        ("refusal_not_worse", "refusal_correctness", "ge", 0, 1),
        ("input_tokens_within_1_2x", "mean_input_tokens", "le", 0, 1.2),
        ("latency_within_1_2x", "mean_latency_seconds", "le", 0, 1.2),
    ):
        a, b = baseline.get(metric), candidate.get(metric)
        checks[label] = bool(
            _number(a) and _number(b)
            and (b >= a * factor + offset if direction == "ge" else b <= a * factor)
        )
    return {"checks": checks, "switch_default": all(checks.values())}


def summary(run_dir: Path, judges: list[str] | None = None) -> dict:
    tasks = _tasks(run_dir)
    arms: dict[str, dict] = {}
    per_run = []
    costs = {}
    for row in _rows(run_dir):
        if row["case"] not in costs:
            costs[row["case"]] = _case_costs(run_dir / row["case"])
        cost = costs[row["case"]].get(row["workflow"], {
            "usage_known": False, "input_tokens": None, "latency_seconds": None,
        })
        task = tasks[row["case"]]
        exp = task["expectation"]
        required = [c["id"] for c in exp["required_conclusions"]]
        key = f"{row['case']}__{row['workflow']}"
        verdicts = _verdicts(run_dir, key, judges) if _report(run_dir, row) is not None else []
        # Conservative merge: covered only if every judge says so; violated if any judge does.
        covered = sum(
            1 for cid in required
            if verdicts and all(v.get("covered", {}).get(cid) is True for v in verdicts)
        )
        violated = any(any(v.get("violated") or []) for v in verdicts)
        outcome_ok = bool(verdicts) and all(v.get("outcome_ok") is True for v in verdicts)
        agree = len(verdicts) >= 2 and len({json.dumps(
            [v.get("covered"), v.get("violated"), v.get("outcome_ok")], sort_keys=True
        ) for v in verdicts}) == 1
        arm = arms.setdefault(row["workflow"], {
            "attempts": 0, "completed": 0, "required": 0, "covered": 0, "violations": 0,
            "refusal_tasks": 0, "refusal_ok": 0, "judged_runs": 0, "judges_agree": 0,
            "usage_known_runs": 0, "usage_unknown_runs": 0,
            "mean_input_tokens": None, "mean_latency_seconds": None,
        })
        arm["usage_known_runs" if cost["usage_known"] else "usage_unknown_runs"] += 1
        if cost["usage_known"]:
            count = arm["usage_known_runs"]
            for metric in ("input_tokens", "latency_seconds"):
                mean = f"mean_{metric}"
                arm[mean] = ((arm[mean] or 0) * (count - 1) + cost[metric]) / count
        arm["attempts"] += 1
        arm["completed"] += row.get("status") == "completed"
        arm["required"] += len(required)
        arm["covered"] += covered
        arm["violations"] += violated
        arm["judged_runs"] += bool(verdicts)
        arm["judges_agree"] += agree
        if exp["outcome"] in REFUSAL_OUTCOMES:
            arm["refusal_tasks"] += 1
            arm["refusal_ok"] += outcome_ok
        per_run.append({"run": key, "status": row.get("status"), "judges": len(verdicts),
                        "covered": f"{covered}/{len(required)}", "violated": violated,
                        "outcome_ok": outcome_ok, **cost})
    for arm in arms.values():
        arm["completion_rate"] = arm["completed"] / arm["attempts"]
        arm["point_coverage"] = arm["covered"] / arm["required"] if arm["required"] else None
        arm["overclaim_rate"] = arm["violations"] / arm["attempts"]
        arm["refusal_correctness"] = (
            arm["refusal_ok"] / arm["refusal_tasks"] if arm["refusal_tasks"] else None
        )
    gates = {"gate_D029": None, "gate_D030": None}
    for candidate, gate_name in (("B-candidate", "gate_D029"), ("C-candidate", "gate_D030")):
        if not {"A-default", candidate} <= arms.keys():
            continue
        a, b = arms["A-default"], arms[candidate]
        checks = {
            "completion_not_worse": b["completion_rate"] >= a["completion_rate"],
            "coverage_plus_10pp": (
                a["point_coverage"] is not None and b["point_coverage"] is not None
                and b["point_coverage"] >= a["point_coverage"] + 0.10
            ),
            "overclaim_not_worse": b["overclaim_rate"] <= a["overclaim_rate"],
            "refusal_not_worse": (b["refusal_correctness"] or 0) >= (a["refusal_correctness"] or 0),
        }
        gates[gate_name] = {
            "checks": checks, f"switch_default_to_{candidate[0]}": all(checks.values()),
        }
    return {
        "label_provenance": "ai-consensus (not human-verified)",
        "judging": ("conservative merge of all available judges" if judges is None
                    else f"conservative merge of selected judges: {', '.join(judges)}"),
        "arms": arms,
        **gates,
        "gate_D035": gate_d035(arms),
        "gate_D036": gate_d036(arms),
        "runs": per_run,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--packets", action="store_true")
    group.add_argument("--judge-api", action="store_true")
    group.add_argument("--summary", action="store_true")
    parser.add_argument("--judges", help="Only merge these judge directories, e.g. human,api")
    parser.add_argument("--max-judge-calls", type=int, default=120)
    args = parser.parse_args(argv)
    judges = None
    if args.judges is not None:
        judges = [name.strip() for name in args.judges.split(",")]
        if not args.summary or not all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in judges):
            parser.error("--judges requires --summary and comma-separated judge names")
    if args.packets:
        result = {"packets": write_packets(args.run_dir)}
    elif args.judge_api:
        result = judge_api(args.run_dir, max_calls=args.max_judge_calls)
    else:
        result = summary(args.run_dir, judges=judges)
        (args.run_dir / "score.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(json.dumps(result if not args.summary else {k: v for k, v in result.items()
                                                       if k != "runs"},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
