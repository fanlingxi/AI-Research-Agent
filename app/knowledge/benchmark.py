from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Protocol

from app.config.settings import get_settings
from app.knowledge.query import KnowledgeQueryService
from app.knowledge.repository import KnowledgeRepository
from app.retrieval.neural_embeddings import MODELS, collection_name, model_identity


class BenchmarkQuery(Protocol):
    def search(
        self, query: str, *, topic_slugs: list[str] | None = None, top_k: int = 5
    ) -> dict[str, Any]: ...


def evaluate_retrieval(
    dataset: dict[str, Any], query_service: BenchmarkQuery, *, top_k: int = 5
) -> dict[str, Any]:
    """Evaluate formal-chunk retrieval with a versioned, reviewable question set."""

    results = []
    for item in dataset["questions"]:
        started = time.perf_counter()
        response = query_service.search(
            item["query"],
            topic_slugs=item.get("topic_slugs", []),
            top_k=top_k,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        evidence = response.get("evidence", [])
        retrieved = [str(entry.get("paper_id", "")) for entry in evidence]
        expected = set(item["expected_paper_ids"])
        hit = bool(expected.intersection(retrieved[:top_k]))
        grounded = all(
            entry.get("paper_id")
            and entry.get("chunk_id")
            and int(entry.get("page_start", 0)) >= 1
            and str(entry.get("text", "")).strip()
            for entry in evidence
        )
        results.append(
            {
                "id": item["id"],
                "hit": hit,
                "grounded": grounded,
                "retrieved_paper_ids": retrieved,
                "latency_ms": round(elapsed_ms, 2),
            }
        )
    count = len(results)
    return {
        "dataset_version": dataset["version"],
        "data_profile": dataset.get("data_profile", {}),
        "top_k": top_k,
        "question_count": count,
        "recall_at_k": round(sum(item["hit"] for item in results) / count, 4),
        "evidence_grounding": round(sum(item["grounded"] for item in results) / count, 4),
        "mean_latency_ms": round(mean(item["latency_ms"] for item in results), 2),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Knowledge Core retrieval benchmark.")
    parser.add_argument(
        "--dataset",
        default="benchmarks/knowledge_core_questions.json",
        help="Versioned benchmark JSON file.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", default="", help="Optional result JSON path.")
    args = parser.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    settings = get_settings()
    repository = KnowledgeRepository(settings.knowledge_db_path)
    result = evaluate_retrieval(
        dataset,
        KnowledgeQueryService(repository, settings=settings),
        top_k=args.top_k,
    )
    result["executed_at"] = datetime.now(tz=UTC).isoformat()
    result["runtime_configuration"] = {
        "embedding_provider": settings.embedding_provider,
        "embedding_model": MODELS.get(settings.embedding_provider,
                                      (settings.embedding_model, None))[0],
        "embedding_dimension": settings.embedding_dimension,
        "qdrant_collection": collection_name(settings),
        **({"embedding_identity": model_identity(settings.embedding_provider)}
           if settings.embedding_provider in MODELS else {}),
        "estimated_embedding_cost_usd": (0.0 if settings.embedding_provider == "hash" else None),
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
