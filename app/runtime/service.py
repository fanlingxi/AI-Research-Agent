from __future__ import annotations

import base64
import json
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from app.config.settings import Settings
from app.knowledge.repository import KnowledgeRepository
from app.runtime.schemas import (
    ProjectionBacklog,
    RuntimeExecutorState,
    RuntimeOverview,
    RuntimeServiceState,
    RuntimeWorkItem,
    RuntimeWorkPage,
)

_ACTIVE_AGENT_STATUSES = {"created", "queued", "preparing", "running", "validating"}
_RETRYABLE_KINDS = {
    "ingestion",
    "report",
    "agent_run",
    "research_command",
    "projection",
    "projection_rebuild",
}


class RuntimeObservabilityService:
    """Build bounded operational projections without creating a second truth store."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        settings: Settings,
        *,
        llm_provider: str,
        live_llm_configured: bool,
        connection_probe: Callable[[str], bool] | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.llm_provider = llm_provider
        self.live_llm_configured = live_llm_configured
        self.connection_probe = connection_probe or _tcp_probe

    def overview(self) -> RuntimeOverview:
        now = datetime.now(tz=UTC)
        executors = []
        for heartbeat in self.repository.jobs.list_executor_heartbeats():
            last_seen = datetime.fromisoformat(heartbeat.last_heartbeat_at)
            online = (now - last_seen).total_seconds() <= max(
                5.0, self.settings.knowledge_worker_poll_seconds * 3
            )
            executors.append(
                RuntimeExecutorState(
                    **heartbeat.model_dump(),
                    online=online,
                )
            )
        worker_online = any(item.online and item.role == "worker" for item in executors)
        vault = Path(self.settings.knowledge_vault_path)
        services = {
            "api": RuntimeServiceState(available=True, detail="FastAPI 编排服务在线"),
            "worker": RuntimeServiceState(
                available=worker_online,
                detail=(
                    "统一 Worker 在线"
                    if worker_online
                    else "统一 Worker 离线；请启动 python -m app.worker"
                ),
            ),
            "llm": RuntimeServiceState(
                configured=self.live_llm_configured,
                available=self.live_llm_configured,
                detail=(
                    f"{self.llm_provider} 已配置"
                    if self.live_llm_configured
                    else "未配置真实 LLM；入库和研究报告会拒绝执行"
                ),
            ),
            "qdrant": self._external_service("Qdrant", self.settings.qdrant_url),
            "neo4j": self._external_service("Neo4j", self.settings.neo4j_uri),
            "vault": RuntimeServiceState(
                available=vault.exists() and vault.is_dir(),
                detail=(
                    f"Vault 可访问 · {vault}"
                    if vault.exists() and vault.is_dir()
                    else f"Vault 目录不存在 · {vault}"
                ),
                endpoint=str(vault),
            ),
        }
        with self.repository._connect() as connection:
            count_rows = connection.execute(
                """
                SELECT kind, status, COUNT(*) AS count
                FROM knowledge_jobs GROUP BY kind, status
                UNION ALL
                SELECT 'projection' AS kind, status, COUNT(*) AS count
                FROM projection_outbox GROUP BY status
                """
            ).fetchall()
            projection_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM projection_outbox GROUP BY status"
            ).fetchall()
            oldest = connection.execute(
                "SELECT MIN(created_at) AS value FROM projection_outbox WHERE status = 'queued'"
            ).fetchone()["value"]
        work_counts: dict[str, dict[str, int]] = {}
        for row in count_rows:
            work_counts.setdefault(str(row["kind"]), {})[str(row["status"])] = int(row["count"])
        projection_counts = {str(row["status"]): int(row["count"]) for row in projection_rows}
        return RuntimeOverview(
            status="ok" if all(service.available for service in services.values()) else "degraded",
            generated_at=now.isoformat(),
            services=services,
            executors=executors,
            work_counts=work_counts,
            projection_backlog=ProjectionBacklog(
                **projection_counts,
                oldest_queued_at=str(oldest) if oldest else None,
            ),
        )

    def list_work(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 30,
    ) -> RuntimeWorkPage:
        if limit < 1 or limit > 100:
            raise ValueError("Runtime work page limit must be between 1 and 100.")
        cursor_values = _decode_cursor(cursor) if cursor else None
        clauses: list[str] = []
        parameters: list[object] = []
        if kind:
            clauses.append("kind = ?")
            parameters.append(kind)
        if status:
            clauses.append("(job_status = ? OR business_status = ?)")
            parameters.extend((status, status))
        if cursor_values:
            clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            parameters.extend((cursor_values[0], cursor_values[0], cursor_values[1]))
        query = _WORK_UNION_SQL
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        parameters.append(limit + 1)
        with self.repository._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = [self._work_item(row) for row in selected]
        next_cursor = (
            _encode_cursor(items[-1].updated_at, items[-1].id)
            if has_more and items
            else None
        )
        return RuntimeWorkPage(items=items, next_cursor=next_cursor)

    def retry_projection(self, event_id: str) -> RuntimeWorkItem:
        self.repository.retry_projection(event_id)
        with self.repository._connect() as connection:
            row = connection.execute(
                _WORK_UNION_SQL + " WHERE id = ? AND kind = 'projection'",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._work_item(row)

    def _external_service(self, label: str, endpoint: str) -> RuntimeServiceState:
        available = self.connection_probe(endpoint)
        return RuntimeServiceState(
            available=available,
            detail=f"{label} {'连接正常' if available else '无法连接'}",
            endpoint=endpoint,
        )

    @staticmethod
    def _work_item(row) -> RuntimeWorkItem:
        kind = str(row["kind"])
        business_status = str(row["business_status"] or row["job_status"])
        return RuntimeWorkItem(
            id=str(row["id"]),
            kind=kind,
            resource_id=str(row["resource_id"]),
            title=str(row["title"] or row["resource_id"]),
            detail_route=str(row["detail_route"]),
            business_status=business_status,
            job_status=str(row["job_status"]),
            current_stage=str(row["current_stage"]) if row["current_stage"] else None,
            attempt=int(row["attempt"] or 0),
            priority=int(row["priority"] or 0),
            queue_position=(
                int(row["queue_position"]) if row["queue_position"] is not None else None
            ),
            lease_until=str(row["lease_until"]) if row["lease_until"] else None,
            executor=str(row["executor"]) if row["executor"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_error=str(row["last_error"]) if row["last_error"] else None,
            can_retry=kind in _RETRYABLE_KINDS and str(row["job_status"]) == "failed",
            can_cancel=kind == "agent_run" and business_status in _ACTIVE_AGENT_STATUSES,
        )


_WORK_UNION_SQL = """
WITH unified AS (
    SELECT
        job.id AS id,
        job.kind AS kind,
        job.resource_id AS resource_id,
        CASE job.kind
            WHEN 'ingestion' THEN COALESCE(ing.topic, job.resource_id)
            WHEN 'report' THEN COALESCE(rep.query, job.resource_id)
            WHEN 'agent_run' THEN COALESCE(task.title, job.resource_id)
            WHEN 'collection_sync' THEN 'Collection 投影同步'
            WHEN 'projection_rebuild' THEN '投影重建 · ' ||
                json_extract(job.payload_json, '$.target') || ' · ' ||
                COALESCE(json_extract(job.payload_json, '$.collection_slug'), '全部 Collection')
            WHEN 'research_command' THEN COALESCE(command.instruction, job.resource_id)
            ELSE job.resource_id
        END AS title,
        CASE job.kind
            WHEN 'ingestion' THEN '/knowledge?ingestion=' || job.resource_id
            WHEN 'report' THEN '/reports?report=' || job.resource_id
            WHEN 'agent_run' THEN '/agent-runs/' || job.resource_id
            WHEN 'research_command' THEN COALESCE(command.target_route, '/')
            WHEN 'projection_rebuild' THEN '/runtime?rebuild=' || job.id
            ELSE '/runtime'
        END AS detail_route,
        CASE job.kind
            WHEN 'ingestion' THEN ing.status
            WHEN 'report' THEN rep.status
            WHEN 'agent_run' THEN run.status
            WHEN 'research_command' THEN command.status
            ELSE job.status
        END AS business_status,
        job.status AS job_status,
        CASE job.kind
            WHEN 'report' THEN json_extract(rep.run_metadata_json, '$.current_stage')
            WHEN 'agent_run' THEN run.current_node
            WHEN 'ingestion' THEN ing.status
            WHEN 'collection_sync' THEN 'collection_sync'
            WHEN 'projection_rebuild' THEN 'projection_rebuild'
            WHEN 'research_command' THEN command.orchestration_stage
            ELSE NULL
        END AS current_stage,
        job.attempts AS attempt,
        job.priority AS priority,
        CASE WHEN job.status = 'queued' THEN 1 + (
            SELECT COUNT(*) FROM knowledge_jobs ahead
            WHERE ahead.status = 'queued' AND (
                ahead.priority > job.priority OR
                (ahead.priority = job.priority AND ahead.created_at < job.created_at) OR
                (ahead.priority = job.priority AND ahead.created_at = job.created_at
                    AND ahead.id < job.id)
            )
        ) ELSE NULL END AS queue_position,
        job.lease_until AS lease_until,
        job.lease_owner AS executor,
        job.created_at AS created_at,
        job.updated_at AS updated_at,
        COALESCE(
            job.last_error, ing.error, rep.error, run.error_message, command.error
        ) AS last_error
    FROM knowledge_jobs job
    LEFT JOIN ingestions ing ON job.kind = 'ingestion' AND ing.id = job.resource_id
    LEFT JOIN reports rep ON job.kind = 'report' AND rep.id = job.resource_id
    LEFT JOIN agent_runs run ON job.kind = 'agent_run' AND run.id = job.resource_id
    LEFT JOIN workspace_tasks task ON run.task_id = task.id
    LEFT JOIN research_commands command
        ON job.kind = 'research_command' AND command.id = job.resource_id

    UNION ALL

    SELECT
        event.id AS id,
        'projection' AS kind,
        event.aggregate_id AS resource_id,
        CASE event.aggregate_type
            WHEN 'relation' THEN '图关系投影 · ' || event.aggregate_id
            ELSE '知识实体投影 · ' || event.aggregate_id
        END AS title,
        '/runtime?projection=' || event.id AS detail_route,
        COALESCE(ing.status, event.status) AS business_status,
        event.status AS job_status,
        event.aggregate_type || '_projection' AS current_stage,
        event.attempts AS attempt,
        0 AS priority,
        CASE WHEN event.status = 'queued' THEN
            (SELECT COUNT(*) FROM knowledge_jobs WHERE status = 'queued') + 1 + (
                SELECT COUNT(*) FROM projection_outbox ahead
                WHERE ahead.status = 'queued' AND (
                    ahead.created_at < event.created_at OR
                    (ahead.created_at = event.created_at AND ahead.id < event.id)
                )
            )
        ELSE NULL END AS queue_position,
        event.lease_until AS lease_until,
        event.lease_owner AS executor,
        event.created_at AS created_at,
        event.updated_at AS updated_at,
        event.last_error AS last_error
    FROM projection_outbox event
    LEFT JOIN ingestions ing ON ing.id = event.ingestion_id
)
SELECT * FROM unified
"""


def _tcp_probe(endpoint: str) -> bool:
    parsed = urlparse(endpoint if "://" in endpoint else f"bolt://{endpoint}")
    host = parsed.hostname or "localhost"
    port = parsed.port or (7687 if parsed.scheme == "bolt" else 6333)
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _encode_cursor(updated_at: str, item_id: str) -> str:
    raw = json.dumps([updated_at, item_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid Runtime work cursor.") from exc
    if not isinstance(values, list) or len(values) != 2 or not all(
        isinstance(value, str) for value in values
    ):
        raise ValueError("Invalid Runtime work cursor.")
    return values[0], values[1]
