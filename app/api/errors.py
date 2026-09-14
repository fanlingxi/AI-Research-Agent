from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from app.agent.errors import (
    AgentRunConflictError,
    AgentRunNotRecoverableError,
    AgentRunTerminalError,
    ResearchLLMRequiredError,
)
from app.context.repository import SnapshotIntegrityError
from app.context.service import ContextBudgetTooSmallError, ContextConflictError
from app.domain_plugins.errors import DomainPluginConflictError
from app.workspace.service import WorkspaceProjectionConflictError


def memory_call(callback: Callable[[], Any]):
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def agent_call(callback: Callable[[], Any]):
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        AgentRunConflictError,
        AgentRunNotRecoverableError,
        AgentRunTerminalError,
        ResearchLLMRequiredError,
        DomainPluginConflictError,
        ContextConflictError,
        SnapshotIntegrityError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def domain_plugin_call(callback: Callable[[], Any]):
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DomainPluginConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def game_knowledge_call(callback: Callable[[], Any]):
    return domain_plugin_call(callback)


def workspace_call(callback: Callable[[], Any]):
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ContextConflictError, WorkspaceProjectionConflictError, SnapshotIntegrityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ContextBudgetTooSmallError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
