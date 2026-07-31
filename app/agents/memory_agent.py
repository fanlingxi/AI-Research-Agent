from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.config.settings import get_settings
from app.memory.store import JsonMemoryStore
from app.schemas.memory import MemoryRecord, MemorySnapshot
from app.schemas.quality import EvaluationResult


class MemoryAgent:
    """Retrieve and persist long-term research memory."""

    def __init__(
        self,
        store: JsonMemoryStore | None = None,
        enabled: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.enabled = settings.memory_enabled if enabled is None else enabled
        self.store = store or JsonMemoryStore(settings.memory_path)
        self.recall_limit = settings.memory_recall_limit

    def recall(self, query: str) -> MemorySnapshot:
        if not self.enabled:
            return MemorySnapshot(summary="本次运行已关闭长期记忆。")

        records = self.store.search(query=query, limit=self.recall_limit)
        if not records:
            return MemorySnapshot(summary="未召回相关历史研究记忆。")

        lines = ["召回到的相关历史研究记忆："]
        for record in records:
            lines.append(
                f"- {record.query} | 分数={record.quality_score:.2f} | {record.summary}"
            )
        return MemorySnapshot(records=records, summary="\n".join(lines))

    def remember(
        self,
        query: str,
        report: str,
        evaluation: EvaluationResult,
        tags: list[str] | None = None,
    ) -> MemoryRecord | None:
        if not self.enabled:
            return None

        created_at = datetime.now(tz=UTC).isoformat()
        record_id = hashlib.sha1(f"{query}|{created_at}".encode()).hexdigest()[:16]
        record = MemoryRecord(
            id=f"memory:{record_id}",
            query=query,
            summary=self._summarize_report(report),
            created_at=created_at,
            quality_score=evaluation.overall_score,
            tags=tags or [],
            metadata={
                "evaluation_passed": evaluation.passed,
                "metric_count": len(evaluation.metrics),
            },
        )
        self.store.add_record(record)
        return record

    def _summarize_report(self, report: str, max_chars: int = 360) -> str:
        normalized = " ".join(report.split())
        return normalized[:max_chars]
