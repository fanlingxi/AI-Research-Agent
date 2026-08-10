"""Versioned, serializable contracts for the offline Benchmarking Plane."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

EVALUATION_CONTRACT_VERSION = "phase6-evaluation-v1"

CaseTarget = Literal[
    "retrieval",
    "context",
    "research_runtime",
    "game_runtime",
    "plugin_boundary",
    "workspace_projection",
]
CasePriority = Literal["blocker", "core"]
CaseStatus = Literal["passed", "failed", "skipped", "error"]
FailureKind = Literal["fixture", "sut", "oracle", "isolation", "environment", "regression"]


class EvaluationExpectation(BaseModel):
    """Declarative facts an evaluator must prove for one curated case."""

    expected_status: str | None = None
    expected_error_contains: str | None = None
    expected_retrieved_paper_ids: list[str] = Field(default_factory=list)
    forbidden_retrieved_paper_ids: list[str] = Field(default_factory=list)
    expected_claim_aliases: list[str] = Field(default_factory=list)
    expected_knowledge_coverage: str | None = None
    expected_selected_bundle_count: int | None = Field(default=None, ge=0)
    expected_value: float | None = None
    expected_plugin_key: str | None = None
    expected_repair_count: int | None = Field(default=None, ge=0)
    expected_tool_call_count: int | None = Field(default=None, ge=0)
    expect_artifact: bool | None = None
    expect_memory_proposal: bool | None = None
    expect_no_business_outputs: bool = False
    required_validation_fields: list[str] = Field(default_factory=list)


class EvaluationCase(BaseModel):
    """One immutable, fixture-driven platform behavior to evaluate."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,80}$")
    suite: str = Field(min_length=2, max_length=80)
    version: str = Field(default="v1", min_length=1, max_length=32)
    priority: CasePriority = "core"
    target: CaseTarget
    fixture: str = Field(min_length=2, max_length=80)
    scenario: str = Field(min_length=2, max_length=100)
    expectation: EvaluationExpectation = Field(default_factory=EvaluationExpectation)
    tags: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def expected_failure_has_an_error_contract(self):
        if (
            self.expectation.expect_no_business_outputs
            and self.expectation.expected_status in {"exception", "failed"}
            and not self.expectation.expected_error_contains
        ):
            raise ValueError("expected failure cases must declare expected_error_contains")
        return self


class EvaluationManifest(BaseModel):
    """A sealed collection of cases that can be compared across runs."""

    contract_version: str = EVALUATION_CONTRACT_VERSION
    suite_version: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=2, max_length=1000)
    cases: list[EvaluationCase] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def case_ids_are_unique(self):
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation manifest contains duplicate case IDs")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class AssertionResult(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = Field(default="", max_length=2000)


class FailureRecord(BaseModel):
    kind: FailureKind
    message: str = Field(min_length=1, max_length=4000)
    detail: dict[str, Any] = Field(default_factory=dict)


class EvaluationCaseResult(BaseModel):
    case_id: str
    suite: str
    status: CaseStatus
    assertions: list[AssertionResult] = Field(default_factory=list)
    metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    semantic_digest: str | None = None
    duration_ms: float = Field(ge=0.0)
    failure: FailureRecord | None = None


class IsolationAttestation(BaseModel):
    real_database_unchanged: bool
    settings_contained: bool
    network_blocked: bool
    sandbox_removed: bool

    @property
    def passed(self) -> bool:
        return all(self.model_dump().values())


class EvaluationEnvironment(BaseModel):
    python_version: str
    platform: str
    machine: str
    schema_version: int
    llm_mode: Literal["scripted_fixture", "not_used"]
    operational_schema_baseline: int = 14


class EvaluationRun(BaseModel):
    """Machine-readable evidence from one complete offline experiment."""

    run_id: str
    contract_version: str = EVALUATION_CONTRACT_VERSION
    profile: Literal["deterministic-contract", "repeatability", "demo-evidence"]
    manifest_sha256: str
    suite_version: str
    environment: EvaluationEnvironment
    cases: list[EvaluationCaseResult]
    isolation: IsolationAttestation

    @property
    def summary(self) -> dict[str, int]:
        return {
            status: sum(case.status == status for case in self.cases)
            for status in ("passed", "failed", "skipped", "error")
        }


class EvaluationBaseline(BaseModel):
    """A manually approved reference run; never auto-created by a passing run."""

    baseline_id: str = Field(min_length=3, max_length=120)
    contract_version: str = EVALUATION_CONTRACT_VERSION
    manifest_sha256: str
    suite_version: str
    profile: Literal["deterministic-contract", "repeatability", "demo-evidence"]
    accepted_run: EvaluationRun
    approved_by: str = Field(min_length=2, max_length=160)
    approved_at: str = Field(min_length=10, max_length=80)


class BaselineComparison(BaseModel):
    comparable: bool
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    metric_deltas: dict[str, float] = Field(default_factory=dict)


def canonical_sha256(value: Any) -> str:
    """Hash JSON in one stable representation for fixtures, manifests, and results."""

    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
