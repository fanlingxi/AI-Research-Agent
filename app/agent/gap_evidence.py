"""Read-only gap discovery over formal bundles and the separate review queue."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.agent.errors import AgentRunConflictError
from app.agent.repository import _sha256
from app.context.ranking import rank_bundles
from app.context.retrieval_audit import bundle_bindings
from app.knowledge.map_view import _CURRENT_CANDIDATE_SQL
from app.retrieval.contracts import validate_candidates
from app.retrieval.hybrid import CHANNEL_LIMIT, bm25_rank

if TYPE_CHECKING:
    from app.agent.service import AgentRunService


PENDING_LIMIT = 500
PENDING_TEXT_LIMIT = 6000
GAP_STATUSES = {"partial", "insufficient_evidence"}


class GapEvidenceRequest(BaseModel):
    output_sha256: str
    # Range errors are report conflicts (409), including negative indices.
    gap_index: int


class RunGapEvidenceService:
    def __init__(self, agents: AgentRunService):
        self.agents = agents
        self.builder = agents.context_builder
        self.memory = agents.knowledge_repository.memory_repository

    def parent(self, run_id: str, request: GapEvidenceRequest):
        run = self.agents.get_run(run_id)
        if run.status != "completed":
            raise AgentRunConflictError("父运行尚未完成。")
        try:
            output = self.agents.repository.get_output(run_id)
        except KeyError as exc:
            raise AgentRunConflictError("父运行尚无输出。") from exc
        digest = _sha256({
            "output_type": output.output_type,
            "structured": output.structured,
            "rendered_text": output.rendered_text,
        })
        if request.output_sha256 != output.output_sha256 or digest != output.output_sha256:
            raise AgentRunConflictError("报告输出已变化，请刷新报告。")
        draft = output.structured.get("draft")
        # Only structured reports state per-question evidence gaps; classic
        # limitations are free-form disclaimers that retrieval cannot fill.
        if not isinstance(draft, dict) or draft.get("answer_status") not in GAP_STATUSES:
            raise AgentRunConflictError("这份报告没有可补充证据的缺口。")
        gaps = draft.get("limitations")
        if (not isinstance(gaps, list) or not 0 <= request.gap_index < len(gaps)
                or not isinstance(gaps[request.gap_index], str)):
            raise AgentRunConflictError("证据缺口不在当前报告中。")
        package = self.agents.load_context_snapshot(run_id)
        return run, package, gaps[request.gap_index]

    def _scopes_tx(self, connection, run, package):
        current = self.memory.snapshot_tx(connection, run.project_id)
        scopes = sorted(set(package.constraints.collection_scopes) & {
            item.collection_slug for item in current.knowledge_scopes
        })
        if not scopes:
            raise AgentRunConflictError("父快照与项目当前知识范围没有交集。")
        return scopes

    def validate_origin(self, run_id, request, *, project_id, task_id, claim_ids):
        run, package, _ = self.parent(run_id, request)
        if run.project_id != project_id or run.task_id != task_id:
            raise AgentRunConflictError("补证据研究必须使用父运行的同一项目和任务。")
        with self.builder.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            return self._validate_claims_tx(connection, run, package, claim_ids)

    def _validate_claims_tx(self, connection, run, package, claim_ids):
        scopes = self._scopes_tx(connection, run, package)
        bundles = self.builder.knowledge_reader.read_formal_bundles_tx(connection, scopes)
        valid_ids = {bundle.claim["id"] for bundle in bundles}
        ignored = {claim_id: "not_formal_in_parent_visible_scope"
                   for claim_id in claim_ids if claim_id not in valid_ids}
        if not any(claim_id in valid_ids for claim_id in claim_ids):
            raise AgentRunConflictError(
                "所选证据均已失效：不在父快照与项目当前范围的交集内，"
                "或不是已审核且来源版本有效的正式知识。请刷新证据缺口。"
            )
        return ignored

    def validate_creation_tx(self, connection, origin, context):
        # The caller holds the creation write lock: parent state cannot change
        # between these local reads and atomic run/event insertion. No external IO.
        self.validate_origin(
            origin["parent_run_id"], GapEvidenceRequest.model_validate(origin),
            project_id=context.project.project_id, task_id=context.task.task_id,
            claim_ids=origin["claim_ids"],
        )
        parent = self.agents.repository._require_run_tx(connection, origin["parent_run_id"])
        if parent["context_snapshot_id"] == context.snapshot_id:
            raise AgentRunConflictError("补证据研究必须创建新快照。")
        self.builder.validate_fresh_context_tx(connection, context)

    def search(self, run_id: str, request: GapEvidenceRequest) -> dict:
        run, package, gap = self.parent(run_id, request)
        query = "\n".join((gap[:2000], package.task.title[:400]))
        with self.builder.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            scopes = self._scopes_tx(connection, run, package)
            bundles = self.builder.knowledge_reader.read_formal_bundles_tx(connection, scopes)
        notices = [
            "待审候选仅作审核提示，不进入研究快照；采用中文兼容的词法检索，跨语言匹配可能遗漏。",
        ]
        references = []
        # Exactly one bounded retrieval, after closing the SQLite read transaction.
        documents = {e.document["id"] for b in bundles for e in b.evidence}
        if documents:
            try:
                references = list(self.builder.vector_retriever.retrieve(
                    query, collection_scopes=scopes, allowed_document_ids=documents,
                    limit=CHANNEL_LIMIT,
                ))[:CHANNEL_LIMIT]
            except Exception:
                notices.append("向量检索不可用，已退回词法检索。")
        # Re-read authority, review state and source versions after external IO.
        current, _, _ = self.parent(run_id, request)
        if current.revision != run.revision:
            raise AgentRunConflictError("父运行已变化，请刷新报告。")
        with self.builder.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            scopes = self._scopes_tx(connection, current, package)
            bundles = self.builder.knowledge_reader.read_formal_bundles_tx(connection, scopes)
            pending, truncated = self._pending_tx(connection, scopes)
        audits = validate_candidates(references, bundle_bindings(bundles))
        vector = any(a.channel == "vector" and a.status == "verified"
                     and (a.raw_score or 0) > 0 for a in audits)
        if not vector and not any("退回" in notice for notice in notices):
            notices.append("未取得有效向量候选，已退回词法检索。")
        if any(a.reason == "sqlite_bound_provider_version_unknown" for a in audits):
            notices.append("部分向量未提供版本标识；已按 SQLite 当前正式证据重新核验。")
        if truncated:
            notices.append(f"待审检索仅覆盖范围内最近 {PENDING_LIMIT} 条候选。")
        ranked = rank_bundles(
            bundles, query=query, candidate_references=references, candidate_audits=audits,
            strategy="hybrid-v1" if vector else "bm25-v1",
        )
        in_snapshot = {b.claim.claim_id for b in package.knowledge.claim_bundles}
        reviewed = [self._reviewed(item.bundle) for item in ranked
                    if item.bundle.claim["id"] not in in_snapshot][:5]
        pending_by_id = {item["candidate_id"]: item for item in pending}
        matches = bm25_rank(query, {
            key: " ".join(str(item[field]) for field in
                          ("title", "summary", "quote", "paper_title"))[:PENDING_TEXT_LIMIT]
            for key, item in pending_by_id.items()
        }, limit=5)
        return {
            "run_id": run_id, "gap_index": request.gap_index, "gap": gap, "scopes": scopes,
            "search": {"method": "vector" if vector else "lexical", "notices": notices},
            "reviewed": reviewed,
            "reviewed_in_snapshot_count": sum(
                item.bundle.claim["id"] in in_snapshot for item in ranked
            ),
            "pending": [pending_by_id[key] for key, _ in matches],
            "can_rerun": bool(reviewed),
        }

    @staticmethod
    def _reviewed(bundle):
        return {
            "claim_id": bundle.claim["id"], "statement": bundle.claim["statement"],
            "evidence": [{
                "evidence_id": item.evidence["id"], "quote": item.evidence["quote"],
                "document_title": item.document["title"],
                "page_start": item.evidence["location"]["page_start"],
                "page_end": item.evidence["location"]["page_end"],
            } for item in bundle.evidence],
        }

    @staticmethod
    def _pending_tx(connection, scopes):
        placeholders = ",".join("?" for _ in scopes)
        # Same authoritative ingestion membership and current-source boundary as
        # the knowledge map. Candidate payload topic slugs never grant scope.
        rows = connection.execute(f"""
            SELECT c.*, ic.collection_slug, d.title AS paper_title,
                json_extract(src.payload_json, '$.name') AS source_name,
                json_extract(dst.payload_json, '$.name') AS target_name
            FROM candidates c
            JOIN ingestion_collections ic ON ic.ingestion_id = c.ingestion_id
            LEFT JOIN documents d ON d.id = json_extract(c.payload_json, '$.evidence.paper_id')
            LEFT JOIN candidates src ON src.id = json_extract(
                c.payload_json, '$.source_candidate_id') AND src.ingestion_id = c.ingestion_id
            LEFT JOIN candidates dst ON dst.id = json_extract(
                c.payload_json, '$.target_candidate_id') AND dst.ingestion_id = c.ingestion_id
            WHERE c.status = 'draft' AND ic.collection_slug IN ({placeholders})
              AND {_CURRENT_CANDIDATE_SQL}
            ORDER BY c.created_at DESC, c.id LIMIT ?
        """, [*scopes, PENDING_LIMIT + 1]).fetchall()
        results = []
        for row in rows[:PENDING_LIMIT]:
            payload = json.loads(row["payload_json"])
            evidence = payload.get("evidence") or {}
            title = payload.get("name", "未命名实体")
            if row["kind"] == "relation":
                title = (f"{row['source_name'] or '未解析实体'} —{payload.get('type', '')}→ "
                         f"{row['target_name'] or '未解析实体'}")
            prereview = (payload.get("metadata") or {}).get("prereview")
            decision = (prereview.get("decision", prereview.get("verdict"))
                        if isinstance(prereview, dict) else None)
            if decision == "unsure":
                decision = "uncertain"
            results.append({
                "candidate_id": row["id"], "kind": row["kind"], "title": title,
                "summary": payload.get("summary", ""), "quote": evidence.get("quote", ""),
                "paper_title": row["paper_title"] or "",
                "page_start": evidence.get("page_start"), "page_end": evidence.get("page_end"),
                "ingestion_id": row["ingestion_id"], "collection_slug": row["collection_slug"],
                "prereview": {"decision": decision,
                              "reason": prereview.get("reason", "")}
                if decision in {"approve", "reject", "uncertain"} else None,
            })
        return results, len(rows) > PENDING_LIMIT

    def list_reruns(self, run_id: str) -> list[dict]:
        parent = self.agents.get_run(run_id)
        with self.builder.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute("""
                SELECT id, request_json, target_resource_type, target_resource_id
                FROM research_commands WHERE mode = 'project_run' AND project_id = ?
                  AND json_extract(request_json, '$.gap_evidence_of.run_id') = ?
                ORDER BY created_at, id
            """, (parent.project_id, run_id)).fetchall()
        result = []
        for row in rows:
            origin = json.loads(row["request_json"])["gap_evidence_of"]
            run, artifact = None, None
            if row["target_resource_type"] == "agent_run" and row["target_resource_id"]:
                run = self.agents.get_run(row["target_resource_id"])
                try:
                    output = self.agents.repository.get_output(run.id)
                    artifact = self.agents.follow_up._report_artifact(run, output)
                except (KeyError, AgentRunConflictError):
                    pass
            result.append({
                "gap_index": origin["gap_index"], "command_id": row["id"],
                "run": run.model_dump() if run else None,
                "artifact": artifact.model_dump() if artifact else None,
            })
        return result
