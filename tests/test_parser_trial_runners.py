"""HTTP, rendering and Paddle orchestration use fakes; no parser process is launched."""

import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.benchmarking.parser_trial.common import digest, read, write
from app.benchmarking.parser_trial.prepare import corpus, prepare
from app.benchmarking.parser_trial.resources import ResourceSampler, gpu_bytes, process_tree_bytes
from app.benchmarking.parser_trial.runners import LocalMinerU, run_mineru, run_paddle


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network/process launches forbidden; inject a fake")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def paper(tmp_path, pages=2):
    path = tmp_path / "p.pdf"
    path.write_bytes(b"synthetic input for fake transport only")
    result = {
        "paper": "p",
        "language": "zh",
        "pdf": str(path),
        "sha256": digest(path),
        "page_count": pages,
    }
    write(tmp_path / "manifest.json", {"papers": [result]})
    write(
        tmp_path / "prepared/p.json", [{"page": i + 1, "png": f"{i + 1}.png"} for i in range(pages)]
    )
    return result


class FakeHTTP:
    def __init__(self, terminal="completed", deduplicated=False):
        self.requests = []
        self.terminal = terminal
        self.deduplicated = deduplicated

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if path == "/v1/uploads":
            value = {"id": "upload-1", "status": "pending"}
            if self.deduplicated:
                value.update(status="completed", file={"id": "file-1"})
        elif path.endswith("/complete"):
            value = {"file": {"id": "file-1"}}
        elif path == "/v1/parse/jobs":
            value = {"job_id": "job-1"}
        elif path == "/v1/parse/jobs/job-1":
            value = {
                "status": self.terminal,
                "files": [
                    {
                        "parse": {"duration_ms": 2500},
                        "output_files": {
                            name: {"file_id": name + "-1"}
                            for name in ("structured_content", "middle_json")
                        },
                    }
                ],
            }
        else:
            value = {"raw": "preserved"}
        return SimpleNamespace(
            json=lambda: value, raise_for_status=lambda: None, content=json.dumps(value).encode()
        )


class FakeSampler:
    def __init__(self, pid, **kwargs):
        assert pid > 0
        self.result = {"peak_memory_bytes": 100, "peak_vram_bytes": 200}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_mineru_upload_job_poll_download_protocol_and_raw_preservation(tmp_path):
    item = paper(tmp_path)
    client = FakeHTTP()
    result = LocalMinerU(client=client).parse(item, tmp_path / "raw")
    methods = [(m, p) for m, p, kwargs in client.requests]
    assert methods == [
        ("POST", "/v1/uploads"),
        ("PUT", "/v1/uploads/upload-1/content"),
        ("POST", "/v1/uploads/upload-1/complete"),
        ("POST", "/v1/parse/jobs"),
        ("GET", "/v1/parse/jobs/job-1"),
        ("GET", "/v1/files/structured_content-1/content"),
        ("GET", "/v1/files/middle_json-1/content"),
    ]
    assert client.requests[0][2]["json"]["sha256sum"] == item["sha256"]
    assert client.requests[3][2]["json"]["files"][0]["page_range"] == "1-2"
    assert result["parse"]["duration_ms"] == 2500
    assert read(tmp_path / "raw/structured_content.json") == {"raw": "preserved"}


def test_mineru_completed_upload_skips_content_put(tmp_path):
    client = FakeHTTP(deduplicated=True)
    LocalMinerU(client=client).parse(paper(tmp_path), tmp_path / "raw")
    assert not any(
        method == "PUT" or path.endswith("/complete") for method, path, kwargs in client.requests
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://mineru.net",
        "http://localhost:16580",
        "http://192.168.1.1",
        "http://127.0.0.1@evil.com",
        "http://127.0.0.1/x",
    ],
)
def test_remote_and_ambiguous_mineru_endpoints_forbidden(url):
    with pytest.raises(ValueError, match="loopback"):
        LocalMinerU(url, client=FakeHTTP())


def test_local_service_unavailable_and_terminal_failure(tmp_path):
    item = paper(tmp_path)

    def fail(*args, **kwargs):
        raise ConnectionError("offline fake")

    with pytest.raises(RuntimeError, match="local service unavailable"):
        LocalMinerU(client=SimpleNamespace(request=fail)).parse(item, tmp_path / "raw")
    with pytest.raises(RuntimeError, match="partial"):
        LocalMinerU(client=FakeHTTP(terminal="partial")).parse(item, tmp_path / "raw")
    clock = iter([0, 2])
    with pytest.raises(TimeoutError):
        LocalMinerU(client=FakeHTTP(terminal="running")).parse(
            item, tmp_path / "raw", clock=lambda: next(clock), timeout=1
        )


def test_mineru_page_measurements_budget_and_no_retries(tmp_path):
    paper(tmp_path)
    client = FakeHTTP()
    run_mineru(tmp_path, 123, client=client, sampler=FakeSampler)
    metrics = read(tmp_path / "raw/mineru/p/metrics.json")
    assert metrics["warm_page_seconds"] == [2.5]
    assert [row["cold"] for row in metrics["page_timings"]] == [True, False]
    assert [
        k["json"]["files"][0]["page_range"] for m, p, k in client.requests if p == "/v1/parse/jobs"
    ] == ["1", "2"]
    assert read(tmp_path / "attempts/mineru.json")["p"]["units"] == 2
    count = len(client.requests)
    run_mineru(tmp_path, 123, client=client, sampler=FakeSampler)
    assert len(client.requests) == count


