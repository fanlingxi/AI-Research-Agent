"""Deterministic benchmarks and explicitly enabled live evaluation tools.

The Evaluation Plane deliberately owns no production facts, AgentRuns, plugin
registrations, or application routes.  It creates isolated fixtures and drives
the existing public service paths from outside the business Runtime. Workbench
archive views live in app.experiments and do not depend on this package.
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
