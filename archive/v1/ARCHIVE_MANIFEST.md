# AI-Research-Agent v1 Archive Manifest

This directory is a frozen, runnable snapshot of the v1 research-report system.

- Source commit: `1fb7dc69ea40534250c60db37e88479196d8ccf4`
- Archive tag: `v1-archive-1fb7dc6`
- Archived on: 2026-08-04
- Tested Python: 3.12.13
- Original test baseline: 38 tests
- Status: frozen and unsupported

## Isolation contract

- Nothing under this directory may be imported by the active application.
- Root tests, lint, Docker images, and releases exclude `archive/`.
- v1 receives no feature, dependency, or security maintenance.
- Existing user data is not copied into or deleted by this archive.

## Known boundaries

- Offline search and the mock LLM are deterministic demonstrations, not formal research evidence.
- Qdrant, Neo4j, live arXiv search, provider APIs, and Obsidian export still depend on their original external configuration.
- The dependency lock reproduces the verified archive environment; it is not maintained for future Python, OS, security, or provider changes.
- The archive has no compatibility contract with the active Knowledge Core API, SQLite schema, worker, or report format.

## Run independently

```bash
cd archive/v1
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
pytest -q
python main.py run "GraphRAG for scientific literature review" --offline
```

API and UI:

```bash
uvicorn app.api.main:app --reload --port 8000
AI_RESEARCH_API_URL=http://localhost:8000 streamlit run app/ui/streamlit_app.py
```

The active Knowledge Core is documented in the repository root. Do not copy
new active code into this archive.
