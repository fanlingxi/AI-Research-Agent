"""Explicit real Qwen/Qdrant integration. Synthetic data is not a quality benchmark."""

from __future__ import annotations

import argparse
import json
import os
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.benchmarking.passage_dataset import import_passages
from app.config.settings import Settings
from app.context.models import ContextBuildRequest, canonical_package_sha256
from app.context.retrieval import QdrantContextCandidateRetriever
from app.context.service import ContextBuilderService
from app.knowledge.projector import QdrantKnowledgeIndexer
from app.knowledge.query import QdrantKnowledgeSearch
from app.knowledge.repository import KnowledgeRepository
from app.persistence.backup import backup_database, restore_database
from app.retrieval.neural_embeddings import collection_name, model_identity

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data/evaluation/a08-store"


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def settings_for(folder, name):
    return Settings(
        _env_file=None,
        llm_provider="mock",
        embedding_provider="qwen3-local",
        embedding_dimension=1024,
        embedding_device="cuda",
        embedding_cache_dir=str(ROOT / "data/models/embeddings"),
        qdrant_url="http://127.0.0.1:6333",
        knowledge_qdrant_collection=name,
        knowledge_db_path=str(folder / "knowledge.db"),
        agent_checkpoint_path=str(folder / "checkpoints.db"),
        knowledge_vault_path=str(folder / "vault"),
        neo4j_uri="bolt://127.0.0.1:9",
    )


