"""Standalone Paddle environment worker. No imports from the main application."""

import importlib.metadata
import json
import struct
import sys
import time
from pathlib import Path


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    request_path, output_path, device = sys.argv[1:]
    started = time.perf_counter()
    from paddleocr import PaddleOCRVL

    pipeline = PaddleOCRVL(pipeline_version="v1.6", device=device)
    cold = time.perf_counter() - started
    first = True
    for paper in json.loads(Path(request_path).read_text(encoding="utf-8")):
        folder = Path(output_path) / paper["paper"]
        timings, warm = [], []
        for page in paper["pages"]:
            png = Path(page["png"])
            with png.open("rb") as stream:
                header = stream.read(24)
            width, height = struct.unpack(">II", header[16:24])
            start = time.perf_counter()
            results = list(pipeline.predict(input=str(png)))
            seconds = time.perf_counter() - start
            if len(results) != 1:
                raise ValueError("Expected one Paddle result per PNG")
            value = results[0].json
            value = value() if callable(value) else value
            value = json.loads(value) if isinstance(value, str) else value
            save(
                folder / f"{page['page']:03d}.json",
                {
                    "raw": value,
                    "image_width": width,
                    "image_height": height,
                    "parser_version": importlib.metadata.version("paddleocr"),
                    "page": page["page"],
                    "seconds": seconds,
                    "cold_inference": first,
                },
            )
            timings.append({"page": page["page"], "seconds": seconds, "cold": first})
            if not first:
                warm.append(seconds)
            first = False
            save(
                folder / "metrics.json",
                {
                    "paper": paper["paper"],
                    "cold_start_seconds": cold,
                    "page_timings": timings,
                    "warm_page_seconds": warm,
                },
            )


if __name__ == "__main__":
    main()
