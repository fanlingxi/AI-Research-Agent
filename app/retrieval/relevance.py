from __future__ import annotations

import re

# Canonical concepts make query planning, reranking, graph retrieval, and
# evaluation use the same interpretation of a research topic.
TOPIC_CONCEPTS: dict[str, tuple[str, ...]] = {
    "GraphRAG": ("graphrag", "graph rag"),
    "查询聚焦摘要": ("query-focused summarization", "query focused summarization"),
    "科学文献综述": ("scientific literature review", "literature review", "文献综述"),
    "多智能体协作": ("multi-agent", "multi agent", "multiagent", "多智能体"),
    "长期记忆": ("long-term memory", "long term memory", "长期记忆"),
    "智能体评估": ("agent evaluation", "llm agent evaluation", "agentbench", "智能体评估"),
    "评估": ("evaluation", "benchmark", "评估", "基准"),
    "知识图谱": ("knowledge graph", "知识图谱"),
    "向量检索": ("vector search", "vector retrieval", "向量检索"),
    "多跳推理": ("multi-hop reasoning", "multi hop reasoning", "多跳推理"),
    "科研智能体": ("research agent", "research agents", "科研智能体", "科研分析"),
}

_STOP_WORDS = {
    "about",
    "and",
    "are",
    "compare",
    "does",
    "for",
    "from",
    "how",
    "improve",
    "into",
    "methods",
    "of",
    "the",
    "this",
    "using",
    "what",
    "with",
}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("-", " ")).strip()


def contains_concept(text: str, variants: tuple[str, ...]) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(variant) in normalized for variant in variants)


def extract_topic_concepts(query: str) -> list[str]:
    return [
        concept
        for concept, variants in TOPIC_CONCEPTS.items()
        if contains_concept(query, variants)
    ]


def concept_coverage(query: str, text: str) -> float:
    concepts = extract_topic_concepts(query)
    if not concepts:
        return token_overlap(query, text)

    matched = sum(
        contains_concept(text, TOPIC_CONCEPTS[concept]) for concept in concepts
    )
    return matched / len(concepts)


def token_overlap(query: str, text: str) -> float:
    query_tokens = _significant_tokens(query)
    if not query_tokens:
        return 0.0
    text_tokens = set(_significant_tokens(text))
    return len(set(query_tokens) & text_tokens) / len(set(query_tokens))


def relevance_score(query: str, text: str) -> float:
    """Return a deterministic 0-1 relevance estimate for lightweight demos."""

    coverage = concept_coverage(query, text)
    overlap = token_overlap(query, text)
    if extract_topic_concepts(query):
        return round(0.75 * coverage + 0.25 * overlap, 3)
    return round(overlap, 3)


def _significant_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9_]{3,}", normalize_text(text))
    return [token for token in tokens if token not in _STOP_WORDS]
