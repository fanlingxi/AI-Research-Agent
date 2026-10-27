"""Project-configured text judge; one attempt per packet, persistent hard cap."""

from __future__ import annotations

import json
import time
from dataclasses import replace

from .common import exclusive, read, reserve, write
from .packets import LINE_INSTRUCTIONS, expand_line_answer, validate_answer


def judge_deepseek(root, max_calls=12, client=None):
    if not 0 <= max_calls <= 12:
        raise ValueError("DeepSeek line annotation budget must be 0..12")
    packet_ids = read(root / "packets/index.json")["line_packets"]
    if (root / "manifest.json").exists():
        expected = [f"lines-{p['paper']}" for p in read(root / "manifest.json")["papers"]]
        if packet_ids != expected:
            raise ValueError("Line packets do not match root manifest")
    calls, failures = 0, 0
    with exclusive(root, "judge-deepseek"):
        ledger_path = root / "attempts/judge-deepseek.json"
        ledger = read(ledger_path) if ledger_path.exists() else {}
        for packet_id in packet_ids:
            if packet_id in ledger:
                continue
            if sum(r["units"] for r in ledger.values()) >= max_calls:
                break
            packet = read(root / "packets/lines" / f"{packet_id}.json")
            if client is None:
                from app.config.settings import Settings
                from app.llms.provider import LangChainChatClient, get_llm_client

                client = get_llm_client(Settings())
                if not isinstance(client, LangChainChatClient):
                    raise ValueError("A real project-configured DeepSeek client is required")
                if "deepseek" not in client.model.lower():
                    raise ValueError("Project LLM must be configured to a DeepSeek model")
                client = replace(
                    client, max_tokens=8192, timeout=300, max_retries=0, thinking_enabled=False
                )
            ledger_path, ledger = reserve(root, "judge-deepseek", packet_id, 1, max_calls)
            calls += 1
            started = time.monotonic()
            try:
                response = client.invoke(json.dumps(packet, ensure_ascii=False), LINE_INSTRUCTIONS)
                # Preserve malformed/truncated responses too; do not silently repair or retry.
                raw_path = root / "judge-deepseek" / f"{packet_id}.response.txt"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(response, encoding="utf-8")
                answer = validate_answer(packet, expand_line_answer(packet, json.loads(response)))
                write(root / "judge-deepseek" / f"{packet_id}.json", answer)
                ledger[packet_id]["status"] = "completed"
            except Exception as exc:
                failures += 1
                ledger[packet_id].update(status="failed", error=type(exc).__name__)
            finally:
                ledger[packet_id].update(
                    seconds=time.monotonic() - started,
                    model=getattr(client, "model", "injected-test-client"),
                    usage=(
                        getattr(client, "last_usage", None)
                        if getattr(client, "last_usage_complete", False)
                        else None
                    ),
                    cost=None,
                )
                write(ledger_path, ledger)
    return {"calls": calls, "failures": failures, "cumulative_calls": len(ledger)}
