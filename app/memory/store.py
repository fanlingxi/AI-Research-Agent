from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from app.schemas.memory import MemoryRecord


class MemoryStore(Protocol):
    """Pluggable durable-memory contract; Graphiti can implement this later."""

    def list_records(self) -> list[MemoryRecord]:
        ...

    def add_record(self, record: MemoryRecord) -> None:
        ...

    def search(self, query: str, limit: int = 3) -> list[MemoryRecord]:
        ...


class JsonMemoryStore:
    """Append-only JSON memory store for local long-term memory."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def list_records(self) -> list[MemoryRecord]:
        if not self.path.exists():
            return []

        payload = json.loads(self.path.read_text())
        return [MemoryRecord.model_validate(item) for item in payload.get("records", [])]

    def add_record(self, record: MemoryRecord) -> None:
        records = self.list_records()
        records.append(record)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"records": [item.model_dump() for item in records]},
                ensure_ascii=False,
                indent=2,
            )
        )

    def search(self, query: str, limit: int = 3) -> list[MemoryRecord]:
        query_tokens = set(_tokens(query))
        scored: list[tuple[float, MemoryRecord]] = []

        for record in self.list_records():
            haystack = " ".join([record.query, record.summary, " ".join(record.tags)])
            record_tokens = set(_tokens(haystack))
            overlap = len(query_tokens & record_tokens)
            if overlap == 0:
                continue

            score = overlap + record.quality_score
            scored.append((score, record))

        return [
            record
            for _, record in sorted(scored, key=lambda item: item[0], reverse=True)
        ][:limit]


def _tokens(text: str) -> list[str]:
    """Create lightweight lexical features for English and Chinese recall."""

    normalized = text.lower()
    tokens = re.findall(r"[a-zA-Z0-9_]+", normalized)

    # Chinese does not use whitespace to separate words. Character n-grams give
    # related research queries useful overlap without introducing a tokenizer dependency.
    for segment in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(segment) == 1:
            tokens.append(segment)
            continue
        for width in (2, 3):
            tokens.extend(
                segment[index : index + width]
                for index in range(len(segment) - width + 1)
            )

    return tokens
