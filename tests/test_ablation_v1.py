"""Deterministic ablation configuration, archived costs and pre-registered selection."""

import copy
import json
import socket

import pytest

from app.benchmarking import holdout_v2, holdout_v2_score
from app.context.models import ContextBuildRequest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_preset_snapshot_specs_and_plan():
    arms = holdout_v2.arm_preset("ablation-v1")
    specs = {json.dumps(a["context"], sort_keys=True) for a in arms}
    assert len(arms) == 5 and len(specs) == 4
    assert arms[3]["context"] == arms[4]["context"]
    assert [a["workflow"] for a in arms] == ["research_v8"] * 4 + ["research_v9"]
    requests = [ContextBuildRequest(**{"task_id": "task", "max_tokens": 16000,
                                       **a["context"]}) for a in arms]
    assert [r.max_tokens for r in requests] == [16000, 16000, 32000, 16000, 16000]
    assert requests[0].reading_format == "inline-v1"
    assert requests[1].reading_format == "compact-v1"
    assert requests[3].evidence_reranking == "coverage-v3"
    plan = holdout_v2.plan("all", arms="ablation-v1", dataset="v3")
    assert (plan["cases"], plan["runs"], plan["max_model_calls"]) == (31, 155, 651)


def live(key, inputs=100, seconds=2):
    return {"kind": "live", "status": "returned", "prompt_sha256": key,
            "usage": {"input_tokens": inputs, "output_tokens": 10}, "latency_seconds": seconds}


def replay(key):
    return {"kind": "exact_prompt_replay", "prompt_sha256": key, "usage": None,
            "new_model_calls": 0}


def test_summary_production_costs(tmp_path, monkeypatch):
    tasks = {name: {"expectation": {"required_conclusions": [], "outcome": "answer"}}
             for name in ("known", "unknown", "missing", "unresolved", "no-ranking")}
    monkeypatch.setattr(holdout_v2_score, "_tasks", lambda _: tasks)
    rows = [{"case": case, "workflow": arm, "status": "completed",
             "elapsed_seconds": 999, "paid_calls": 0}
            for case in tasks for arm in ("A", "B")]
    write(tmp_path / "results.json", rows)
    for case in tasks:
        if case == "missing":
            continue
        if case != "no-ranking":
            write(tmp_path / case / "ranking/calls.json", [live("rank", 20, 0.5)])
        original = live("gen")
        if case == "unknown":
            original["usage"] = None
        write(tmp_path / case / "A/generation/calls.json", [original])
        write(tmp_path / case / "B/generation/calls.json",
              [replay("missing" if case == "unresolved" else "gen")])
    result = holdout_v2_score.summary(tmp_path)
    a, b = result["arms"]["A"], result["arms"]["B"]
    assert (a["usage_known_runs"], a["usage_unknown_runs"]) == (3, 2)
    assert a["mean_input_tokens"] == pytest.approx(340 / 3)
    assert a["mean_latency_seconds"] == pytest.approx(7 / 3)
    assert (b["usage_known_runs"], b["usage_unknown_runs"]) == (2, 3)
    assert b["mean_input_tokens"] == 110
    assert b["mean_latency_seconds"] == 2.25
    known = {r["run"]: r for r in result["runs"]}
    assert known["known__B"]["input_tokens"] == 120
    assert known["unknown__B"]["usage_known"] is False
    assert known["unknown__B"]["input_tokens"] is None
    assert result["gate_D035"] is None


@pytest.mark.parametrize("bad", [{"usage": None}, {"latency_seconds": None},
                                  {"usage": {"output_tokens": 1}}])
def test_unknown_ranking_invalidates_entire_run(tmp_path, bad):
    write(tmp_path / "A/generation/calls.json", [live("gen")])
    write(tmp_path / "ranking/calls.json", [{**live("rank"), **bad}])
    assert holdout_v2_score._case_costs(tmp_path)["A"] == {
        "usage_known": False, "input_tokens": None, "latency_seconds": None,
    }


