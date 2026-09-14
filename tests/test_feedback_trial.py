import sqlite3
from contextlib import closing

import pytest

from app.benchmarking.feedback_trial import CASES, archive_run, backup_archive, revise, run
from tests.test_run_feedback import stack as stack


def test_trial_defaults_to_no_paid_calls_and_blocks_offline_execution(offline_trial_inputs):
    offline_trial_inputs("feedback_trial")
    result = run()
    assert result["mode"] == "dry_run"
    assert result["max_calls"] == 12
    assert {c["task_id"] for c in result["cases"]} == {
        "evaluation-q1", "evaluation-q8", "attribution-q12"}
    with pytest.raises(ValueError, match="offline"):
        run(execute=True)


def test_trial_backup_is_separate_and_never_overwrites(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "copy.db"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE marker (value TEXT)")
        db.execute("INSERT INTO marker VALUES ('original')")
    original = source.read_bytes()
    backup_archive(source, target)
    with closing(sqlite3.connect(target)) as db, db:
        db.execute("UPDATE marker SET value='revised'")
    assert source.read_bytes() == original
    with pytest.raises(ValueError):
        backup_archive(source, target)
    with pytest.raises(ValueError):
        backup_archive(source, source)


def test_trial_uses_real_feedback_and_keeps_original_for_human_recheck(stack, tmp_path):
    repo, service, _, _, task, _, _, _, original = stack
    old = archive_run(service, original.id, tmp_path / "original")
    child = revise(service, CASES[0], original.id)
    history = service.feedback.list(original.id)
    assert history["links"][0]["child_run_id"] == child.id
    assert history["links"][0]["recheck"] is None
    assert history["feedback"][0]["status"] == "accepted"
    assert CASES[0]["revision"] in repo.memory_repository.get_workspace_task(task.id).goal
    assert child.context_snapshot_id != original.context_snapshot_id
    assert archive_run(service, original.id, tmp_path / "after") == old
    assert archive_run(service, child.id, tmp_path / "child")["status"] == "queued"
