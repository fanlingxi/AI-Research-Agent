"""Standalone Paddle-venv entry point; no application imports or gateway code."""

import argparse
import json
import math
import os
import socket
import time
from pathlib import Path


def offline_only():
    """Fail on attempted downloads/source checks instead of leaving offline mode."""
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["HF_HUB_OFFLINE"] = "1"

    def blocked(*args, **kwargs):
        raise RuntimeError("Table worker is offline; provision model caches separately")

    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked


def process(request, pipeline):
    started = time.perf_counter()
    from PIL import Image

    image = Image.open(request["page_png"]).convert("RGB")
    width, height = request["page_size_pt"]
    x0, y0, x1, y1 = request["bbox_pt"]
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("Invalid crop")
    crop = [
        math.floor(x0 * image.width / width),
        math.floor(y0 * image.height / height),
        math.ceil(x1 * image.width / width),
        math.ceil(y1 * image.height / height),
    ]
    import numpy as np

    # Paddle accepts an OpenCV BGR ndarray; avoid temporary image encoding.
    outputs = list(
        pipeline.predict(np.asarray(image.crop(crop))[:, :, ::-1], use_layout_detection=False)
    )
    if len(outputs) != 1:
        raise ValueError("Expected one crop result")
    payload = outputs[0].json
    if callable(payload):
        payload = payload()
    if isinstance(payload, str):
        payload = json.loads(payload)
    payload = payload.get("res", payload)
    tables = payload["table_res_list"]
    if len(tables) != 1:
        raise ValueError("Expected one table per candidate crop")
    table = tables[0]
    result = {
        key: table[key] for key in ("cell_box_list", "pred_html", "table_ocr_pred") if key in table
    }
    result.update(
        crop_px=crop, image_size_px=list(image.size), elapsed_seconds=time.perf_counter() - started
    )
    try:
        import paddle

        result["resources"] = {
            "paddle_peak_allocated_bytes": int(paddle.device.cuda.max_memory_allocated())
        }
    except Exception:
        result["resources"] = {"paddle_peak_allocated_bytes": None}

    return result


def serializable(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    raise TypeError(type(value).__name__)


def main():
    import contextlib
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    offline_only()
    started = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        from paddleocr import TableRecognitionPipelineV2

        pipeline = TableRecognitionPipelineV2(
            device="gpu:0", use_doc_orientation_classify=False, use_doc_unwarping=False
        )
    cold = time.perf_counter() - started
    if args.stream:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                with contextlib.redirect_stdout(sys.stderr):
                    result = process(request, pipeline)
                result["cold_start_seconds"] = cold
                cold = 0
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(result, ensure_ascii=False, default=serializable), flush=True)
    else:
        if args.request is None or args.output is None:
            parser.error("--request and --output required without --stream")
        result = process(json.loads(args.request.read_text(encoding="utf-8")), pipeline)
        result["cold_start_seconds"] = cold
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, default=serializable), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
