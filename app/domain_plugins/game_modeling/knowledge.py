"""Governed Formula and Patch publication for the Game Modeling plugin.

This adapter deliberately reuses the frozen Knowledge Core lifecycle:

``CandidateEntity → review decision → PublishedEntity → Core Entity/Claim``.

It adds no Game-specific table, projection, or alternate ownership model.  The
plugin contributes only typed validation and a reviewed Core representation;
the existing repository remains the transaction and Collection-membership
authority.
"""

from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

from app.domain_plugins.errors import DomainPluginConflictError
from app.domain_plugins.game_modeling.models import (
    GameFormulaCandidateCreateRequest,
    GameFormulaDefinition,
    GameKnowledgePublication,
    GamePatchCandidateCreateRequest,
    GamePatchDefinition,
)
from app.domain_plugins.game_modeling.plugin import GAME_MODELING_PLUGIN_KEY
from app.knowledge.core_models import CoreEntityClaimOverride
from app.knowledge.repository import DecisionAlreadyApplied, KnowledgeRepository
from app.knowledge.schemas import CandidateEntity

_GameEntityType = Literal["Formula", "Patch"]


class GameKnowledgeAuthoringService:
    """Create and review only the Game facts consumed by this plugin.

    All durable writes are delegated to :class:`KnowledgeRepository`; in
    particular, Collection membership, legacy/Core identity mapping, evidence
    validation, review event, and projection outbox are committed as one
    existing publication transaction.
    """

    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    def create_formula_candidate(
        self, request: GameFormulaCandidateCreateRequest
    ) -> CandidateEntity:
        self._require_patch_reference(request.formula)
        self._require_evidence_version(request.evidence, request.formula.patch_version)
        return self._create_candidate(
            entity_type="Formula",
            ingestion_id=request.ingestion_id,
            name=request.name,
            summary=request.summary,
            confidence=request.confidence,
            evidence=request.evidence,
            definition=request.formula.model_dump(mode="json"),
        )

    def create_patch_candidate(
        self, request: GamePatchCandidateCreateRequest
    ) -> CandidateEntity:
        self._require_evidence_version(request.evidence, request.patch.patch_version)
        return self._create_candidate(
            entity_type="Patch",
            ingestion_id=request.ingestion_id,
            name=request.name,
            summary=request.summary,
            confidence=request.confidence,
            evidence=request.evidence,
            definition=request.patch.model_dump(mode="json"),
        )

    def approve_candidate(self, candidate_id: str) -> GameKnowledgePublication:
        candidate, entity_type, definition = self._game_candidate(candidate_id, draft_only=True)
        patch_version = self._patch_version(entity_type, definition)
        if entity_type == "Formula":
            self._require_patch_reference(GameFormulaDefinition.model_validate(definition))

        claim = self._claim_override(candidate, entity_type, definition, patch_version)
        try:
            entity = self.repository.publish_entity(
                candidate.id,
                domain_plugin_key=GAME_MODELING_PLUGIN_KEY,
                core_domain="game",
                core_claim_override=claim,
            )
        except DecisionAlreadyApplied as exc:
            raise DomainPluginConflictError(
                f"Game Knowledge candidate {candidate_id} already has a terminal decision."
            ) from exc
        core_claim = self.repository.core_repository.get_claim_by_legacy_id(
            f"published_entity_definition:{entity.id}"
        )
        return GameKnowledgePublication(
            candidate_id=candidate.id,
            entity_id=core_claim.entity_id or "",
            claim_id=core_claim.id,
            entity_type=entity_type,
            patch_version=patch_version,
        )

    def reject_candidate(
        self, candidate_id: str, *, review_note: str | None = None
    ) -> CandidateEntity:
        candidate, _, _ = self._game_candidate(candidate_id, draft_only=True)
        try:
            rejected = self.repository.reject_candidate(
                candidate.id,
                review_note=review_note,
                domain_plugin_key=GAME_MODELING_PLUGIN_KEY,
            )
        except DecisionAlreadyApplied as exc:
            raise DomainPluginConflictError(
                f"Game Knowledge candidate {candidate_id} already has a terminal decision."
            ) from exc
        return CandidateEntity.model_validate(rejected["candidate"])

    def _create_candidate(
        self,
        *,
        entity_type: _GameEntityType,
        ingestion_id: str,
        name: str,
        summary: str,
        confidence: float,
        evidence,
        definition: dict[str, object],
    ) -> CandidateEntity:
        ingestion = self.repository.get_ingestion(ingestion_id)
        self.repository.require_evidence_for_ingestion(ingestion.id, evidence)
        candidate = CandidateEntity(
            id=f"game-model-candidate-{uuid4().hex}",
            ingestion_id=ingestion.id,
            topic_slug=ingestion.collection_slug,
            name=name,
            type=entity_type,
            summary=summary,
            confidence=confidence,
            evidence=evidence,
            metadata={
                "domain_plugin": GAME_MODELING_PLUGIN_KEY,
                # ``domain_plugin`` already exists as historical extraction
                # metadata on Research candidates.  This explicit route is
                # the permission marker consumed by KnowledgeRepository.
                "domain_review_route": GAME_MODELING_PLUGIN_KEY,
                "game_model": {"kind": entity_type.casefold(), "definition": definition},
            },
        )
        self.repository.add_candidate_entity(candidate)
        return candidate

    def _game_candidate(
        self, candidate_id: str, *, draft_only: bool
    ) -> tuple[CandidateEntity, _GameEntityType, dict[str, object]]:
        stored = self.repository.get_candidate(candidate_id)
        if stored["kind"] != "entity":
            raise ValueError("Game Knowledge review only supports entity candidates.")
        candidate = CandidateEntity.model_validate(stored["candidate"])
        if candidate.metadata.get("domain_review_route") != GAME_MODELING_PLUGIN_KEY:
            raise ValueError("Candidate is not owned by the Game Modeling plugin.")
        if candidate.type not in {"Formula", "Patch"}:
            raise ValueError("Game Modeling only governs Formula and Patch candidates.")
        if draft_only and candidate.status != "draft":
            raise DomainPluginConflictError(
                f"Game Knowledge candidate {candidate_id} already has a terminal decision."
            )
        game_model = candidate.metadata.get("game_model")
        if not isinstance(game_model, dict):
            raise ValueError("Game Knowledge candidate is missing its typed definition.")
        if game_model.get("kind") != candidate.type.casefold():
            raise ValueError("Game Knowledge candidate kind does not match its entity type.")
        raw_definition = game_model.get("definition")
        if not isinstance(raw_definition, dict):
            raise ValueError("Game Knowledge candidate definition is invalid.")
        definition: dict[str, object] = dict(raw_definition)
        if candidate.type == "Formula":
            GameFormulaDefinition.model_validate(definition)
        else:
            GamePatchDefinition.model_validate(definition)
        return candidate, candidate.type, definition

    def _require_patch_reference(self, formula: GameFormulaDefinition) -> None:
        """Require an already-reviewed Patch Claim at the exact version."""

        try:
            patch_claim = self.repository.core_repository.get_claim(formula.patch_claim_id)
            if patch_claim.entity_id is None:
                raise ValueError("Formula patch_claim_id must refer to a Patch Entity Claim.")
            patch_entity = self.repository.core_repository.get_entity(patch_claim.entity_id)
            if (
                patch_claim.status != "published"
                or patch_entity.status != "published"
                or patch_entity.domain != "game"
                or patch_entity.entity_type != "Patch"
            ):
                raise ValueError("Formula patch_claim_id must refer to a published game Patch.")
            patch = GamePatchDefinition.model_validate(json.loads(patch_claim.object_value))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError(
                "Formula patch_claim_id does not resolve to a reviewed Patch."
            ) from exc
        if patch.patch_version != formula.patch_version:
            raise ValueError("Formula patch_version must exactly match its reviewed Patch.")

    def _require_evidence_version(self, evidence, patch_version: str) -> None:
        source_version = self.repository.core_repository.source_version_for_evidence(evidence)
        if source_version != patch_version:
            raise ValueError(
                "Game Knowledge Evidence Source version must exactly match the patch_version."
            )

    @staticmethod
    def _patch_version(entity_type: _GameEntityType, definition: dict[str, object]) -> str:
        if entity_type == "Formula":
            return GameFormulaDefinition.model_validate(definition).patch_version
        return GamePatchDefinition.model_validate(definition).patch_version

    @staticmethod
    def _claim_override(
        candidate: CandidateEntity,
        entity_type: _GameEntityType,
        definition: dict[str, object],
        patch_version: str,
    ) -> CoreEntityClaimOverride:
        claim_type = "game_formula" if entity_type == "Formula" else "game_patch_version"
        predicate = "defines_formula" if entity_type == "Formula" else "defines_patch"
        return CoreEntityClaimOverride(
            subject=candidate.name,
            predicate=predicate,
            object_value=json.dumps(
                definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            claim_type=claim_type,
            statement=candidate.summary,
            confidence=candidate.confidence,
            properties={
                "domain_plugin": GAME_MODELING_PLUGIN_KEY,
                "source_version": patch_version,
            },
        )
