"""Typed failures for bounded Agent Runtime operations."""


class AgentRunError(ValueError):
    """Base class for safe Agent Runtime failures."""


class AgentRunConflictError(AgentRunError):
    """The task already owns an active run or input ownership conflicts."""


class AgentRunTerminalError(AgentRunError):
    """A terminal AgentRun cannot execute, cancel, or resume again."""


class AgentRunNotRecoverableError(AgentRunError):
    """The current status has no safe recovery path."""


class AgentRunCancelledError(AgentRunError):
    """The worker observed a cancellation before a graph node could run."""


class AgentToolPermissionError(AgentRunError):
    """A tool request exceeds the registered Runtime permission."""


class ResearchLLMRequiredError(AgentRunError):
    """A formal Research workflow cannot run against the mock sentinel."""


class ResearchValidationError(AgentRunError):
    """A draft failed its bounded evidence and citation validation."""