def run(*, execute=False, verify=None):
    if not execute:
        return {"mode": "dry_run", "model_calls": 0, "scope": "synthetic service contracts"}
    if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("RUN_STORE_INTEGRATION") == "0":
        raise ValueError("Real store execution disabled in offline tests")
    from qdrant_client import QdrantClient

    if verify:
        folder = Path(verify).resolve(strict=True)
        if folder.parent != OUTPUT.resolve():
            raise ValueError("Verify requires this tool's managed archive")
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    else:
        folder = OUTPUT / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        folder.mkdir(parents=True, exist_ok=False)
        config = {"collection": f"a08_store_{uuid4().hex}", "model": model_identity("qwen3-local")}
        write(folder / "config.json", config)
    settings = settings_for(folder, config["collection"])
    repo = KnowledgeRepository(settings.knowledge_db_path)
    search = QdrantKnowledgeSearch(settings)
    builder = ContextBuilderService(repo, vector_retriever=QdrantContextCandidateRetriever(search))
    with closing(QdrantClient(url=settings.qdrant_url, timeout=10)) as client:
        server = client.info().version
        if verify:
            state = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            package = builder.build_context(ContextBuildRequest.model_validate(state["request"]))
            assert (
                sorted(b.claim.claim_id for b in package.knowledge.claim_bundles) == state["claims"]
            )
            assert client.count(collection_name(settings), exact=True).count == state["points"]
            result = {"server_version": server, "restart_read": "passed", "claims": state["claims"]}
            write(folder / f"restart-{uuid4().hex[:8]}.json", result)
            return {"output": str(folder), **result}

        sources = []
        for key, paragraphs in {
            "allowed": [
                "Atomic review commits approved facts and projection intents "
                "in one SQLite transaction.",
                "A worker retries failed vector projections idempotently "
                "using stable chunk identifiers.",
                "This unreviewed passage must not become authoritative evidence.",
            ],
            "denied": ["A restricted paper claims unrelated results outside this project."],
        }.items():
            text = "\n".join(paragraphs)
            pages, start = [], 0
            for index, part in enumerate(paragraphs):
                end = start + len(part) + (1 if index < len(paragraphs) - 1 else 0)
                pages.append(SimpleNamespace(start=start, end=end, pdf_page=index + 1))
                start = end
            sources.append(
                SimpleNamespace(
                    id=key,
                    title=f"Synthetic {key}",
                    pdf_url=f"synthetic://{key}",
                    pdf_path=f"{key}.txt",
                    pdf_pages=len(pages),
                    pages=pages,
                    text=text,
                    version="v1",
                    pdf_sha256=None,
                    license="project-authored synthetic text",
                )
            )
        mapping = import_passages(repo, SimpleNamespace(sources=sources), folder)
        chunks = repo.list_projection_chunks()
        indexer = QdrantKnowledgeIndexer(settings)
        indexer.index(chunks)
        indexer.index(chunks)
        assert client.count(collection_name(settings), exact=True).count == len(chunks)
        denied = next(c for c in chunks if mapping["chunks"][c.id]["source_id"] == "denied")
        allowed = mapping["sources"]["allowed"]["document_id"]
        # Forge the projection's scope hint. SQLite must still reject its real ID.
        client.set_payload(
            collection_name(settings),
            payload={"paper_id": allowed, "text": "FORGED PAYLOAD"},
            points=[str(uuid5(NAMESPACE_URL, denied.id))],
            wait=True,
        )
        assert search.search("原子审核与失败恢复", allowed_paper_ids=set(), top_k=10) == []
        raw = search.search("原子审核与失败恢复", allowed_paper_ids={allowed}, top_k=10)
        assert denied.id in {hit.chunk_id for hit in raw}
        unreviewed = next(
            k
            for k, v in mapping["chunks"].items()
            if v["source_id"] == "allowed" and v["pdf_page"] == 3
        )
        with repo.database.connect() as db:
            db.execute(
                "UPDATE claims SET status='proposed' WHERE id=?",
                (mapping["chunks"][unreviewed]["claim_id"],),
            )
        mem = repo.memory_repository
        project = mem.create_project(
            name="Store validation",
            goal="原子审核与失败恢复",
            domain="research",
            metadata={"evaluation_only": True},
        )
        mem.replace_project_knowledge_scopes(
            project.id,
            [mapping["sources"]["allowed"]["scope"]],
            expected_project_revision=project.revision,
        )
        task = mem.create_workspace_task(
            project_id=project.id,
            title="原子审核与失败恢复",
            goal="原子审核与失败恢复",
            priority="high",
            metadata={},
        )
        request = ContextBuildRequest(
            task_id=task.id,
            max_tokens=16000,
            enable_vector_candidates=True,
            retrieval_strategy="dense-v1",
        )
        package = builder.build_context(request)
        selected = {c.chunk_id for b in package.knowledge.claim_bundles for c in b.chunks}
        assert len(selected) == 2 and not {denied.id, unreviewed} & selected
        assert all(
            "FORGED PAYLOAD" not in c.content
            for b in package.knowledge.claim_bundles
            for c in b.chunks
        )
        stale = sorted(selected)[0]
        with repo.database.connect() as db:
            original = db.execute(
                "SELECT content_sha256 FROM chunks WHERE id=?", (stale,)
            ).fetchone()[0]
            db.execute("UPDATE chunks SET content_sha256='stale' WHERE id=?", (stale,))
        try:
            filtered = builder.build_context(request)
            assert stale not in {
                c.chunk_id for b in filtered.knowledge.claim_bundles for c in b.chunks
            }
        finally:
            with repo.database.connect() as db:
                db.execute("UPDATE chunks SET content_sha256=? WHERE id=?", (original, stale))
        assert (
            canonical_package_sha256(builder.snapshot_repository.get(package.snapshot_id))
            == package.package_sha256
        )
        backup_database(Path(settings.knowledge_db_path), folder / "backup")
        restore_database(folder / "backup", folder / "restored")
        result = {
            "server_version": server,
            "model": config["model"],
            "points": len(chunks),
            "request": request.model_dump(),
            "claims": sorted(b.claim.claim_id for b in package.knowledge.claim_bundles),
            "checks": [
                "real_cuda_encoding",
                "http_index_query",
                "idempotent_upsert",
                "empty_scope",
                "forged_scope_filtered",
                "unreviewed_filtered",
                "stale_content_filtered",
                "frozen_snapshot_unchanged",
                "backup_restore",
            ],
            "quality_score": None,
            "llm_calls": 0,
        }
        write(folder / "result.json", result)
        return {"output": str(folder), **result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(execute=args.execute, verify=args.verify), ensure_ascii=False, indent=2))
