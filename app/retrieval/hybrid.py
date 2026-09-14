"""Payload-free BM25 and reciprocal rank fusion over caller-authorized IDs."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Literal

RetrievalStrategy = Literal["legacy", "bm25-v1", "hybrid-v1", "dense-v1"]
CHANNEL_LIMIT = 40
RRF_K = 60
PARAMETERS = {
    "tokenizer": "nfkc-identifiers-cjk12-v1", "k1": 1.2, "b": 0.75,
    "rrf_k": RRF_K, "channel_limit": CHANNEL_LIMIT,
    "fusion_rank_kind": "verified_unique_position", "tie_break": "id",
}
_STOP = {"a", "an", "the", "of", "to", "in", "and", "or", "is", "for", "with"}


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens = []
    for word in re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text):
        compact = word.replace("-", "").replace("_", "")
        if compact not in _STOP:
            tokens.append(compact)
            parts = re.findall(r"[a-z]+|[0-9]+", word)
            if len(parts) > 1:
                tokens.extend(parts)
    for sequence in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(sequence)
        tokens.extend(sequence[i:i + 2] for i in range(len(sequence) - 1))
    return tokens


def bm25_rank(
    query: str, documents: Mapping[str, str], *, k1: float = 1.2, b: float = 0.75,
    limit: int = CHANNEL_LIMIT,
) -> list[tuple[str, float]]:
    if not math.isfinite(k1) or k1 <= 0 or not math.isfinite(b) or not 0 <= b <= 1:
        raise ValueError("Invalid BM25 parameters")
    if limit < 1:
        raise ValueError("limit must be positive")
    query_tokens = set(tokenize(query))
    if not documents or not query_tokens:
        return []
    corpus = {key: Counter(tokenize(value)) for key, value in documents.items()}
    lengths = {key: sum(counts.values()) for key, counts in corpus.items()}
    average = sum(lengths.values()) / len(corpus)
    if not average:
        return []
    frequencies = Counter(token for counts in corpus.values() for token in counts)
    result = []
    for key, counts in corpus.items():
        score = 0.0
        for term in query_tokens:
            count = counts[term]
            if count:
                idf = math.log1p((len(corpus) - frequencies[term] + 0.5)
                                / (frequencies[term] + 0.5))
                score += idf * count * (k1 + 1) / (
                    count + k1 * (1 - b + b * lengths[key] / average)
                )
        if score > 0:
            result.append((key, score))
    return sorted(result, key=lambda item: (-item[1], item[0]))[:limit]


def reciprocal_rank_fusion(
    channels: Mapping[str, Sequence[str]], *, k: int = RRF_K,
) -> list[tuple[str, float, dict[str, int]]]:
    if k < 1:
        raise ValueError("RRF k must be positive")
    ranks: dict[str, dict[str, int]] = {}
    for channel, ids in channels.items():
        for rank, key in enumerate(dict.fromkeys(ids), start=1):
            ranks.setdefault(key, {})[channel] = rank
    return sorted(
        ((key, sum(1 / (k + rank) for rank in channels.values()), channels)
         for key, channels in ranks.items()),
        key=lambda item: (-item[1], item[0]),
    )