def test_failed_mineru_consumes_budget_and_prevents_retry(tmp_path):
    paper(tmp_path)
    client = FakeHTTP(terminal="failed")
    with pytest.raises(RuntimeError):
        run_mineru(tmp_path, 123, client=client, sampler=FakeSampler)
    ledger = read(tmp_path / "attempts/mineru.json")
    assert ledger["p"] == {"units": 2, "status": "failed", "error": "MinerU job ended: failed"}
    count = len(client.requests)
    run_mineru(tmp_path, 123, client=client, sampler=FakeSampler)
    assert len(client.requests) == count


def test_page_cap_checked_before_requests(tmp_path):
    paper(tmp_path)
    client = FakeHTTP()
    with pytest.raises(ValueError, match="budget"):
        run_mineru(tmp_path, 123, max_pages=1, client=client, sampler=FakeSampler)
    assert client.requests == []


def test_paddle_subprocess_contract_and_failure_accounting(tmp_path):
    paper(tmp_path)
    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        assert "PADDLE_PDX_CACHE_HOME" in kwargs["env"]
        assert Path(command[1]).name == "paddle_worker.py"
        write(Path(command[3]) / "p/metrics.json", {"warm_page_seconds": [1]})
        return SimpleNamespace(pid=456, wait=lambda timeout: 0)

    run_paddle(tmp_path, "paddle-python.exe", popen=popen, sampler=FakeSampler)
    assert len(calls) == 1
    metrics = read(tmp_path / "raw/paddle/p/metrics.json")
    assert metrics["peak_memory_bytes"] == 100
    assert read(tmp_path / "attempts/paddle.json")["p"]["status"] == "completed"
    run_paddle(tmp_path, "paddle-python.exe", popen=popen, sampler=FakeSampler)
    assert len(calls) == 1


def test_paddle_timeout_kills_worker_and_records_failure(tmp_path):
    paper(tmp_path)
    killed = []

    def wait(timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return 1

    process = SimpleNamespace(pid=456, wait=wait, kill=lambda: killed.append(True))
    with pytest.raises(subprocess.TimeoutExpired):
        run_paddle(tmp_path, "fake", popen=lambda *a, **kw: process, sampler=FakeSampler)
    assert killed == [True]
    assert read(tmp_path / "attempts/paddle.json")["p"]["status"] == "failed"


def test_resource_cim_tree_fallback_and_gpu_units(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    rows = [
        {"ProcessId": 1, "ParentProcessId": 0, "WorkingSetSize": 10},
        {"ProcessId": 2, "ParentProcessId": 1, "WorkingSetSize": 20},
        {"ProcessId": 3, "ParentProcessId": 2, "WorkingSetSize": 30},
        {"ProcessId": 4, "ParentProcessId": 0, "WorkingSetSize": 999},
    ]

    def run(command, **kwargs):
        assert command[0] == "powershell"
        return SimpleNamespace(stdout=json.dumps(rows))

    assert process_tree_bytes(1, run=run) == 60
    assert gpu_bytes(run=lambda *a, **kw: SimpleNamespace(stdout="400\n600\n")) == 1000 * 1024**2


def test_resource_sampling_missing_data_remains_unknown(monkeypatch):
    def fail():
        raise FileNotFoundError("fake missing nvidia-smi")

    monkeypatch.setattr("app.benchmarking.parser_trial.resources.gpu_bytes", fail)
    monkeypatch.setattr(
        "app.benchmarking.parser_trial.resources.process_tree_bytes", lambda pid: 100
    )
    with ResourceSampler(12) as sampler:
        pass
    assert sampler.result["peak_memory_bytes"] == 100
    assert sampler.result["peak_vram_bytes"] is None
    assert sampler.result["resource_errors"]


def test_prepare_reuses_layout_and_renders_via_fake_subprocess(tmp_path, monkeypatch):
    from app.tools.pdf_layout import TextRun

    item = paper(tmp_path, pages=1)
    fake_page = SimpleNamespace(
        mediabox=SimpleNamespace(width=600, height=800), extract_text=lambda: "Body text."
    )
    monkeypatch.setattr("pypdf.PdfReader", lambda path: SimpleNamespace(pages=[fake_page]))
    monkeypatch.setattr(
        "app.tools.pdf_layout.collect_text_runs",
        lambda page: ([TextRun("Body text.", 60, 700, 10)], 800),
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert Path(command[1]).name == "render_worker.py"

    prepare(tmp_path, "renderer.exe", [item], run=run)
    prepared = read(tmp_path / "prepared/p.json")
    assert prepared[0]["lines"][0]["id"] == "p:p001:l0001"
    assert prepared[0]["lines"][0]["anchor_pt"] == [60, 100]
    assert read(tmp_path / "manifest.json")["papers"][0]["sha256"] == item["sha256"]
    assert len(calls) == 1
    assert len(corpus()) == 12
    assert "2025.ccl-1.15" not in {p["paper"] for p in corpus()}
