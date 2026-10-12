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
        root = dataset_path(config.get("dataset", "v2"))
    tasks = {}
    for name in ("tasks.dev.json", "tasks.holdout.json"):
        for task in json.loads((root / name).read_text(encoding="utf-8"))["tasks"]:
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


def _verdicts(run_dir: Path, key: str) -> list[dict]:
    found = []
    for folder in sorted(run_dir.glob("judge-*")):
        if folder.name == "judge-packets":
            continue
        path = folder / f"{key}.json"
        if path.is_file():
            found.append(json.loads(path.read_text(encoding="utf-8")))
    return found


def summary(run_dir: Path) -> dict:
    tasks = _tasks(run_dir)
    arms: dict[str, dict] = {}
    per_run = []
    for row in _rows(run_dir):
        task = tasks[row["case"]]
        exp = task["expectation"]
        required = [c["id"] for c in exp["required_conclusions"]]
        key = f"{row['case']}__{row['workflow']}"
        verdicts = _verdicts(run_dir, key) if _report(run_dir, row) is not None else []
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
        })
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
                        "outcome_ok": outcome_ok})
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
        "judging": "conservative merge of all available judges",
        "arms": arms,
        **gates,
        "runs": per_run,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--packets", action="store_true")
    group.add_argument("--judge-api", action="store_true")
    group.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    if args.packets:
        result = {"packets": write_packets(args.run_dir)}
    elif args.judge_api:
        result = judge_api(args.run_dir)
    else:
        result = summary(args.run_dir)
        (args.run_dir / "score.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(json.dumps(result if not args.summary else {k: v for k, v in result.items()
                                                       if k != "runs"},
                     ensure_ascii=False, indent=1))
