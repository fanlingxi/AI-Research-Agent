"""Lazy, isolated LangGraph checkpoint construction."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver


class NamespacedSqliteSaver(SqliteSaver):
    """Bind one root graph to a stable checkpoint namespace.

    LangGraph root graphs normalize ``checkpoint_ns`` to an empty string before
    calling their saver.  This adapter makes Phase 3A and Phase 3B namespaces
    real SQLite partition keys while leaving ``thread_id == agent_run_id``.
    """

    def __init__(self, connection: sqlite3.Connection, *, namespace: str) -> None:
        super().__init__(connection)
        self.namespace = namespace

    def get_tuple(self, config: dict[str, Any]):
        return super().get_tuple(self._namespaced(config))

    def put(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        return super().put(self._namespaced(config), checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        super().put_writes(self._namespaced(config), writes, task_id, task_path)

    def list(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ):
        return super().list(
            self._namespaced(config) if config is not None else None,
            filter=filter,
            before=self._namespaced(before) if before is not None else None,
            limit=limit,
        )

    def _namespaced(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            **config,
            "configurable": {
                **dict(config.get("configurable") or {}),
                "checkpoint_ns": self.namespace,
            },
        }


class AgentCheckpointFactory:
    """Open a short-lived checkpoint saver only when a graph actually runs.

    The separate SQLite file is operational recovery state, not the authority
    for AgentRun status, trace, outputs, Artifact, or Memory.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def open(self, *, namespace: str | None = None) -> Iterator[SqliteSaver]:
        checkpoint_path = Path(self.path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
        try:
            if namespace is None:
                yield SqliteSaver(connection)
            else:
                yield NamespacedSqliteSaver(connection, namespace=namespace)
        finally:
            connection.close()
