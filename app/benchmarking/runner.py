"""Offline runner, isolation guard, and JSON persistence for Phase 6."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import socket
import sys
import tempfile
import time
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.benchmarking.contracts import (
    EVALUATION_CONTRACT_VERSION,
    AssertionResult,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationEnvironment,
    EvaluationManifest,
    EvaluationRun,
    FailureRecord,
    IsolationAttestation,
)
from app.benchmarking.evaluator import evaluate_case
from app.benchmarking.fixtures import EvaluationFixture, build_fixture
from app.benchmarking.sut import execute_case


def load_manifest(path: str | Path) -> EvaluationManifest:
    return EvaluationManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def run_manifest(
    manifest: EvaluationManifest,
    *,
    profile: str = "deterministic-contract",
    case_ids: set[str] | None = None,
) -> EvaluationRun:
    """Run selected cases in fresh sandboxes without reading application defaults."""

    if profile not in {"deterministic-contract", "repeatability", "demo-evidence"}:
        raise ValueError(f"Unknown Evaluation profile: {profile}")
    selected = [case for case in manifest.cases if case_ids is None or case.id in case_ids]
    if not selected:
        raise ValueError("No Evaluation cases selected")
    results: list[EvaluationCaseResult] = []
    attestations: list[IsolationAttestation] = []
    schema_version = 0
    llm_mode = "not_used"
    for case in selected:
        repeats = 3 if profile == "repeatability" else 1
        repeated = [_run_case(case) for _ in range(repeats)]
        first_result, first_attestation, first_schema, first_llm_mode = repeated[0]
        if repeats > 1:
            _attach_repeatability_assertion(first_result, [item[0] for item in repeated])
        results.append(first_result)
        attestations.extend(item[1] for item in repeated)
        schema_version = max(schema_version, *(item[2] for item in repeated))
        if first_llm_mode == "scripted_fixture":
            llm_mode = first_llm_mode
    isolation = IsolationAttestation(
        real_database_unchanged=all(item.real_database_unchanged for item in attestations),
        settings_contained=all(item.settings_contained for item in attestations),
        network_blocked=all(item.network_blocked for item in attestations),
        sandbox_removed=all(item.sandbox_removed for item in attestations),
    )
    return EvaluationRun(
        run_id=f"phase6-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:10]}",
        contract_version=EVALUATION_CONTRACT_VERSION,
        profile=profile,
        manifest_sha256=manifest.sha256,
        suite_version=manifest.suite_version,
        environment=EvaluationEnvironment(
            python_version=sys.version.split()[0],
            platform=platform.platform(),
            machine=platform.machine(),
            schema_version=schema_version,
            llm_mode=llm_mode,
        ),
        cases=results,
        isolation=isolation,
    )


def write_run(run: EvaluationRun, output_root: str | Path) -> Path:
    """Persist a result in a new directory; historical evaluation evidence is immutable."""

    root = Path(output_root)
    target = root / run.run_id
    if target.exists():
        raise FileExistsError(f"Evaluation result already exists: {target}")
    target.mkdir(parents=True)
    (target / "run.json").write_text(
        json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _run_case(
    case: EvaluationCase,
) -> tuple[EvaluationCaseResult, IsolationAttestation, int, str]:
    real_before = _database_fingerprint(_real_database_path())
    root = Path(tempfile.mkdtemp(prefix=f"ai-research-agent-phase6-{case.id}-"))
    fixture: EvaluationFixture | None = None
    network_guard = _NetworkDenyGuard()
    started = time.perf_counter()
    try:
        with network_guard:
            fixture = build_fixture(case.fixture, root)
            actual = execute_case(fixture, case)
        result = evaluate_case(
            case, actual, duration_ms=(time.perf_counter() - started) * 1000
        )
        schema_version = fixture.repository.schema_version()
        llm_mode = "scripted_fixture" if fixture.scripted_llm is not None else "not_used"
        settings_contained = _settings_contained(fixture, root)
    except Exception as exc:
        result = EvaluationCaseResult(
            case_id=case.id,
            suite=case.suite,
            status="error",
            assertions=[],
            metrics={},
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            failure=FailureRecord(kind="fixture", message=f"{type(exc).__name__}: {exc}"),
        )
        schema_version = fixture.repository.schema_version() if fixture is not None else 0
        llm_mode = "scripted_fixture" if fixture and fixture.scripted_llm else "not_used"
        settings_contained = fixture is not None and _settings_contained(fixture, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    attestation = IsolationAttestation(
        real_database_unchanged=_database_fingerprint(_real_database_path()) == real_before,
        settings_contained=settings_contained,
        network_blocked=not network_guard.attempted,
        sandbox_removed=not root.exists(),
    )
    if not attestation.passed:
        result.status = "failed"
        result.failure = FailureRecord(
            kind="isolation",
            message="Evaluation isolation attestation failed.",
            detail=attestation.model_dump(mode="json"),
        )
    return result, attestation, schema_version, llm_mode


def _attach_repeatability_assertion(
    result: EvaluationCaseResult, repeated: list[EvaluationCaseResult]
) -> None:
    digests = [item.semantic_digest for item in repeated]
    statuses = [item.status for item in repeated]
    stable = len(set(digests)) == 1 and len(set(statuses)) == 1
    result.assertions.append(
        AssertionResult(
            name="repeatability",
            passed=stable,
            expected="three identical semantic digests and statuses",
            actual={"digests": digests, "statuses": statuses},
        )
    )
    result.metrics["repeatability_stable"] = stable
    if not stable:
        result.status = "failed"
        result.failure = FailureRecord(
            kind="regression",
            message="Fresh Evaluation sandboxes produced different semantic outcomes.",
        )


def _real_database_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "knowledge" / "knowledge.db"


def _database_fingerprint(path: Path) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for candidate in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        values[candidate.name] = (
            hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None
        )
    return values


def _settings_contained(fixture: EvaluationFixture, root: Path) -> bool:
    expected = [
        Path(fixture.settings.knowledge_db_path),
        Path(fixture.settings.agent_checkpoint_path),
        Path(fixture.settings.knowledge_vault_path),
    ]
    root_resolved = root.resolve()
    return all(path.resolve().is_relative_to(root_resolved) for path in expected)


class _NetworkDenyGuard(AbstractContextManager["_NetworkDenyGuard"]):
    """Fail closed if an evaluation path tries to open a network socket."""

    def __init__(self) -> None:
        self.attempted = False
        self._create_connection = None
        self._socket_connect = None

    def __enter__(self):
        self._create_connection = socket.create_connection
        self._socket_connect = socket.socket.connect

        def blocked_create_connection(*args, **kwargs):
            del args, kwargs
            self.attempted = True
            raise RuntimeError("Phase 6 Evaluation blocks all network access")

        def blocked_socket_connect(instance, *args, **kwargs):
            del instance, args, kwargs
            self.attempted = True
            raise RuntimeError("Phase 6 Evaluation blocks all network access")

        socket.create_connection = blocked_create_connection
        socket.socket.connect = blocked_socket_connect
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback
        if self._create_connection is not None:
            socket.create_connection = self._create_connection
        if self._socket_connect is not None:
            socket.socket.connect = self._socket_connect
        return False
