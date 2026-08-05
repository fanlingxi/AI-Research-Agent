from __future__ import annotations

from dataclasses import dataclass

from app.schemas.documents import EvidenceSourceTier, PaperMetadata, RetrievalHit

_TIER_SCORES: dict[EvidenceSourceTier, float] = {
    "primary_fulltext": 1.0,
    "online_metadata": 0.75,
    "offline_demo": 0.0,
    "unknown": 0.7,
}
_REAL_TIERS = {"primary_fulltext", "online_metadata"}


@dataclass(frozen=True)
class EvidenceAssessment:
    """Source-quality outcome shared by evaluation, memory, and Vault export."""

    score: float
    status: str
    admissible: bool
    primary_hits: int
    real_hits: int
    offline_hits: int
    reason: str


def source_tier(value: str | None) -> EvidenceSourceTier:
    if value in _TIER_SCORES:
        return value  # type: ignore[return-value]
    return "unknown"


def source_score(value: str | None) -> float:
    return _TIER_SCORES[source_tier(value)]


def is_real_source(value: str | None) -> bool:
    return source_tier(value) in _REAL_TIERS


def is_offline_demo(value: str | None) -> bool:
    return source_tier(value) == "offline_demo"


def source_tier_from_hit(hit: RetrievalHit) -> EvidenceSourceTier:
    value = hit.source_tier
    if value == "unknown":
        value = str(hit.metadata.get("source_tier", "unknown"))
    return source_tier(value)


def assess_evidence(
    papers: list[PaperMetadata],
    hits: list[RetrievalHit],
    minimum_score: float,
) -> EvidenceAssessment:
    """Determine whether evidence may be retained as formal research knowledge."""

    hit_tiers = [source_tier_from_hit(hit) for hit in hits]
    primary_hits = sum(tier == "primary_fulltext" for tier in hit_tiers)
    real_hits = sum(tier in _REAL_TIERS for tier in hit_tiers)
    offline_hits = sum(tier == "offline_demo" for tier in hit_tiers)
    has_real_papers = any(is_real_source(paper.source_tier) for paper in papers)

    if not hits:
        return EvidenceAssessment(
            0.0,
            "blocked",
            False,
            0,
            0,
            0,
            "没有检索到可验证的证据片段。",
        )

    score = round(sum(source_score(tier) for tier in hit_tiers) / len(hit_tiers), 3)
    if real_hits == 0:
        return EvidenceAssessment(
            score,
            "simulation",
            False,
            primary_hits,
            real_hits,
            offline_hits,
            "检索结果只包含离线演示或未知来源，结果仅可作为模拟演示。",
        )
    if has_real_papers and offline_hits:
        return EvidenceAssessment(
            score,
            "blocked",
            False,
            primary_hits,
            real_hits,
            offline_hits,
            "存在真实资料时，正式证据集合不得混入 offline-demo 占位材料。",
        )
    if score < minimum_score:
        return EvidenceAssessment(
            score,
            "blocked",
            False,
            primary_hits,
            real_hits,
            offline_hits,
            f"来源质量评分为 {score:.2f}，低于正式研究门槛 {minimum_score:.2f}。",
        )

    detail = "包含显式 PDF 全文证据。" if primary_hits else "使用可追溯在线论文元数据。"
    return EvidenceAssessment(
        score,
        "formal",
        True,
        primary_hits,
        real_hits,
        offline_hits,
        f"{detail} 共 {real_hits} 条真实来源检索证据。",
    )


def rerank_evidence_hits(hits: list[RetrievalHit], top_k: int) -> list[RetrievalHit]:
    """Prefer explicit full text and remove demos when real evidence is present."""

    has_real = any(is_real_source(source_tier_from_hit(hit)) for hit in hits)
    eligible = (
        [hit for hit in hits if is_real_source(source_tier_from_hit(hit))]
        if has_real
        else hits
    )

    def rank_key(hit: RetrievalHit) -> tuple[float, float, str]:
        priority = {"primary_fulltext": 0.18, "online_metadata": 0.08}.get(
            source_tier_from_hit(hit),
            0.0,
        )
        return (round(hit.score + priority, 6), hit.score, hit.chunk_id)

    return sorted(eligible, key=rank_key, reverse=True)[:top_k]
