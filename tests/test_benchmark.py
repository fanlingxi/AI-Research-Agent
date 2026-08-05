from app.knowledge.benchmark import evaluate_retrieval


class _Query:
    def search(self, query, *, topic_slugs=None, top_k=5):
        paper_id = "paper:hit" if "hit" in query else "paper:other"
        return {
            "evidence": [
                {
                    "paper_id": paper_id,
                    "chunk_id": f"{paper_id}:page:1:chunk:0",
                    "page_start": 1,
                    "text": "Grounded source text.",
                }
            ]
        }


def test_benchmark_calculates_recall_grounding_and_latency() -> None:
    dataset = {
        "version": "test-v1",
        "questions": [
            {"id": "Q1", "query": "hit", "expected_paper_ids": ["paper:hit"]},
            {"id": "Q2", "query": "miss", "expected_paper_ids": ["paper:hit"]},
        ],
    }

    result = evaluate_retrieval(dataset, _Query(), top_k=5)

    assert result["question_count"] == 2
    assert result["recall_at_k"] == 0.5
    assert result["evidence_grounding"] == 1.0
    assert result["mean_latency_ms"] >= 0
