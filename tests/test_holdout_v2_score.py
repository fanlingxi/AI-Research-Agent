import json
import subprocess
import sys

from app.benchmarking.holdout_v2_score import summary, write_packets


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_conservative_merge_and_gate(tmp_path) -> None:
    case = "multistep-q1"  # three required conclusions, outcome "answer"
    _write(tmp_path / "results.json", [
        {"case": case, "workflow": "A-default", "status": "completed"},
        {"case": case, "workflow": "B-candidate", "status": "completed"},
    ])
    for arm in ("A-default", "B-candidate"):
        (tmp_path / case / arm / "archive").mkdir(parents=True)
        (tmp_path / case / arm / "archive" / "report.md").write_text("报告", encoding="utf-8")
    assert write_packets(tmp_path) == 2
    ids = [f"{case}-c{i}" for i in (1, 2, 3)]
    full = {"covered": dict.fromkeys(ids, True), "violated": [False, False], "outcome_ok": True}
    one_miss = {**full, "covered": {**full["covered"], ids[2]: False}}
    _write(tmp_path / "judge-api" / f"{case}__A-default.json", one_miss)
    _write(tmp_path / "judge-claude" / f"{case}__A-default.json", full)
    _write(tmp_path / "judge-api" / f"{case}__B-candidate.json", full)
    _write(tmp_path / "judge-claude" / f"{case}__B-candidate.json",
           {**full, "violated": [True, False]})

    result = summary(tmp_path)
    a, b = result["arms"]["A-default"], result["arms"]["B-candidate"]
    assert (a["covered"], b["covered"]) == (2, 3)  # A loses the disputed point
    assert (a["violations"], b["violations"]) == (0, 1)  # any judge flags → violation
    assert result["gate_D029"]["switch_default_to_B"] is False
    assert result["label_provenance"].startswith("ai-consensus")

    _write(tmp_path / "judge-human" / f"{case}__A-default.json", full)
    human_only = summary(tmp_path, judges=["human"])
    assert human_only["arms"]["A-default"]["covered"] == 3
    assert human_only["arms"]["B-candidate"]["judged_runs"] == 0
    assert human_only["runs"][0]["judges"] == 1
    assert "selected judges: human" in human_only["judging"]
    assert summary(tmp_path, judges=["human", "api"])["arms"]["A-default"]["covered"] == 2
    assert summary(tmp_path)["runs"][0]["judges"] == 3
    assert summary(tmp_path, judges=[])["arms"]["A-default"]["judged_runs"] == 0
    cli = subprocess.run(
        [sys.executable, "-m", "app.benchmarking.holdout_v2_score", str(tmp_path),
         "--summary", "--judges", "human,api"],
        capture_output=True, check=True,
    )
    assert json.loads(cli.stdout)["arms"]["A-default"]["covered"] == 2
    assert json.loads((tmp_path / "score.json").read_text(encoding="utf-8"))["runs"][0][
        "judges"
    ] == 2
