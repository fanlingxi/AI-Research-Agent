"""Fuse per-question rankings of complete, already verified claim bundles."""

from app.context.ranking import RankedKnowledgeBundle, rank_bundles
from app.retrieval.hybrid import CHANNEL_LIMIT, reciprocal_rank_fusion


def rank_query_bundles(bundles, queries, query_candidates, query_audits, *, hybrid=False):
    channels = {}
    by_id = {b.claim["id"]: b for b in bundles}
    details = []
    for index, (query, candidates, audits) in enumerate(
        zip(queries, query_candidates, query_audits, strict=True)
    ):
        ranked = rank_bundles(bundles, query=query, candidate_references=candidates,
                              candidate_audits=audits,
                              strategy="hybrid-v1" if hybrid else "bm25-v1")
        channel = f"query_{index}"
        channels[channel] = [r.bundle.claim["id"] for r in ranked[:CHANNEL_LIMIT]]
        details.append({"query_index": index, "query": query, "rankings": [
            {"claim_id": r.bundle.claim["id"], "rank": r.rank,
             "scores": r.score_breakdown} for r in ranked[:CHANNEL_LIMIT]
        ], "candidates": [a.model_dump(mode="json") for a in audits]})
    results = [RankedKnowledgeBundle(
        bundle=by_id[key], rank=index, score=score,
        score_breakdown={"rrf_score": score, **{f"{q}_rank": r for q, r in ranks.items()}},
        channels=list(ranks), selected_reason="SQLite-verified finite query fusion",
    ) for index, (key, score, ranks) in enumerate(reciprocal_rank_fusion(channels), 1)]
    return results, details
