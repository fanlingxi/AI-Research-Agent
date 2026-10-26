"""D036 frozen-knowledge A0/A1 comparison; --plan never opens a model or store."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.benchmarking import holdout_v2, resume_closeout
from app.benchmarking.store_validation import settings_for
from app.context.knowledge_reader import KnowledgeContextReader
from app.knowledge.projector import QdrantKnowledgeIndexer
from app.schemas.documents import DocumentChunk

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "benchmarks/research/holdout-v4"
DATA = ROOT / "data/evaluation/holdout-v4"
ARMS = holdout_v2.ARM_PRESETS["ablation-v1"][:2]


def cases(root: Path | None = None) -> list[dict]:
    payload = resume_closeout.read((root or DATASET) / "tasks.holdout.json")
    return [
        {"id": t["id"], "question": t["question"], "source_ids": t["allowed_source_ids"]}
        for t in (payload if isinstance(payload, list) else payload["tasks"])
    ]


def plan(root: Path | None = None) -> dict:
    count = len(cases(root))
    runs = count * len(ARMS)
    return {
        "mode": "dry_run", "dataset": "v4", "label_provenance": "ai-consensus",
        "split": "holdout", "cases": count, "arms": [a["name"] for a in ARMS],
        "workflows": [a["workflow"] for a in ARMS], "runs": runs,
        "max_model_calls": runs * holdout_v2.PER_RUN_CALLS + count,
    }


def verify_freeze(root: Path | None = None, data: Path | None = None) -> dict:
    """Hash every frozen input before creating an archive or opening the database."""
    root, data = root or DATASET, data or DATA
    freeze = resume_closeout.read(root / "freeze.json")

    def check(path, expected):
        if resume_closeout.digest(path) != expected:
            raise ValueError(f"Freeze hash mismatch: {path}")

    check(root / "tasks.holdout.json", freeze["tasks_sha256"])
    check(root / "corpus.json", freeze["corpus_sha256"])
    corpus = resume_closeout.read(root / "corpus.json")
    sources = corpus["sources"]
    if (len({s["id"] for s in sources}) != len(sources)
            or {s["id"] for s in sources} != set(freeze["pdf_sha256"])):
        raise ValueError("Freeze PDF inventory mismatch")
    for source in sources:
        expected = freeze["pdf_sha256"][source["id"]]
        if source["pdf_sha256"] != expected:
            raise ValueError(f"Freeze hash mismatch: {source['id']}")
        check(data / source["pdf_path"], expected)
    # Resolve the manifest's repository-relative path within the selected evaluation domain.
    store_relative = Path(freeze["local_store"]).relative_to("data/evaluation/holdout-v4")
    store = data / store_relative
    check(store, freeze["local_store_sha256"])
    check(data / "approved-evidence.json", freeze["approved_evidence_sha256"])
    # The frozen file must be self-contained: immutable reads intentionally ignore a WAL.
    if Path(f"{store}-wal").exists() and Path(f"{store}-wal").stat().st_size:
        raise ValueError("Frozen store has an outstanding WAL")
    return {"freeze": freeze, "sources": sources, "store": store}


def formal_bundles(seed: Path, scopes: list[str]):
    with closing(sqlite3.connect(
        (seed / "knowledge.db").resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
    )) as connection:
        connection.row_factory = sqlite3.Row
        return KnowledgeContextReader().read_formal_bundles_tx(connection, scopes)


def projection_chunks(seed: Path, mapping: dict) -> list[DocumentChunk]:
    """Index reviewed evidence, including knowledge without a Paper-type entity."""
    chunks = {}
    scopes = [s["scope"] for s in mapping["sources"].values()]
    for bundle in formal_bundles(seed, scopes):
        for item in bundle.evidence:
            chunk = item.chunk
            chunks[chunk["id"]] = DocumentChunk(
                id=chunk["id"], paper_id=chunk["document_id"], title=item.document["title"],
                text=chunk["content"], chunk_index=chunk["chunk_index"],
                token_count=max(1, len(chunk["content"].split())), source_tier="primary_fulltext",
                metadata={"page_start": chunk["page_start"], "page_end": chunk["page_end"]},
            )
    return [chunks[key] for key in sorted(chunks)]


def prepare(*, root: Path | None = None, data: Path | None = None):
    resume_closeout.guard("RUN_STORE_INTEGRATION")
    root, data = root or DATASET, data or DATA
    verified = verify_freeze(root, data)
    selected = cases(root)
    mapping = {"sources": {
        s["id"]: {"scope": s["collection_scope"]} for s in verified["sources"]
    }}
    if any(not s["scope"] for s in mapping["sources"].values()) or any(
        not c["source_ids"] or not set(c["source_ids"]) <= mapping["sources"].keys()
        for c in selected
    ):
        raise ValueError("Unknown or empty case source scope")
    output = data / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    seed = output / "seed"
    seed.mkdir()
    config = {
        "schema": "holdout-v4-run-v1", "dataset": "v4", "split": "holdout",
        "label_provenance": "ai-consensus", "human_labels": None,
        "semantic_success_rate": None, "cases_sha256": resume_closeout.digest(root / "freeze.json"),
        "production_code": resume_closeout.fingerprints(),
        "collection": f"holdout_v4_{uuid4().hex}", "prepared": False,
        "max_calls": plan(root)["max_model_calls"], "per_run_calls": holdout_v2.PER_RUN_CALLS,
        "context_tokens": 16000, "run_tokens": 1048576,
        "arms": list(ARMS), "workflows": [a["workflow"] for a in ARMS], "cases": selected,
    }
    resume_closeout.write(output / "config.json", config)
    # immutable + mode=ro also prevents creating sidecars beside the frozen original.
    with closing(sqlite3.connect(
        verified["store"].resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
    )) as source, closing(sqlite3.connect(seed / "knowledge.db")) as target:
        source.backup(target)
    settings = settings_for(seed, config["collection"])
    resume_closeout.write(seed / "source-map.json", mapping)
    chunks = projection_chunks(seed, mapping)
    if not chunks:
        raise ValueError("Frozen store contains no formal projection chunks")
    QdrantKnowledgeIndexer(settings).index(chunks)
    config.update(
        prepared=True, chunks=len(chunks),
        seed_files={name: resume_closeout.digest(seed / name)
                    for name in ("knowledge.db", "source-map.json")},
    )
    resume_closeout.write(output / "config.json", config)
    return {"output": str(output), "chunks": len(chunks), "model_calls": 0}


def validate_snapshot(package, case, mapping, seed):
    """Rehydrate allowed formal evidence from the immutable seed, never from hints.

    The production reader joins collection membership to ingestion documents and
    checks publication, source identity/version and content hashes. Compare every
    snapshot evidence and chunk to those authoritative rows, including actual text.
    """
    scopes = [mapping["sources"][sid]["scope"] for sid in case["source_ids"]]
    allowed = {b.claim["id"]: b for b in formal_bundles(seed, scopes)}
    for bundle in package.knowledge.claim_bundles:
        original = allowed.get(bundle.claim.claim_id)
        if original is None:
            raise ValueError("Snapshot claim outside allowed seed scope")
        evidence = {e.evidence["id"]: e for e in original.evidence}
        chunks = {e.chunk["id"]: e.chunk for e in original.evidence}
        snapshot_chunks = {c.chunk_id for c in bundle.chunks}
        for item in bundle.evidence:
            entry = evidence.get(item.evidence_id)
            if (entry is None or item.claim_id != bundle.claim.claim_id
                    or item.chunk_id not in snapshot_chunks
                    or any(getattr(item, key) != entry.evidence[key] for key in (
                        "source_id", "chunk_id", "quote", "quote_sha256",
                    ))):
                raise ValueError("Snapshot evidence identity or scope violation")
        for chunk in bundle.chunks:
            entry = chunks.get(chunk.chunk_id)
            if (entry is None or chunk.document_id != entry["document_id"]
                    or chunk.content_sha256 != entry["content_sha256"]
                    or hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
                    != entry["content_sha256"]):
                raise ValueError("Snapshot chunk identity or hash violation")


def execute(output):
    # Preserve the shared runner's guards even before opening local validation inputs.
    resume_closeout.guard("RUN_LIVE_LLM_INTEGRATION")
    resume_closeout.guard("RUN_STORE_INTEGRATION")
    output = Path(output).resolve(strict=True)
    config = resume_closeout.read(output / "config.json")
    if config["dataset"] != "v4":
        raise ValueError("Expected a prepared v4 archive")
    for name, expected in config["seed_files"].items():
        if resume_closeout.digest(output / "seed" / name) != expected:
            raise ValueError(f"Seed hash mismatch: {name}")
    return resume_closeout.execute(
        output, cases_path=DATASET / "freeze.json", output_root=DATA / "runs",
        snapshot_validator=validate_snapshot,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true")
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--execute", type=Path)
    args = parser.parse_args()
    result = prepare() if args.prepare else execute(args.execute) if args.execute else plan()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
