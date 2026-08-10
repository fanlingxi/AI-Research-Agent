"""Snapshot-only, deterministic tools for the Game Modeling plugin."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from typing import Any

from app.agent.registry import RegisteredTool, ToolRegistry
from app.context.models import ContextPackage, KnowledgeClaimBundle
from app.domain_plugins.game_modeling.models import (
    GameFormulaDefinition,
    GameModelProvenance,
    GameModelResult,
    GameModelValidation,
    GamePatchDefinition,
    ResolvedGameModel,
    game_model_metadata,
)

_MAX_ABSOLUTE_RESULT = 1_000_000_000_000.0


def build_game_model_tool_registry(package: ContextPackage) -> ToolRegistry:
    """Expose one pure calculation over the exact immutable ContextSnapshot."""

    resolved = resolve_game_model(package)
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            name="game.run_formula",
            permission="deterministic_compute",
            handler=lambda arguments: _run_formula(resolved, package, arguments),
        )
    )
    return registry


def resolve_game_model(package: ContextPackage) -> ResolvedGameModel:
    """Resolve Task-selected Formula and Patch bundles without widening scope."""

    request = game_model_metadata(package.task.metadata)
    bundles = {item.claim.claim_id: item for item in package.knowledge.claim_bundles}
    formula_bundle = _required_bundle(bundles, request.formula_claim_id, "Formula")
    patch_bundle = _required_bundle(bundles, request.patch_claim_id, "Patch")
    formula = _formula_from_bundle(formula_bundle)
    patch = _patch_from_bundle(patch_bundle)
    if patch.patch_version != request.patch_version:
        raise ValueError("Patch Claim patch_version does not match the task request")
    formula_provenance = _provenance(formula_bundle)
    patch_provenance = _provenance(patch_bundle)
    _require_source_version(formula_provenance, request.patch_version, "Formula")
    _require_source_version(patch_provenance, request.patch_version, "Patch")
    return ResolvedGameModel(
        request=request,
        formula=formula,
        formula_provenance=formula_provenance,
        patch_provenance=patch_provenance,
        formula_sha256=_formula_sha256(formula),
    )


def execute_game_formula(resolved: ResolvedGameModel) -> GameModelResult:
    """Evaluate the constrained arithmetic grammar without ``eval`` or I/O."""

    parameters = dict(resolved.request.parameters)
    expected = set(resolved.formula.variables)
    received = set(parameters)
    if received != expected:
        missing = sorted(expected - received)
        unexpected = sorted(received - expected)
        details = []
        if missing:
            details.append(f"missing parameters: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected parameters: {', '.join(unexpected)}")
        raise ValueError(
            "Formula parameters do not match its published definition "
            f"({'; '.join(details)})"
        )
    expression = ast.parse(resolved.formula.expression, mode="eval")
    names: set[str] = set()
    value = _evaluate_expression(expression.body, parameters, names)
    if names != expected:
        raise ValueError("Formula expression names do not match its declared variables")
    _require_safe_number(value)
    return GameModelResult(
        formula_claim_id=resolved.request.formula_claim_id,
        patch_claim_id=resolved.request.patch_claim_id,
        patch_version=resolved.request.patch_version,
        formula_sha256=resolved.formula_sha256,
        unit=resolved.formula.unit,
        parameters=parameters,
        value=value,
        formula_provenance=resolved.formula_provenance,
        patch_provenance=resolved.patch_provenance,
    )


def validate_game_model_result(
    result: GameModelResult, resolved: ResolvedGameModel
) -> GameModelValidation:
    """Verify result identity and complete Formula/Patch evidence provenance."""

    errors: list[str] = []
    if result.formula_claim_id != resolved.request.formula_claim_id:
        errors.append("result Formula Claim does not match the task request")
    if result.patch_claim_id != resolved.request.patch_claim_id:
        errors.append("result Patch Claim does not match the task request")
    if result.patch_version != resolved.request.patch_version:
        errors.append("result patch_version does not match the task request")
    if result.formula_sha256 != resolved.formula_sha256:
        errors.append("result Formula hash does not match the published definition")
    if result.parameters != resolved.request.parameters:
        errors.append("result parameters do not match the task request")
    if not result.formula_provenance.evidence_ids or not result.patch_provenance.evidence_ids:
        errors.append("Formula and Patch must retain locatable Evidence")
    try:
        _require_safe_number(result.value)
    except ValueError as exc:
        errors.append(str(exc))
    cited = sorted(
        set(result.formula_provenance.evidence_ids) | set(result.patch_provenance.evidence_ids)
    )
    return GameModelValidation(
        passed=not errors,
        errors=errors,
        formula_claim_id=result.formula_claim_id,
        patch_claim_id=result.patch_claim_id,
        cited_evidence_ids=cited,
        patch_version=result.patch_version,
    )


def game_tool_audit_summary(result: GameModelResult) -> dict[str, Any]:
    """Persist only identifier-sized audit metadata, never formula bodies."""

    return {
        "formula_claim_id": result.formula_claim_id,
        "patch_claim_id": result.patch_claim_id,
        "patch_version": result.patch_version,
        "formula_sha256": result.formula_sha256,
        "unit": result.unit,
        "value": result.value,
        "formula_evidence_ids": result.formula_provenance.evidence_ids,
        "patch_evidence_ids": result.patch_provenance.evidence_ids,
    }


def game_model_markdown(result: GameModelResult) -> str:
    """Render a concise, citation-linked Artifact without exposing formula text."""

    citations = " ".join(
        f"[cite:{evidence_id}]"
        for evidence_id in sorted(
            set(result.formula_provenance.evidence_ids)
            | set(result.patch_provenance.evidence_ids)
        )
    )
    return "\n".join(
        [
            "# Game Model Result",
            "",
            f"- Patch version: `{result.patch_version}`",
            f"- Result: `{result.value:g} {result.unit}`",
            f"- Formula Claim: `{result.formula_claim_id}`",
            f"- Patch Claim: `{result.patch_claim_id}`",
            "",
            f"Evidence: {citations}",
        ]
    )


def _run_formula(
    resolved: ResolvedGameModel, package: ContextPackage, arguments: dict[str, Any]
) -> dict[str, Any]:
    _require_snapshot_identity(package, arguments)
    return execute_game_formula(resolved).model_dump(mode="json")


def _required_bundle(
    bundles: dict[str, KnowledgeClaimBundle], claim_id: str, entity_type: str
) -> KnowledgeClaimBundle:
    bundle = bundles.get(claim_id)
    if bundle is None:
        raise ValueError(f"{entity_type} Claim is absent from the immutable ContextSnapshot")
    if bundle.claim.status != "published" or bundle.entity is None:
        raise ValueError(f"{entity_type} Claim is not a published entity-backed Claim")
    if (
        bundle.entity.domain.casefold() != "game"
        or bundle.entity.entity_type.casefold() != entity_type.casefold()
    ):
        raise ValueError(f"Claim {claim_id} is not a published game {entity_type}")
    return bundle


def _formula_from_bundle(bundle: KnowledgeClaimBundle) -> GameFormulaDefinition:
    return GameFormulaDefinition.model_validate(
        _json_object(bundle.claim.object_value, "Formula Claim")
    )


def _patch_from_bundle(bundle: KnowledgeClaimBundle) -> GamePatchDefinition:
    return GamePatchDefinition.model_validate(
        _json_object(bundle.claim.object_value, "Patch Claim")
    )


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} object_value must contain JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} object_value must contain a JSON object")
    return parsed


def _provenance(bundle: KnowledgeClaimBundle) -> GameModelProvenance:
    sources = {source.source_id: source for source in bundle.sources}
    source_ids = sorted({evidence.source_id for evidence in bundle.evidence})
    if any(source_id not in sources for source_id in source_ids):
        raise ValueError("Claim Evidence has no locatable Source in the ContextSnapshot")
    versions = sorted(
        {
            sources[source_id].version
            for source_id in source_ids
            if sources[source_id].version
        }
    )
    if not versions:
        raise ValueError("Game Formula and Patch Sources must retain a version")
    entity = bundle.entity
    if entity is None:
        raise ValueError("Game Claim must be entity-backed")
    return GameModelProvenance(
        claim_id=bundle.claim.claim_id,
        entity_id=entity.entity_id,
        evidence_ids=sorted({item.evidence_id for item in bundle.evidence}),
        source_ids=source_ids,
        chunk_ids=sorted({item.chunk_id for item in bundle.evidence}),
        source_versions=versions,
    )


def _require_source_version(
    provenance: GameModelProvenance, version: str, label: str
) -> None:
    # A Claim Bundle may carry several Evidence records.  Accepting a bundle
    # merely because *one* Source is on the requested patch would make a mixed
    # patch citation look valid.  Formula and Patch facts are versioned as a
    # whole: every retained Source must describe the exact requested version.
    if provenance.source_versions != [version]:
        raise ValueError(
            f"{label} Source versions must exactly match the requested patch_version"
        )


def _formula_sha256(formula: GameFormulaDefinition) -> str:
    encoded = json.dumps(
        formula.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evaluate_expression(node: ast.AST, parameters: dict[str, float], names: set[str]) -> float:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        value = float(node.value)
    elif isinstance(node, ast.Name):
        if node.id not in parameters:
            raise ValueError(f"Formula references an undeclared parameter: {node.id}")
        names.add(node.id)
        value = float(parameters[node.id])
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _evaluate_expression(node.operand, parameters, names)
        value = operand if isinstance(node.op, ast.UAdd) else -operand
    elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = _evaluate_expression(node.left, parameters, names)
        right = _evaluate_expression(node.right, parameters, names)
        if isinstance(node.op, ast.Add):
            value = left + right
        elif isinstance(node.op, ast.Sub):
            value = left - right
        elif isinstance(node.op, ast.Mult):
            value = left * right
        else:
            if right == 0:
                raise ValueError("Formula division by zero")
            value = left / right
    else:
        raise ValueError("Formula contains an unsupported operation")
    _require_safe_number(value)
    return value


def _require_safe_number(value: float) -> None:
    if not math.isfinite(value) or abs(value) > _MAX_ABSOLUTE_RESULT:
        raise ValueError("Formula result is non-finite or outside the safe numeric range")


def _require_snapshot_identity(package: ContextPackage, arguments: dict[str, Any]) -> None:
    if (
        str(arguments.get("context_snapshot_id") or "") != package.snapshot_id
        or str(arguments.get("context_sha256") or "") != package.package_sha256
    ):
        raise ValueError("Game tools require the AgentRun's exact ContextSnapshot identity")
