"""Explicit live entry points. No parser imports or network actions at import time."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from app.error_redaction import redact_error

from .common import PROJECT, digest, exclusive, read, reserve, write
from .resources import ResourceSampler


class LocalMinerU:
    def __init__(self, base_url="http://127.0.0.1:16580", client=None):
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MinerU requires a literal loopback HTTP address; remote is forbidden")
        if client is None:
            import httpx

            client = httpx.Client(
                base_url=base_url.rstrip("/"), timeout=60, trust_env=False, follow_redirects=False
            )
        self.client = client

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:
            raise RuntimeError(
                "MinerU local service unavailable or request failed; "
                "start/check the local service separately"
            ) from exc

    def parse(
        self, paper, folder, sleep=time.sleep, clock=time.monotonic, timeout=3600, page_range=None
    ):
        path = Path(paper["pdf"])
        if digest(path) != paper["sha256"]:
            raise ValueError("PDF hash changed since prepare")
        start = clock()
        upload = self.request(
            "POST",
            "/v1/uploads",
            json={
                "filename": path.name,
                "bytes": path.stat().st_size,
                "mime_type": "application/pdf",
                "purpose": "parse",
                "sha256sum": paper["sha256"],
            },
        ).json()
        upload_id = quote(str(upload.get("upload_id", upload.get("id", ""))), safe="")
        if not upload_id:
            raise ValueError("Upload response lacks upload_id/id")
        if upload.get("status") == "completed" and upload.get("file"):
            completed = upload
        else:
            self.request(
                "PUT",
                f"/v1/uploads/{upload_id}/content",
                content=path.read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
            completed = self.request("POST", f"/v1/uploads/{upload_id}/complete").json()
        created = self.request(
            "POST",
            "/v1/parse/jobs",
            json={
                "files": [
                    {
                        "source": {"type": "file_id", "file_id": completed["file"]["id"]},
                        "page_range": page_range or f"1-{paper['page_count']}",
                    }
                ],
                "tier": "standard",
                "output_formats": ["structured_content", "middle_json"],
            },
        ).json()
        write(folder / "created.json", created)
        job_id = quote(str(created["job_id"]), safe="")
        while True:
            job = self.request("GET", f"/v1/parse/jobs/{job_id}").json()
            write(folder / "job.json", job)
            if job["status"] == "completed":
                break
            if job["status"] in {"failed", "canceled", "cancelled", "partial", "partial_failed"}:
                raise RuntimeError(f"MinerU job ended: {job['status']}")
            if clock() - start >= timeout:
                raise TimeoutError(f"MinerU job {job_id} timed out; inspect before another run")
            sleep(1)
        outputs = output_files(job)
        for name in ("structured_content", "middle_json"):
            file_id = quote(outputs[name], safe="")
            response = self.request("GET", f"/v1/files/{file_id}/content")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{name}.json").write_bytes(response.content)
        return {"wall_seconds": clock() - start, "parse": find_parse(job)}


def output_files(value):
    """Accept format/file_id records and keyed output maps; never follow server URLs."""
    found = {}

    def visit(obj):
        if isinstance(obj, list):
            for item in obj:
                visit(item)
        elif isinstance(obj, dict):
            fmt = obj.get("format") or obj.get("output_format")
            file_id = obj.get("file_id") or obj.get("output_file_id") or obj.get("id")
            if fmt in {"structured_content", "middle_json"} and isinstance(file_id, str):
                found[fmt] = file_id
            for key, child in obj.items():
                if key in {"structured_content", "middle_json"}:
                    if isinstance(child, str):
                        found[key] = child
                    elif isinstance(child, dict):
                        candidate = child.get("file_id") or child.get("id")
                        if candidate:
                            found[key] = candidate
                visit(child)

    visit(value)
    if set(found) != {"structured_content", "middle_json"}:
        raise ValueError("MinerU job lacks structured_content/middle_json file IDs")
    return found


def find_parse(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("parse"), dict):
            return obj["parse"]
        for child in obj.values():
            found = find_parse(child)
            if found:
                return found
    elif isinstance(obj, list):
        for child in obj:
            found = find_parse(child)
            if found:
                return found
    return {}


def run_mineru(
    root,
    service_pid,
    base_url="http://127.0.0.1:16580",
    max_pages=150,
    cold_start_seconds=None,
    client=None,
    sampler=ResourceSampler,
):
    if not 0 < max_pages <= 150 or service_pid <= 0:
        raise ValueError("Require service PID and page budget 1..150")
    api = LocalMinerU(base_url, client)
    with exclusive(root, "gpu"):
        first = True
        for paper in read(root / "manifest.json")["papers"]:
            key = paper["paper"]
            ledger_file = root / "attempts/mineru.json"
            if ledger_file.exists() and key in read(ledger_file):
                continue
            folder = root / "raw/mineru" / key
            ledger_path, ledger = reserve(root, "mineru", key, paper["page_count"], max_pages)
            try:
                timings, warm = [], []
                with sampler(service_pid) as resource:
                    # One job per physical page yields a real page distribution while
                    # retaining all pages in the fixed per-paper development slice.
                    for page in range(1, paper["page_count"] + 1):
                        timing = api.parse(paper, folder / f"{page:03d}", page_range=str(page))
                        duration = timing["parse"].get("duration_ms")
                        cache_hit = bool(timing["parse"].get("cache_hit", False))
                        seconds = duration / 1000 if duration is not None else None
                        timings.append(
                            {
                                "page": page,
                                "seconds": seconds,
                                "cold": first,
                                "wall_seconds": timing["wall_seconds"],
                                "cache_hit": cache_hit,
                            }
                        )
                        write(folder / f"{page:03d}" / "timing.json", timings[-1])
                        if not first and not cache_hit and seconds is not None:
                            warm.append(seconds)
                        first = False
                write(
                    folder / "metrics.json",
                    {
                        **resource.result,
                        "paper": key,
                        "page_timings": timings,
                        "cold_start_seconds": cold_start_seconds,
                        "warm_page_seconds": warm,
                        "timing_note": (
                            "Single-page jobs; first inference excluded; service startup external"
                        ),
                    },
                )
                ledger[key]["status"] = "completed"
            except Exception as exc:
                ledger[key].update(status="failed", error=redact_error(exc, max_length=None))
                raise
            finally:
                write(ledger_path, ledger)


def run_paddle(
    root,
    python,
    max_pages=150,
    popen=subprocess.Popen,
    sampler=ResourceSampler,
    timeout=7200,
    cpu=False,
):
    if not 0 < max_pages <= 150:
        raise ValueError("Page budget must be 1..150")
    with exclusive(root, "gpu"):
        papers = read(root / "manifest.json")["papers"]
        ledger_path = root / "attempts/paddle.json"
        old = read(ledger_path) if ledger_path.exists() else {}
        papers = [p for p in papers if p["paper"] not in old]
        needed = sum(p["page_count"] for p in papers)
        if not papers:
            return
        if sum(v["units"] for v in old.values()) + needed > max_pages:
            raise ValueError("Paddle cumulative page budget exceeded")
        request = []
        for paper in papers:
            if digest(Path(paper["pdf"])) != paper["sha256"]:
                raise ValueError("PDF hash changed since prepare")
            pages = read(root / "prepared" / f"{paper['paper']}.json")
            request.append({"paper": paper["paper"], "pages": pages})
        # Reserve the complete batch before loading the model, including interrupted work.
        for paper in papers:
            ledger_path, old = reserve(
                root, "paddle", paper["paper"], paper["page_count"], max_pages
            )
        folder = root / "raw/paddle"
        write(folder / "request.json", request)
        env = {
            **os.environ,
            "PADDLE_PDX_CACHE_HOME": str(PROJECT / "data/tools/parsers/paddleocr-vl/home"),
        }
        command = [
            str(python),
            str(Path(__file__).with_name("paddle_worker.py")),
            str(folder / "request.json"),
            str(folder),
            "cpu" if cpu else "gpu:0",
        ]
        try:
            with (
                (folder / "stdout.log").open("w", encoding="utf-8") as stdout,
                (folder / "stderr.log").open("w", encoding="utf-8") as stderr,
            ):
                process = popen(command, stdout=stdout, stderr=stderr, env=env)
                try:
                    with sampler(process.pid, cpu=cpu) as resource:
                        code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
            write(folder / "resources.json", resource.result)
            if code:
                raise RuntimeError(f"Paddle worker failed ({code}); inspect stderr.log")
            for paper in papers:
                metrics = folder / paper["paper"] / "metrics.json"
                if not metrics.exists():
                    raise ValueError("Worker returned without metrics")
                write(
                    metrics,
                    {
                        **read(metrics),
                        **resource.result,
                        "resource_scope": "whole-batch-upper-bound",
                    },
                )
                old[paper["paper"]]["status"] = "completed"
        except Exception as exc:
            for paper in papers:
                if old[paper["paper"]]["status"] == "started":
                    old[paper["paper"]].update(
                        status="failed", error=redact_error(exc, max_length=None)
                    )
            raise
        finally:
            write(ledger_path, old)
