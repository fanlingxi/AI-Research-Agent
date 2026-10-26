"""Local sampled process-tree/device resources; missing WDDM counters remain unknown."""

from __future__ import annotations

import json
import os
import subprocess
import threading

from app.benchmarking.parser_trial.resources import process_tree_bytes


def process_ids(pid, run):
    try:
        import psutil

        return {pid, *(p.pid for p in psutil.Process(pid).children(recursive=True))}
    except ImportError:
        output = run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | "
                "ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout
        rows = json.loads(output)
        rows = rows if isinstance(rows, list) else [rows]
        ids = {pid}
        while True:
            added = {int(r["ProcessId"]) for r in rows if int(r["ParentProcessId"]) in ids}
            if added <= ids:
                return ids
            ids |= added


def observe(pid, run=subprocess.run):
    result = dict.fromkeys(
        (
            "process_tree_rss_bytes",
            "process_vram_bytes",
            "whole_device_vram_bytes",
            "shared_gpu_memory_bytes",
        )
    )
    try:
        result["process_tree_rss_bytes"] = process_tree_bytes(pid, run=run)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    ids = None
    try:
        ids = process_ids(pid, run)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    try:
        output = run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout
        values = [
            v.strip()
            for line in output.splitlines()
            if "," in line
            for p, v in [line.split(",", 1)]
            if ids is not None and p.strip().isdigit() and int(p) in ids
        ]
        if values and all(v.replace(".", "", 1).isdigit() for v in values):
            result["process_vram_bytes"] = sum(float(v) for v in values) * 1024**2
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        output = run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout
        result["whole_device_vram_bytes"] = sum(float(v) for v in output.splitlines()) * 1024**2
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if os.name == "nt" and ids is not None:
        try:
            output = run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-Counter '\\GPU Process Memory(*)\\Shared Usage' -ErrorAction Stop | "
                    "Select-Object -ExpandProperty CounterSamples | "
                    "Select-Object InstanceName,CookedValue | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=3,
            ).stdout
            samples = json.loads(output)
            samples = samples if isinstance(samples, list) else [samples]
            values = [
                float(sample["CookedValue"])
                for sample in samples
                if any(f"pid_{p}_" in sample["InstanceName"].lower() for p in ids)
            ]
            if values:
                result["shared_gpu_memory_bytes"] = sum(values)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass
    return result


class Resources:
    def __init__(self, sampler=observe, interval=0.5):
        self.sampler, self.interval = sampler, interval
        self.stop = threading.Event()
        self.result = {
            "sample_interval_seconds": interval,
            "scope": "pilot-and-descendants",
            "process_tree_rss_bytes": None,
            "process_vram_bytes": None,
            "whole_device_vram_bytes": None,
            "shared_gpu_memory_bytes": None,
            "errors": [],
            "method": "sampled maxima; not exact OS high-water marks",
        }

    def sample(self):
        try:
            for key, value in self.sampler(os.getpid()).items():
                if value is not None:
                    self.result[key] = max(self.result.get(key) or 0, value)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error not in self.result["errors"]:
                self.result["errors"].append(error)

    def loop(self):
        while not self.stop.wait(self.interval):
            self.sample()

    def __enter__(self):
        self.sample()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=20)
        self.sample()
