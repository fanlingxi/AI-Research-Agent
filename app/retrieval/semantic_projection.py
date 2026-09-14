"""Immutable local dense projection for explicit evaluation or injected retrieval.

SQLite remains authoritative. Consumers must rehydrate every candidate there.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np

from app.knowledge.schemas import ChunkSearchHit
from app.retrieval.contracts import SourceIdentity
from app.retrieval.neural_embeddings import identity_digest, validate_vectors


class SemanticProjection:
    def __init__(self, repository, provider, directory):
        self.provider = provider
        self.directory = Path(directory)
        with repository.database.connect() as db:
            rows = db.execute("""
                SELECT c.id, c.content, c.content_sha256 AS chunk_sha, c.document_id,
                       d.content_sha256 AS document_sha, d.source_id,
                       s.version, s.content_sha256 AS source_sha
                FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN sources s ON s.id=d.source_id ORDER BY c.id
            """).fetchall()
        self.identities = [
            SourceIdentity(
                source_id=r["source_id"],
                source_version=r["version"],
                source_sha256=r["source_sha"],
                document_id=r["document_id"],
                document_sha256=r["document_sha"],
                chunk_id=r["id"],
                chunk_sha256=r["chunk_sha"],
            )
            for r in rows
        ]
        identity = getattr(
            provider, "identity", {"provider": "hash", "dimension": provider.dimension}
        )
        self.manifest = {
            "schema": "semantic-projection-v1",
            "model": identity,
            "sources": [i.model_dump() for i in self.identities],
        }
        self.digest = identity_digest(self.manifest)
        manifest_path = self.directory / "manifest.json"
        vectors_path = self.directory / "vectors.npy"
        self.query_cache = {}
        if manifest_path.exists():
            stored = json.loads(manifest_path.read_text(encoding="utf-8"))
            if stored["identity"] != self.manifest:
                raise ValueError("Projection model or source identity changed; use a new index")
            if hashlib.sha256(vectors_path.read_bytes()).hexdigest() != stored["vectors_sha256"]:
                raise ValueError("Projection vectors fingerprint mismatch")
            self.vectors = np.load(vectors_path, allow_pickle=False)
        else:
            self.directory.mkdir(parents=True, exist_ok=False)
            started = time.perf_counter()
            vectors = provider.embed_documents([r["content"] for r in rows])
            validate_vectors(vectors, len(rows), provider.dimension)
            self.vectors = np.asarray(vectors, dtype=np.float32).reshape(-1, provider.dimension)
            np.save(vectors_path, self.vectors, allow_pickle=False)
            manifest_path.write_text(
                json.dumps(
                    {
                        "identity": self.manifest,
                        "index_digest": self.digest,
                        "build_seconds": time.perf_counter() - started,
                        "encoded_texts": len(rows),
                        "vectors_sha256": hashlib.sha256(vectors_path.read_bytes()).hexdigest(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        validate_vectors(self.vectors, len(rows), provider.dimension)

    def search(self, query, *, allowed_paper_ids, top_k):
        if not allowed_paper_ids or not self.identities or top_k <= 0:
            return []
        if query not in self.query_cache:
            vector = self.provider.embed_query(query)
            validate_vectors([vector], 1, self.provider.dimension)
            self.query_cache[query] = np.asarray(vector, dtype=np.float32)
        vector = self.query_cache[query]
        # Both model encoders normalize; normalize again for injected test providers.
        norms = np.linalg.norm(self.vectors, axis=1) * np.linalg.norm(vector)
        scores = self.vectors @ vector / norms
        eligible = [
            i for i, source in enumerate(self.identities) if source.document_id in allowed_paper_ids
        ]
        ordered = sorted(eligible, key=lambda i: (-float(scores[i]), self.identities[i].chunk_id))
        return [
            ChunkSearchHit(
                chunk_id=self.identities[i].chunk_id,
                score=float(scores[i]),
                source_identity=self.identities[i],
            )
            for i in ordered[: min(max(top_k * 6, top_k), 100)]
        ]
