"""Optional pinned local dense encoders. No remote code or lexical fallback."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

MODELS = {
    "qwen3-local": ("Qwen/Qwen3-Embedding-0.6B", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"),
    "bge-m3-local": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181"),
}
QUERY_INSTRUCTION = "Retrieve relevant passages from scientific papers that answer the question."


def model_identity(provider):
    model, revision = MODELS[provider]
    return {
        "provider": provider,
        "model": model,
        "revision": revision,
        "dimension": 1024,
        "normalization": "l2",
        "encoding": "dense-v1",
        "max_tokens": 8192,
        "query_instruction": QUERY_INSTRUCTION if provider == "qwen3-local" else None,
    }


def identity_digest(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def collection_name(settings):
    if settings.embedding_provider not in MODELS:
        return settings.knowledge_qdrant_collection
    if settings.embedding_dimension != 1024:
        raise ValueError("Pinned local encoders require embedding_dimension=1024")
    return (
        settings.knowledge_qdrant_collection
        + "__"
        + identity_digest(model_identity(settings.embedding_provider))[:16]
    )


def validate_vectors(vectors, count, dimension):
    if len(vectors) != count:
        raise ValueError("Embedding response count mismatch")
    for vector in vectors:
        if len(vector) != dimension or any(not math.isfinite(float(v)) for v in vector):
            raise ValueError("Invalid embedding dimension or nonfinite value")
        if sum(float(v) ** 2 for v in vector) <= 0:
            raise ValueError("Zero embedding is not a semantic representation")
    return vectors


@lru_cache(maxsize=1)
def _load(provider, device, cache_dir):
    import torch
    from huggingface_hub import try_to_load_from_cache
    from sentence_transformers import SentenceTransformer

    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    model, revision = MODELS[provider]
    filename = "model.safetensors" if provider == "qwen3-local" else "pytorch_model.bin"
    cached = try_to_load_from_cache(model, filename, revision=revision, cache_dir=cache_dir)
    if not isinstance(cached, str) or not Path(cached).is_file():
        raise RuntimeError(
            "Pinned model weights missing; complete the explicit model download first"
        )
    encoder = SentenceTransformer(
        model,
        revision=revision,
        device=device,
        cache_folder=cache_dir,
        trust_remote_code=False,
        local_files_only=True,
        model_kwargs={"torch_dtype": torch.float16 if device == "cuda" else torch.float32},
    )
    encoder.max_seq_length = 8192
    if provider == "qwen3-local":
        encoder.tokenizer.padding_side = "left"
    return encoder


@dataclass
class LocalEmbeddingProvider:
    provider: str
    device: str = "cpu"
    cache_dir: str = "data/models/embeddings"
    batch_size: int = 2
    dimension: int = 1024

    def __post_init__(self):
        if self.provider not in MODELS or self.dimension != 1024:
            raise ValueError("Unknown local model or incompatible dimension")
        if self.device not in {"cpu", "cuda"} or not 1 <= self.batch_size <= 32:
            raise ValueError("Invalid embedding device or batch size")

    @property
    def identity(self):
        return model_identity(self.provider)

    def embed_query(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding query must not be empty")
        if self.provider == "qwen3-local":
            text = f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}"
        return self._encode([text])[0]

    def embed_documents(self, texts):
        return self._encode(texts)

    def _encode(self, texts):
        if not texts:
            return []
        if len(texts) > 20000 or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("Embedding requires bounded nonempty text inputs")
        encoder = _load(self.provider, self.device, self.cache_dir)
        for start in range(0, len(texts), self.batch_size):
            tokenized = encoder.tokenizer(texts[start : start + self.batch_size], truncation=False)
            if any(len(ids) > 8192 for ids in tokenized["input_ids"]):
                raise ValueError("Embedding input exceeds 8192 tokens; truncation is forbidden")
        vectors = encoder.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            prompt="",
        ).tolist()
        return validate_vectors(vectors, len(texts), self.dimension)