def metrics():
    return {a["name"]: {"completion_rate": 1, "overclaim_rate": 0,
                        "refusal_correctness": 1, "point_coverage": 0.5,
                        "mean_input_tokens": 100, "mean_latency_seconds": 10}
            for a in holdout_v2.arm_preset("ablation-v1")}


def test_d035_selection_and_tiebreak():
    arms = metrics()
    assert holdout_v2_score.gate_d035(arms)["selected"] is None
    arms["A1-compact"]["point_coverage"] = 0.55
    result = holdout_v2_score.gate_d035(arms)
    assert result["eligible"] == ["A1-compact"]
    assert result["selected"] == "A1-compact"
    arms["A3-compact-cov3"].update(point_coverage=0.7, mean_input_tokens=120)
    assert holdout_v2_score.gate_d035(arms)["selected"] == "A3-compact-cov3"
    arms["A4-compact-cov3-v9"].update(point_coverage=0.7, mean_input_tokens=110)
    assert holdout_v2_score.gate_d035(arms)["selected"] == "A4-compact-cov3-v9"
    arms["A4-compact-cov3-v9"]["mean_input_tokens"] = 121
    result = holdout_v2_score.gate_d035(arms)
    assert result["selected"] == "A3-compact-cov3"
    assert result["checks"]["A4-compact-cov3-v9"]["input_tokens_within_1_2x"] is False
    for name in arms:
        partial = copy.deepcopy(arms)
        del partial[name]
        assert holdout_v2_score.gate_d035(partial) is None


@pytest.mark.parametrize(("metric", "value"), [
    ("completion_rate", 0.9), ("overclaim_rate", 0.1), ("refusal_correctness", 0.9),
    ("point_coverage", 0.54), ("mean_input_tokens", 121), ("mean_latency_seconds", 12.1),
    ("mean_input_tokens", None), ("refusal_correctness", None),
])
def test_d035_each_requirement(metric, value):
    arms = metrics()
    arms["A1-compact"].update(point_coverage=0.6)
    arms["A1-compact"][metric] = value
    assert holdout_v2_score.gate_d035(arms)["selected"] is None


@pytest.mark.parametrize("limit", [None, 155])
def test_judge_cli_limit_is_forwarded_without_model(tmp_path, monkeypatch, capsys, limit):
    # Execute the CLI with a local stub: no provider is constructed or invoked.
    monkeypatch.setattr(holdout_v2_score, "judge_api",
                        lambda run_dir, max_calls=120: {"limit": max_calls})
    argv = [str(tmp_path), "--judge-api"]
    if limit is not None:
        argv += ["--max-judge-calls", str(limit)]
    holdout_v2_score.main(argv)
    assert json.loads(capsys.readouterr().out)["limit"] == (limit or 120)


def test_selection_is_charged_once_per_arm_with_its_retries(tmp_path):
    write(tmp_path / "A/generation/calls.json", [live("gen")])
    write(tmp_path / "B/generation/calls.json", [replay("gen")])
    failed = {**live("rank", 0, 1.5), "status": "failed", "usage": None}
    # One build pays (failed attempt + retry); three other builds replay the prompt.
    write(tmp_path / "ranking/calls.json",
          [failed, live("rank", 20, 0.5), replay("rank"), replay("rank"), replay("rank")])
    costs = holdout_v2_score._case_costs(tmp_path)
    for arm in ("A", "B"):
        assert costs[arm] == {"usage_known": True, "input_tokens": 120, "latency_seconds": 4}


def test_distinct_selection_prompts_are_not_guessed(tmp_path):
    write(tmp_path / "A/generation/calls.json", [live("gen")])
    write(tmp_path / "ranking/calls.json", [live("rank-1"), live("rank-2")])
    assert holdout_v2_score._case_costs(tmp_path)["A"]["usage_known"] is False
