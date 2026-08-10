"""Serializable Game Modeling input, provenance, and output contracts."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.knowledge.schemas import EvidenceSpan


class GameModelTaskRequest(BaseModel):
    """Task-owned selection of already formalized game inputs.

    Parameter values are explicit modeling inputs, not new Knowledge facts.
    Formula and Patch identities must resolve to published Snapshot bundles.
    """

    formula_claim_id: str = Field(min_length=1, max_length=200)
    patch_claim_id: str = Field(min_length=1, max_length=200)
    patch_version: str = Field(min_length=1, max_length=160)
    parameters: dict[str, float] = Field(min_length=1, max_length=32)

    @field_validator("parameters")
    @classmethod
    def parameters_are_finite(cls, value: dict[str, float]) -> dict[str, float]:
        invalid = [name for name, number in value.items() if not math.isfinite(number)]
        if invalid:
            raise ValueError(f"game model parameters must be finite: {', '.join(sorted(invalid))}")
        return value

    @field_validator("parameters")
    @classmethod
    def parameter_names_are_identifiers(cls, value: dict[str, float]) -> dict[str, float]:
        invalid = [name for name in value if not name.isidentifier()]
        if invalid:
            raise ValueError(
                f"game model parameter names are invalid: {', '.join(sorted(invalid))}"
            )
        return value


class GameFormulaDefinition(BaseModel):
    """A safe arithmetic formula encoded by a published Formula Claim."""

    expression: str = Field(min_length=1, max_length=500)
    patch_claim_id: str = Field(min_length=1, max_length=200)
    patch_version: str = Field(min_length=1, max_length=160)
    unit: str = Field(min_length=1, max_length=80)
    variables: list[str] = Field(min_length=1, max_length=32)

    @field_validator("variables")
    @classmethod
    def variables_are_unique_identifiers(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("formula variables must be unique")
        invalid = [name for name in value if not name.isidentifier()]
        if invalid:
            raise ValueError(f"formula variable names are invalid: {', '.join(sorted(invalid))}")
        return value


class GamePatchDefinition(BaseModel):
    """Version marker encoded by a published Patch Claim."""

    patch_version: str = Field(min_length=1, max_length=160)


class GameKnowledgeCandidateBase(BaseModel):
    """Reviewer-authored input for a formal, evidence-grounded Game fact.

    The candidate is stored through the existing Knowledge candidate table and
    must be approved through the Game plugin service before it reaches Core.
    It is intentionally not a second Game-specific knowledge store.
    """

    ingestion_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=2, max_length=160)
    summary: str = Field(min_length=12, max_length=900)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: EvidenceSpan


class GameFormulaCandidateCreateRequest(GameKnowledgeCandidateBase):
    formula: GameFormulaDefinition


class GamePatchCandidateCreateRequest(GameKnowledgeCandidateBase):
    patch: GamePatchDefinition


class GameKnowledgePublication(BaseModel):
    """Stable identifiers returned after a reviewed Game candidate publishes."""

    candidate_id: str
    entity_id: str
    claim_id: str
    entity_type: Literal["Formula", "Patch"]
    status: Literal["published"] = "published"
    patch_version: str = Field(min_length=1, max_length=160)


class GameModelProvenance(BaseModel):
    claim_id: str
    entity_id: str
    evidence_ids: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    chunk_ids: list[str] = Field(min_length=1)
    source_versions: list[str] = Field(min_length=1)


class GameModelResult(BaseModel):
    formula_claim_id: str
    patch_claim_id: str
    patch_version: str
    formula_sha256: str
    unit: str
    parameters: dict[str, float]
    value: float
    formula_provenance: GameModelProvenance
    patch_provenance: GameModelProvenance

    @field_validator("value")
    @classmethod
    def value_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("formula result must be finite")
        return value


class GameModelValidation(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    formula_claim_id: str | None = None
    patch_claim_id: str | None = None
    cited_evidence_ids: list[str] = Field(default_factory=list)
    patch_version: str | None = None


class ResolvedGameModel(BaseModel):
    """Ephemeral validated input; never stored in LangGraph checkpoint state."""

    request: GameModelTaskRequest
    formula: GameFormulaDefinition
    formula_provenance: GameModelProvenance
    patch_provenance: GameModelProvenance
    formula_sha256: str

    @model_validator(mode="after")
    def formula_and_request_versions_agree(self):
        if self.formula.patch_claim_id != self.request.patch_claim_id:
            raise ValueError("Formula Claim patch_claim_id does not match the task request")
        if self.formula.patch_version != self.request.patch_version:
            raise ValueError("Formula Claim patch_version does not match the task request")
        return self


def game_model_metadata(value: dict[str, Any]) -> GameModelTaskRequest:
    """Parse only the bounded Game Modeling request from Task metadata."""

    raw = value.get("game_model")
    if not isinstance(raw, dict):
        raise ValueError("WorkspaceTask metadata.game_model is required for Game Modeling")
    return GameModelTaskRequest.model_validate(raw)
