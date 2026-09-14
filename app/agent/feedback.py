"""Run-bound feedback and review history, using the platform's existing queue."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

from app.agent.errors import AgentRunConflictError
from app.agent.feedback_models import (
    FeedbackDecision,
    FeedbackRecheck,
    FeedbackRequest,
    FeedbackRerunRequest,
)
from app.agent.models import AgentRunCreateRequest
from app.agent.repository import _dump, _now, _sha256

if TYPE_CHECKING:
    from app.agent.service import AgentRunService


REVIEWABLE = {"completed", "failed", "needs_review", "stale_context", "cancelled"}


class RunFeedbackService:
    def __init__(self, agents: AgentRunService):
        self.agents = agents
        self.repository = agents.repository

    def create(self, run_id: str, request: FeedbackRequest) -> dict:
        run = self.agents.get_run(run_id)
        package = self.agents.context_builder.snapshot_repository.get(run.context_snapshot_id)
        if package.package_sha256 != run.context_sha256:
            raise AgentRunConflictError("Feedback snapshot differs from the original run.")
        evidence = {
            e.evidence_id: {
                **e.model_dump(mode="json"),
                "source_version": next(
                    s.version for s in bundle.sources if s.source_id == e.source_id
                ),
            }
            for bundle in package.knowledge.claim_bundles
            for e in bundle.evidence
        }
        with self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM agent_run_feedback WHERE run_id = ? AND idempotency_key = ?",
                (run_id, request.idempotency_key),
            ).fetchone()
            if prior:
                self._same(prior["request_json"], request.model_dump(), "feedback")
                return self._feedback(prior)
            current = self.repository._require_run_tx(connection, run_id)
            if current["revision"] != run.revision or current["status"] not in REVIEWABLE:
                raise AgentRunConflictError("Feedback requires an unchanged terminal run.")
            output = connection.execute(
                "SELECT * FROM agent_run_outputs WHERE run_id = ?", (run_id,)
            ).fetchone()
            structured = json.loads(output["structured_json"]) if output else {}
            if output and output["output_sha256"] != _sha256(
                {
                    "output_type": output["output_type"],
                    "structured": structured,
                    "rendered_text": output["rendered_text"],
                }
            ):
                raise AgentRunConflictError("Feedback output failed its stored integrity check.")
            findings = structured.get("draft", {}).get("findings", [])
            assertion = None
            if request.finding_index is not None:
                if request.finding_index > len(findings):
                    raise AgentRunConflictError("Finding is not in this run's stored output.")
                finding = findings[request.finding_index - 1]
                assertion = finding["assertion"]
                if not set(request.evidence_ids).issubset(finding["evidence_ids"]):
                    raise AgentRunConflictError("Evidence does not belong to the selected finding.")
            if len(set(request.evidence_ids)) != len(request.evidence_ids):
                raise AgentRunConflictError("Duplicate evidence references.")
            if not set(request.evidence_ids).issubset(evidence):
                raise AgentRunConflictError("Evidence is outside the run snapshot.")
            anchor = {
                "project_id": run.project_id,
                "task_id": run.task_id,
                "snapshot_id": run.context_snapshot_id,
                "snapshot_sha256": run.context_sha256,
                "output_id": output["id"] if output else None,
                "output_sha256": output["output_sha256"] if output else None,
                "artifact_id": structured.get("artifact_id"),
                "assertion": assertion,
                "evidence": [evidence[key] for key in request.evidence_ids],
                "run_status": run.status,
                "run_revision": run.revision,
                "error_code": run.error_code,
                "error_message": run.error_message,
            }
            identifier, now = f"feedback-{uuid4().hex}", _now()
            connection.execute(
                """INSERT INTO agent_run_feedback
                (id, run_id, idempotency_key, request_json, anchor_json, status,
                 created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    identifier,
                    run_id,
                    request.idempotency_key,
                    _dump(request.model_dump()),
                    _dump(anchor),
                    now,
                    now,
                ),
            )
            self._event(connection, run_id, "feedback_created", {"feedback_id": identifier})
            return self._feedback(self._get_tx(connection, identifier))

    def list(self, run_id: str) -> dict:
        with self.repository._connect() as connection:
            connection.execute("BEGIN")
            self.repository._require_run_tx(connection, run_id)
            feedback = [
                self._feedback(row)
                for row in connection.execute(
                    "SELECT * FROM agent_run_feedback WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                )
            ]
            links = [
                self._link(row)
                for row in connection.execute(
                    """SELECT r.*, a.status AS child_status,
                a.context_snapshot_id AS child_snapshot_id,
                o.id AS child_output_id, o.structured_json AS child_output_json
                FROM agent_run_rechecks r JOIN agent_runs a ON a.id = r.child_run_id
                LEFT JOIN agent_run_outputs o ON o.run_id = a.id
                WHERE r.parent_run_id = ? OR r.child_run_id = ? ORDER BY r.created_at, r.id""",
                    (run_id, run_id),
                )
            ]
            return {"feedback": feedback, "links": links}

    def decide(self, run_id: str, feedback_id: str, request: FeedbackDecision) -> dict:
        with self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_tx(connection, feedback_id, run_id)
            if row["decision_json"]:
                self._same(row["decision_json"], request.model_dump(), "review decision")
                return self._feedback(row)
            if row["revision"] != request.expected_revision or row["status"] != "pending":
                raise AgentRunConflictError("Feedback revision or review state changed.")
            connection.execute(
                """UPDATE agent_run_feedback SET status = ?, decision_json = ?,
                revision = revision + 1, updated_at = ? WHERE id = ?""",
                (request.decision, _dump(request.model_dump()), _now(), feedback_id),
            )
            self._event(connection, run_id, "feedback_reviewed", {"feedback_id": feedback_id})
            return self._feedback(self._get_tx(connection, feedback_id))

    def rerun(self, run_id: str, feedback_id: str, request: FeedbackRerunRequest):
        # Before context building, make duplicate delivery cheap and side-effect free.
        with self.repository._connect() as connection:
            connection.execute("BEGIN")
            existing = self.check_rerun_tx(connection, run_id, feedback_id, request)
            if existing:
                return existing
            parent = self.repository._run_from_row(
                self.repository._require_run_tx(connection, run_id)
            )
        return self.agents._create_run(
            parent.project_id,
            parent.task_id,
            AgentRunCreateRequest(
                workflow=parent.options.workflow,
                create_memory_proposal=parent.options.create_memory_proposal,
                max_steps=parent.max_steps,
                max_tool_calls=parent.max_tool_calls,
                token_budget=(request.token_budget if request.token_budget is not None
                              else parent.token_budget),
                context_max_tokens=request.context_max_tokens,
            ),
            feedback_rerun=(run_id, feedback_id, request, parent.revision),
        )

    def check_rerun_tx(self, connection, run_id, feedback_id, request):
        row = self._get_tx(connection, feedback_id, run_id)
        prior = connection.execute(
            "SELECT * FROM agent_run_rechecks WHERE feedback_id = ? AND idempotency_key = ?",
            (feedback_id, request.idempotency_key),
        ).fetchone()
        if prior:
            self._same(prior["request_json"], request.model_dump(exclude_none=True), "rerun")
            return self.repository._run_from_row(
                self.repository._require_run_tx(connection, prior["child_run_id"])
            )
        if row["status"] != "accepted" or row["revision"] != request.expected_revision:
            raise AgentRunConflictError("Rerun requires accepted feedback at the current revision.")
        parent = self.repository._require_run_tx(connection, run_id)
        if (
            parent["status"] not in REVIEWABLE
            or parent["revision"] != json.loads(row["anchor_json"])["run_revision"]
        ):
            raise AgentRunConflictError(
                "Original run changed; submit feedback for the current result."
            )
        if connection.execute(
            "SELECT 1 FROM agent_run_rechecks WHERE feedback_id = ? AND recheck_json IS NULL",
            (feedback_id,),
        ).fetchone():
            raise AgentRunConflictError("Recheck the previous attempt before starting another.")
        return None

    def link_rerun_tx(self, connection, parent, feedback_id, request, child):
        identifier = f"recheck-{uuid4().hex}"
        connection.execute(
            """INSERT INTO agent_run_rechecks
            (id, feedback_id, idempotency_key, request_json,
             parent_run_id, child_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                identifier,
                feedback_id,
                request.idempotency_key,
                _dump(request.model_dump(exclude_none=True)),
                parent,
                child.id,
                _now(),
            ),
        )
        connection.execute(
            "UPDATE agent_run_feedback SET revision = revision + 1, updated_at = ? WHERE id = ?",
            (_now(), feedback_id),
        )
        self._event(
            connection,
            parent,
            "feedback_rerun",
            {
                "feedback_id": feedback_id,
                "child_run_id": child.id,
                "recheck_id": identifier,
            },
        )
        self._event(
            connection,
            child.id,
            "review_origin",
            {
                "parent_run_id": parent,
                "feedback_id": feedback_id,
                "recheck_id": identifier,
            },
        )

    def recheck(self, run_id: str, link_id: str, request: FeedbackRecheck) -> dict:
        with self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            link = connection.execute(
                "SELECT * FROM agent_run_rechecks WHERE id = ? AND parent_run_id = ?",
                (link_id, run_id),
            ).fetchone()
            if not link:
                raise KeyError("Unknown recheck for this run.")
            if link["recheck_json"]:
                self._same(link["recheck_json"], request.model_dump(), "recheck")
                return {"id": link_id, "recheck": json.loads(link["recheck_json"])}
            child = self.repository._require_run_tx(connection, link["child_run_id"])
            if child["status"] not in REVIEWABLE:
                raise AgentRunConflictError("Wait for the child run to finish before rechecking.")
            if request.decision == "resolved" and child["status"] != "completed":
                raise AgentRunConflictError("A failed or cancelled run cannot resolve feedback.")
            now = _now()
            output = connection.execute(
                "SELECT id, output_sha256 FROM agent_run_outputs WHERE run_id = ?",
                (child["id"],),
            ).fetchone()
            anchor = {
                "run_id": child["id"],
                "run_revision": child["revision"],
                "status": child["status"],
                "snapshot_id": child["context_snapshot_id"],
                "snapshot_sha256": child["context_sha256"],
                "output_id": output["id"] if output else None,
                "output_sha256": output["output_sha256"] if output else None,
            }
            connection.execute(
                """UPDATE agent_run_rechecks SET recheck_json = ?, recheck_anchor_json = ?,
                checked_at = ? WHERE id = ?""",
                (_dump(request.model_dump()), _dump(anchor), now, link_id),
            )
            connection.execute(
                """UPDATE agent_run_feedback SET status = ?, revision = revision + 1,
                updated_at = ? WHERE id = ?""",
                (
                    "resolved" if request.decision == "resolved" else "accepted",
                    now,
                    link["feedback_id"],
                ),
            )
            self._event(
                connection,
                run_id,
                "feedback_rechecked",
                {
                    "recheck_id": link_id,
                    "decision": request.decision,
                },
            )
            return {"id": link_id, "recheck": request.model_dump()}

    def export_candidate(self, run_id: str, feedback_id: str, target_dev_version: str) -> dict:
        with self.repository._connect() as connection:
            connection.execute("BEGIN")
            row = self._get_tx(connection, feedback_id, run_id)
            if row["status"] not in {"accepted", "resolved"}:
                raise AgentRunConflictError("Only accepted issues can enter dev candidates.")
            return {
                "version": "reviewed-dev-candidate-v1",
                "split": "dev",
                "target_dev_version": target_dev_version,
                "feedback": self._feedback(row),
                "automatic_import": False,
                "gold_label": None,
                "requires_dataset_review": True,
            }

    def _get_tx(self, connection, feedback_id, run_id=None):
        row = connection.execute(
            "SELECT * FROM agent_run_feedback WHERE id = ?", (feedback_id,)
        ).fetchone()
        if not row or (run_id is not None and row["run_id"] != run_id):
            raise KeyError("Unknown feedback for this run.")
        return row

    def _event(self, connection, run_id, event_type, summary):
        row = self.repository._require_run_tx(connection, run_id)
        self.repository._append_event_tx(
            connection,
            run_id=run_id,
            event_type=event_type,
            node_name=None,
            status=row["status"],
            output_summary=summary,
        )

    @staticmethod
    def _same(stored, requested, name):
        if json.loads(stored) != requested:
            raise AgentRunConflictError(f"Conflicting duplicate {name} operation.")

    @staticmethod
    def _feedback(row):
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "status": row["status"],
            "revision": row["revision"],
            "request": json.loads(row["request_json"]),
            "anchor": json.loads(row["anchor_json"]),
            "decision": json.loads(row["decision_json"]) if row["decision_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _link(row):
        output = json.loads(row["child_output_json"]) if row["child_output_json"] else {}
        return {
            "id": row["id"],
            "feedback_id": row["feedback_id"],
            "parent_run_id": row["parent_run_id"],
            "child_run_id": row["child_run_id"],
            "child_status": row["child_status"],
            "child_snapshot_id": row["child_snapshot_id"],
            "child_output_id": row["child_output_id"],
            "child_artifact_id": output.get("artifact_id"),
            "request": json.loads(row["request_json"]),
            "recheck": json.loads(row["recheck_json"]) if row["recheck_json"] else None,
            "recheck_anchor": (
                json.loads(row["recheck_anchor_json"]) if row["recheck_anchor_json"] else None
            ),
            "created_at": row["created_at"],
            "checked_at": row["checked_at"],
        }
