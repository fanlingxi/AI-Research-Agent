from __future__ import annotations

from pydantic import BaseModel, Field


class EvaluationMetric(BaseModel):
    """A single quality metric."""

    name: str
    score: float
    reason: str


class EvaluationResult(BaseModel):
    """Overall evaluation for a research run."""

    overall_score: float
    passed: bool
    summary: str
    metrics: list[EvaluationMetric] = Field(default_factory=list)


class CritiqueResult(BaseModel):
    """Critic Agent feedback."""

    quality_score: float
    needs_revision: bool
    strengths: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class ReflectionResult(BaseModel):
    """Reflection output after applying critic feedback."""

    revised_report: str
    notes: list[str] = Field(default_factory=list)
    applied_suggestions: list[str] = Field(default_factory=list)
