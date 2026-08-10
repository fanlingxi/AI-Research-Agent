"""Offline, deterministic benchmarking support for the frozen platform.

The Evaluation Plane deliberately owns no production facts, AgentRuns, plugin
registrations, or application routes.  It creates isolated fixtures and drives
the existing public service paths from outside the business Runtime.
"""

from app.benchmarking.contracts import (
    BaselineComparison,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationRun,
)

__all__ = [
    "BaselineComparison",
    "EvaluationCase",
    "EvaluationCaseResult",
    "EvaluationRun",
]
