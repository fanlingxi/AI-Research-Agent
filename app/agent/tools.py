"""Snapshot-bounded tool definitions for Agent Runtime workflows."""

from __future__ import annotations

from typing import Any

from app.agent.registry import RegisteredTool, ToolRegistry
from app.context.models import ContextPackage
from app.context.reading import research_payload


def build_foundation_tool_registry() -> ToolRegistry:
    """Register the one deterministic, non-content-bearing foundation tool."""

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            name="context.snapshot_metadata",
            permission="context_read",
            handler=_context_snapshot_metadata,
        )
    )
    return registry


def _context_snapshot_metadata(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return only identifiers already present in graph state.

    Phase 3A deliberately has no Knowledge, file, network, Qdrant, Neo4j, or
    Obsidian tools. Phase 3B will add Snapshot-bounded read tools.
    """

    return {
        "context_snapshot_id": str(arguments["context_snapshot_id"]),
        "context_sha256": str(arguments["context_sha256"]),
    }


def build_research_tool_registry(package: ContextPackage) -> ToolRegistry:
    """Expose only the immutable ContextSnapshot supplied to this AgentRun.

    No handler receives a repository, a filesystem path, or a projection
    client. Consequently a plan cannot expand its scope through SQLite,
    Qdrant, Neo4j, Obsidian, network, or current project state.
    """

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            name="context.research_input",
            permission="context_read",
            handler=lambda arguments: _research_input(package, arguments),
        )
    )
    registry.register(
        RegisteredTool(
            name="context.task_constraints",
            permission="context_read",
            handler=lambda arguments: _task_constraints(package, arguments),
        )
    )
    registry.register(
        RegisteredTool(
            name="context.knowledge_bundles",
            permission="context_read",
            handler=lambda arguments: _knowledge_bundles(package, arguments),
        )
    )
    return registry


def snapshot_tool_audit_summary(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return an audit-safe summary, never document text or evidence quotes."""

    if name == "context.research_input":
        knowledge = result.get("knowledge", {})
        bundles = knowledge.get("claim_bundles", [])
        return {
            "context_snapshot_id": result.get("context_snapshot_id"),
            "claim_bundle_ids": [item.get("claim", {}).get("claim_id") for item in bundles],
            "evidence_ids": [
                evidence.get("evidence_id")
                for item in bundles
                for evidence in item.get("evidence", [])
            ],
            "claim_bundle_count": len(bundles),
        }
    if name == "context.task_constraints":
        return {
            "context_snapshot_id": result.get("context_snapshot_id"),
            "task_id": result.get("task", {}).get("task_id"),
            "collection_scopes": result.get("constraints", {}).get("collection_scopes", []),
        }
    if name == "context.knowledge_bundles":
        bundles = result.get("claim_bundles", [])
        return {
            "context_snapshot_id": result.get("context_snapshot_id"),
            "claim_bundle_ids": [item.get("claim", {}).get("claim_id") for item in bundles],
            "claim_bundle_count": len(bundles),
        }
    raise ValueError(f"Unsupported snapshot tool: {name}")


def _research_input(package: ContextPackage, arguments: dict[str, Any]) -> dict[str, Any]:
    _require_snapshot_identity(package, arguments)
    return research_input_payload(package)


def research_input_payload(package: ContextPackage) -> dict[str, Any]:
    """Build the bounded snapshot payload for ephemeral Runtime use.

    Callers must obtain ``package`` through the AgentRun's hash-validated
    ContextSnapshot boundary.  The returned content is intentionally local to
    a graph node and must never be placed in checkpoint state.
    """

    return research_payload(package)


def _task_constraints(package: ContextPackage, arguments: dict[str, Any]) -> dict[str, Any]:
    _require_snapshot_identity(package, arguments)
    return {
        "context_snapshot_id": package.snapshot_id,
        "task": package.task.model_dump(mode="json"),
        "constraints": package.constraints.model_dump(mode="json"),
    }


def _knowledge_bundles(package: ContextPackage, arguments: dict[str, Any]) -> dict[str, Any]:
    _require_snapshot_identity(package, arguments)
    return {
        "context_snapshot_id": package.snapshot_id,
        "claim_bundles": [item.model_dump(mode="json") for item in package.knowledge.claim_bundles],
    }


def _require_snapshot_identity(package: ContextPackage, arguments: dict[str, Any]) -> None:
    if (
        str(arguments.get("context_snapshot_id") or "") != package.snapshot_id
        or str(arguments.get("context_sha256") or "") != package.package_sha256
    ):
        raise ValueError(
            "Snapshot-only tools require the AgentRun's exact ContextSnapshot identity."
        )
