from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import worker


def test_help_exits_before_building_worker_or_loading_database_settings(monkeypatch, capsys):
    build = Mock(side_effect=AssertionError("Worker must not be built"))
    settings = Mock(side_effect=AssertionError("Settings must not be loaded"))
    monkeypatch.setattr(worker, "build_worker", build)
    monkeypatch.setattr(worker, "get_settings", settings)
    monkeypatch.setattr("sys.argv", ["app.worker", "--help"])

    with pytest.raises(SystemExit) as exc:
        worker.main()

    assert exc.value.code == 0
    assert "--once" in capsys.readouterr().out
    build.assert_not_called()
    settings.assert_not_called()


@pytest.mark.parametrize("processed", [True, False])
def test_once_runs_exactly_once_and_prints_result(monkeypatch, capsys, processed):
    instance = Mock()
    instance.run_once.return_value = processed
    build = Mock(return_value=instance)
    monkeypatch.setattr(worker, "build_worker", build)

    worker.main(["--once"])

    build.assert_called_once_with()
    instance.run_once.assert_called_once_with()
    instance.run_forever.assert_not_called()
    assert capsys.readouterr().out == f"Processed task: {processed}\n"


def test_no_arguments_keeps_running_forever(monkeypatch):
    instance = Mock()
    monkeypatch.setattr(worker, "build_worker", lambda: instance)
    monkeypatch.setattr(worker, "get_settings",
                        lambda: SimpleNamespace(knowledge_worker_poll_seconds=2.5))

    worker.main([])

    instance.run_forever.assert_called_once_with(2.5)
    instance.run_once.assert_not_called()
