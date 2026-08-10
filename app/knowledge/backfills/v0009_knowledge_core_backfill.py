from __future__ import annotations

from typing import Any

from app.config.settings import Settings, get_settings
from app.knowledge.core_models import BackfillSummary
from app.knowledge.core_repository import KnowledgeCoreRepository


class KnowledgeCoreBackfillUnavailableError(RuntimeError):
    """Raised before writing when the legacy Qdrant payload cannot be read."""


def run_v0009_knowledge_core_backfill(
    repository: KnowledgeCoreRepository,
    *,
    settings: Settings | None = None,
) -> BackfillSummary:
    """Backfill v8 Core records from the existing Qdrant projection.

    This is intentionally an explicit data migration, not an application-start
    migration. It reads all available legacy chunk payloads before opening the
    SQLite write transaction, so a missing Qdrant service cannot leave a partial
    backfill behind. PDF reparsing remains a later validation workflow.
    """

    settings = settings or get_settings()
    payloads = _legacy_qdrant_payloads(settings)
    return repository.backfill_legacy_documents(payloads)


def _legacy_qdrant_payloads(settings: Settings) -> list[dict[str, Any]]:
    from qdrant_client import QdrantClient

    client = QdrantClient(url=settings.qdrant_url)
    if not client.collection_exists(settings.knowledge_qdrant_collection):
        raise KnowledgeCoreBackfillUnavailableError(
            f"Qdrant collection {settings.knowledge_qdrant_collection!r} is unavailable."
        )

    payloads: list[dict[str, Any]] = []
    offset = None
    while True:
        try:
            points, offset = client.scroll(
                collection_name=settings.knowledge_qdrant_collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:  # pragma: no cover - external service boundary
            raise KnowledgeCoreBackfillUnavailableError(
                "Could not read legacy Qdrant chunk payloads."
            ) from exc
        for point in points:
            payload = dict(point.payload or {})
            if payload:
                payloads.append(payload)
        if offset is None:
            break
    return payloads
