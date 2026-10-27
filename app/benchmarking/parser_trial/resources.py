"""Sample process-tree RSS and device-wide VRAM (including desktop usage).

Missing observations remain null. Device-wide sampling is a conservative upper bound,
not a fabricated per-process allocation on Windows/WDDM.
"""

from __future__ import annotations

import json
import subprocess
import threading


def process_tree_bytes(pid, run=subprocess.run):
    try:
        import psutil

        parent = psutil.Process(pid)
        return sum(p.memory_info().rss for p in [parent, *parent.children(recursive=True)])
    except ImportError:
        # CIM supplies ParentProcessId, unlike tasklist, so descendants are included.
        result = run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object ProcessId,"
                "ParentProcessId,WorkingSetSize | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        rows = json.loads(result.stdout)
        rows = rows if isinstance(rows, list) else [rows]
        ids = {pid}
        while True:
            added = {int(r["ProcessId"]) for r in rows if int(r["ParentProcessId"]) in ids}
            if added <= ids:
                break
            ids |= added
        matches = [int(r["WorkingSetSize"]) for r in rows if int(r["ProcessId"]) in ids]
        return sum(matches) if matches else None


def gpu_bytes(run=subprocess.run):
    result = run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return sum(float(v.strip()) for v in result.stdout.splitlines()) * 1024**2


class ResourceSampler:
    def __init__(self, pid, interval=0.5, cpu=False):
        self.pid, self.interval, self.cpu = pid, interval, cpu
        self.stop = threading.Event()
        self.result = {
            "peak_memory_bytes": None,
            "peak_vram_bytes": 0 if cpu else None,
            "vram_scope": "cpu" if cpu else "whole-device-including-desktop",
            "resource_errors": [],
            "sample_interval_seconds": interval,
        }

    def sample(self):
        measurements = [("peak_memory_bytes", lambda: process_tree_bytes(self.pid))]
        if not self.cpu:
            measurements.append(("peak_vram_bytes", gpu_bytes))
        for name, measure in measurements:
            try:
                value = measure()
                if value is not None:
                    self.result[name] = max(self.result[name] or 0, value)
            except Exception as exc:
                error = f"{name}: {type(exc).__name__}"
                if error not in self.result["resource_errors"]:
                    self.result["resource_errors"].append(error)

    def _loop(self):
        while not self.stop.wait(self.interval):
            self.sample()

    def __enter__(self):
        self.sample()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=25)
