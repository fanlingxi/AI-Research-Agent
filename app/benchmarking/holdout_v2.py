"""Frozen AI-consensus holdout-v2 through the production chain (prepare / execute).

Same path as the resume closeout: literal passages enter an isolated evaluation SQLite via
the review/publish entry, Qdrant indexes them, every case gets one frozen ContextSnapshot,
and each workflow arm runs through Worker and final commit. Labels never reach the model:
only id, question and the allowed source scope leave the task file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.benchmarking import resume_closeout
from scripts.validate_holdout_v2 import (
    DATASET,
    data_path,
    dataset_path,
    dataset_version,
    extract_pages,
    load_tasks,
    validate_dataset,
)

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data/evaluation/holdout-v2"
OUTPUT = DATA / "runs"
FREEZE = DATASET / "freeze.json"
WORKFLOWS = ("research_v4", "research_v7")  # same-snapshot generation comparison
# D029 pre-registered pipeline comparison: current default vs candidate end to end.
ARMS = (
    {"name": "A-default", "workflow": "research", "context": {}},
    {
        "name": "B-candidate",
        "workflow": "research_v7",
        "context": resume_closeout.SHARED_CONTEXT,
    },
)
# v7-vs-v8: identical candidate snapshot, only the final-report workflow differs (dev tuning).
ARM_PRESETS = {
    "d029": ARMS,
    "d030": (
        {"name": "A-default", "workflow": "research", "context": {}},
        {"name": "C-candidate", "workflow": "research_v8",
         "context": resume_closeout.SHARED_CONTEXT},
    ),
    "v7-vs-v8": (
        {"name": "v7-direct", "workflow": "research_v7", "context": resume_closeout.SHARED_CONTEXT},
        {"name": "v8-anchored", "workflow": "research_v8",
         "context": resume_closeout.SHARED_CONTEXT},
    ),
}
PER_RUN_CALLS = 4


def arm_preset(arms) -> tuple | None:
    """``True`` keeps the original D029 flag meaning; a string selects a named preset."""
    if not arms:
        return None
    return ARM_PRESETS["d029" if arms is True else arms]


def cases(split: str = "holdout", dataset: str = "v2") -> list[dict]:
    """Allowlist the model-facing fields; expectations and labels stay behind."""
    return [
        {"id": t.id, "question": t.question, "source_ids": list(t.allowed_source_ids)}
        for t in load_tasks(dataset_path(dataset))
        if split == "all" or t.split == split
    ]


def plan(split: str = "holdout", workflows=WORKFLOWS, arms=False, dataset: str = "v2") -> dict:
    selected = cases(split, dataset)
    preset = arm_preset(arms)
    if preset:
        workflows = [a["workflow"] for a in preset]
    runs = len(selected) * len(workflows)
    return {
        "mode": "dry_run",
        "dataset": dataset_version(dataset),
        "label_provenance": "ai-consensus",
        "split": split,
        "cases": len(selected),
        "workflows": list(workflows),
        "arms": [a["name"] for a in preset] if preset else None,
        "runs": runs,
        "max_model_calls": runs * PER_RUN_CALLS + len(selected),
    }


def load_corpus(data: Path, source_ids: set[str], *, download: bool = False, dataset: str = "v2"):
    """Rebuild the exact frozen text from local PDFs; any byte or text drift is fatal."""
    corpus = json.loads((dataset_path(dataset) / "corpus.json").read_text(encoding="utf-8"))
    sources = []
    for meta in corpus["sources"]:
        if meta["id"] not in source_ids:
            continue
        pdf = data / meta["pdf_path"]
        if not pdf.is_file():
            if not download:
                raise FileNotFoundError(f"Missing {pdf}; rerun with --download")
            request = urllib.request.Request(
                meta["pdf_url"], headers={"User-Agent": "ResearchProject/1.0"}
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read(40_000_000)
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(payload)
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != meta["pdf_sha256"]:
            raise ValueError(f"{meta['id']}: PDF differs from the frozen corpus")
        text, pages = extract_pages(pdf)
        if hashlib.sha256(text.encode()).hexdigest() != meta["text_sha256"]:
            raise ValueError(f"{meta['id']}: extracted text differs (check pypdf version)")
        sources.append(SimpleNamespace(
            **{k: meta[k] for k in ("id", "title", "version", "pdf_url", "pdf_path",
                                    "pdf_sha256", "pdf_pages", "license")},
            text=text, pages=[SimpleNamespace(**p) for p in pages],
        ))
    return SimpleNamespace(sources=sources)


def verify_corpus(split: str = "holdout", data: Path | None = None, dataset: str = "v2") -> dict:
    """Offline prepare rehearsal: PDF/text identity plus passage import into a temp DB."""
    import tempfile

    from app.benchmarking.passage_dataset import import_passages
    from app.knowledge.repository import KnowledgeRepository

    data = data or data_path(dataset)
    validate_dataset(dataset_path(dataset), data=data)
    selected = cases(split, dataset)
    corpus = load_corpus(data, {s for c in selected for s in c["source_ids"]}, dataset=dataset)
    with tempfile.TemporaryDirectory() as folder:
        repo = KnowledgeRepository(str(Path(folder) / "knowledge.db"))
        mapping = import_passages(repo, corpus, data)
        chunks = len(repo.list_projection_chunks())
    return {"mode": "verify_corpus", "cases": len(selected), "papers": len(corpus.sources),
            "passages": len(mapping["chunks"]), "projection_chunks": chunks, "model_calls": 0}


def prepare(
    split: str = "holdout", workflows=WORKFLOWS, data: Path | None = None, download=False,
    arms=False, dataset: str = "v2",
):
    resume_closeout.guard("RUN_STORE_INTEGRATION")
    from app.benchmarking.passage_dataset import import_passages
    from app.benchmarking.store_validation import settings_for
    from app.knowledge.projector import QdrantKnowledgeIndexer
    from app.knowledge.repository import KnowledgeRepository

    data = data or data_path(dataset)
    root = dataset_path(dataset)
    validate_dataset(root, data=data)
    preset = arm_preset(arms)
    if preset:
        workflows = [a["workflow"] for a in preset]
    selected = cases(split, dataset)
    corpus = load_corpus(data, {s for c in selected for s in c["source_ids"]},
                         download=download, dataset=dataset)
    output = data_path(dataset) / "runs" / (
        f"{split}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    )
    output.mkdir(parents=True, exist_ok=False)
    config = {
        "schema": f"{root.name}-run-v1",
        "dataset": dataset_version(dataset),
        "split": split,
        "label_provenance": "ai-consensus",
        "cases_sha256": resume_closeout.digest(root / "freeze.json"),
        "production_code": resume_closeout.fingerprints(),
        "collection": f"{root.name.replace('-', '_')}_{uuid4().hex}",
        "prepared": False,
        "max_calls": plan(split, workflows, arms, dataset)["max_model_calls"],
        "per_run_calls": PER_RUN_CALLS,
        "context_tokens": 16000,
        "run_tokens": 1048576,
        "workflows": list(workflows),
        "arms": list(preset) if preset else None,
        "cases": selected,
        "human_labels": None,
        "semantic_success_rate": None,
    }
    resume_closeout.write(output / "config.json", config)
    seed = output / "seed"
    seed.mkdir()
    settings = settings_for(seed, config["collection"])
    repo = KnowledgeRepository(settings.knowledge_db_path)
    mapping = import_passages(repo, corpus, data)
    resume_closeout.write(seed / "source-map.json", mapping)
    chunks = repo.list_projection_chunks()
    QdrantKnowledgeIndexer(settings).index(chunks)
    config.update(
        prepared=True,
        chunks=len(chunks),
        sources=[{k: v for k, v in vars(s).items() if k not in {"text", "pages"}}
                 for s in corpus.sources],
        seed_files={
            n: resume_closeout.digest(seed / n) for n in ["knowledge.db", "source-map.json"]
        },
    )
    resume_closeout.write(output / "config.json", config)
    return {"output": str(output), "chunks": len(chunks), "model_calls": 0}


def execute(output, dataset: str = "v2"):
    return resume_closeout.execute(
        output, cases_path=dataset_path(dataset) / "freeze.json",
        output_root=data_path(dataset) / "runs",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=["v2", "v3"], default="v2")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="holdout")
    parser.add_argument("--workflows", nargs=2, default=list(WORKFLOWS))
    parser.add_argument("--data", type=Path)
    parser.add_argument("--download", action="store_true")
    arm_group = parser.add_mutually_exclusive_group()
    arm_group.add_argument("--arms", action="store_true", help="D029 default-vs-candidate arms")
    arm_group.add_argument("--arms-preset", choices=sorted(ARM_PRESETS),
                           help="named arms; v7-vs-v8 shares one candidate snapshot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true")
    group.add_argument("--verify-corpus", action="store_true")
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--execute", type=Path)
    args = parser.parse_args()
    arms = args.arms_preset or args.arms
    if args.verify_corpus:
        result = verify_corpus(args.split, args.data, args.dataset)
    elif args.prepare:
        result = prepare(args.split, args.workflows, args.data, args.download, arms, args.dataset)
    elif args.execute:
        result = execute(args.execute, args.dataset)
    else:
        result = plan(args.split, args.workflows, arms, args.dataset)
    print(json.dumps(result, ensure_ascii=False, indent=2))
