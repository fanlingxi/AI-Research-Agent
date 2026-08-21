from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.agent.service import AgentRunService
from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan, ReportEvidence
from app.knowledge.service import KnowledgeIngestionService
from app.research_commands.models import ResearchCommandSubmission
from app.research_commands.service import ResearchCommandService
from app.worker import KnowledgeWorker
from tests.core_fixtures import persist_evidence_chunk


class _LiveFixtureLLM:
    provider_name = "openai"

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        return (
            "# 研究报告\n\n## 背景\n正式证据支持可追溯研究。[E1]\n\n"
            "## 结论\n引用可以回溯到原始文献。[E1]"
        )


class _Query:
    def search(self, query, *, topic_slugs=None, top_k=8):
        return {
            "query": query,
            "topic_slugs": topic_slugs or [],
            "graph": [],
            "evidence": [
                ReportEvidence(
                    id="E1",
                    paper_id="paper:command",
                    chunk_id="paper:command:page:1:chunk:0",
                    title="Research Command Paper",
                    text="A durable idempotency key maps one instruction to one target.",
                    page_start=1,
                    page_end=1,
                    score=0.98,
                ).model_dump()
            ],
        }


def _stack(tmp_path, *, agent_llm=None):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
        agent_checkpoint_path=str(tmp_path / "checkpoint.db"),
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    ingestion = repository.create_ingestion(
        topic="Command Papers", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="paper:command",
        chunk_id="paper:command:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="A durable idempotency key maps one instruction to one target.",
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    candidate = CandidateEntity(
        id="candidate-command-paper",
        ingestion_id=ingestion.id,
        topic_slug=ingestion.topic_slug,
        name="Research Command Paper",
        type="Paper",
        summary="一篇用于验证幂等研究指令编排的正式证据文献。",
        confidence=0.98,
        evidence=evidence,
    )
    repository.add_candidate_entity(candidate)
    repository.publish_entity(candidate.id)
    llm = agent_llm if agent_llm is not None else _LiveFixtureLLM()
    agents = AgentRunService(repository, settings=settings, llm=llm)
    reports = KnowledgeReportService(
        repository,
        settings=settings,
        query_service=_Query(),
        llm=_LiveFixtureLLM(),
        require_live_llm=True,
    )
    commands = ResearchCommandService(
        repository,
        report_service=reports,
        agent_service=agents,
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=KnowledgeIngestionService(repository, settings=settings),
        report_service=reports,
        research_command_service=commands,
        worker_id="command-worker",
        lease_seconds=30,
    )
    app = create_app(
        knowledge_repository=repository,
        report_service=reports,
        agent_run_service=agents,
        research_command_service=commands,
    )
    return repository, agents, commands, worker, app


def _table_count(repository, table: str) -> int:
    with repository._connect() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_quick_command_is_idempotent_and_worker_creates_exactly_one_report(tmp_path) -> None:
    repository, _, _, worker, app = _stack(tmp_path)
    payload = {
        "instruction": "研究指令如何避免重复报告？",
        "collection_slugs": ["command-papers"],
        "top_k": 8,
        "report_depth": "standard",
    }
    headers = {"Idempotency-Key": "quick-command-key-001"}
    with TestClient(app) as client:
        first = client.post("/api/v1/research-commands", json=payload, headers=headers)
        replay = client.post("/api/v1/research-commands", json=payload, headers=headers)
        conflict = client.post(
            "/api/v1/research-commands",
            json={**payload, "instruction": "另一个问题"},
            headers=headers,
        )

        assert first.status_code == replay.status_code == 202
        assert first.json()["id"] == replay.json()["id"]
        assert first.json()["mode"] == "quick_report"
        assert conflict.status_code == 409
        assert _table_count(repository, "reports") == 0

        assert worker.run_once()
        orchestrated = client.get(f"/api/v1/research-commands/{first.json()['id']}")
        again = client.post("/api/v1/research-commands", json=payload, headers=headers)

    assert orchestrated.json()["status"] == "queued"
    assert orchestrated.json()["target_resource_type"] == "report"
    assert orchestrated.json()["target_route"].startswith("/reports?report=")
    assert again.json()["target_resource_id"] == orchestrated.json()["target_resource_id"]
    assert _table_count(repository, "research_commands") == 1
    assert _table_count(repository, "reports") == 1


def test_concurrent_same_key_creates_one_command_and_one_job(tmp_path) -> None:
    repository, _, commands, _, _ = _stack(tmp_path)
    submission = ResearchCommandSubmission.model_validate(
        {
            "instruction": "并发双击只能创建一个研究任务",
            "collection_slugs": ["command-papers"],
        }
    )

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(
            pool.map(
                lambda _: (
                    commands.submit(
                        submission,
                        idempotency_key="concurrent-command-key-001",
                    ).id
                ),
                range(12),
            )
        )

    assert len(set(ids)) == 1
    assert _table_count(repository, "research_commands") == 1
    with repository._connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_jobs WHERE kind = 'research_command'"
            ).fetchone()[0]
            == 1
        )


def test_worker_persists_command_terminal_state_when_target_finishes(tmp_path) -> None:
    repository, _, commands, worker, _ = _stack(tmp_path)
    command = commands.submit(
        ResearchCommandSubmission.model_validate(
            {
                "instruction": "报告完成后同步持久化指令状态",
                "collection_slugs": ["command-papers"],
            }
        ),
        idempotency_key="terminal-command-key-001",
    )

    assert worker.run_once()  # orchestrates the command
    assert worker.run_once()  # executes the report

    persisted = commands.repository.get(command.id)
    assert persisted.status == "completed"
    assert persisted.orchestration_stage == "completed"


def test_project_command_creates_task_snapshot_and_agent_run_once(tmp_path) -> None:
    repository, _, _, worker, app = _stack(tmp_path)
    project = repository.memory_repository.create_project(
        name="Command Project", goal="", domain="research", metadata={}
    )
    instruction = "评估项目研究 Agent 的证据约束、恢复机制与当前局限"
    payload = {
        "mode": "project_run",
        "instruction": instruction,
        "project_id": project.id,
        "task": {"kind": "new", "title": "", "priority": "high"},
        "create_memory_proposal": True,
        "max_steps": 10,
        "max_tool_calls": 3,
        "token_budget": 6000,
    }
    headers = {"Idempotency-Key": "project-command-key-001"}
    with TestClient(app) as client:
        created = client.post("/api/v1/research-commands", json=payload, headers=headers)
        assert created.status_code == 202
        assert worker.run_once()
        command = client.get(f"/api/v1/research-commands/{created.json()['id']}").json()
        replay = client.post("/api/v1/research-commands", json=payload, headers=headers).json()

    assert command["status"] == "queued"
    assert command["target_resource_type"] == "agent_run"
    assert command["target_route"].startswith("/agent-runs/")
    assert command["task_id"] and command["snapshot_id"]
    assert replay["target_resource_id"] == command["target_resource_id"]
    task_title = repository.memory_repository.get_workspace_task(command["task_id"]).title
    assert task_title == instruction[:60]
    assert _table_count(repository, "workspace_tasks") == 1
    assert _table_count(repository, "context_snapshots") == 1
    assert _table_count(repository, "agent_runs") == 1


def test_project_command_uses_the_explicit_existing_task(tmp_path) -> None:
    repository, _, _, worker, app = _stack(tmp_path)
    project = repository.memory_repository.create_project(
        name="Existing Task Project", goal="", domain="research", metadata={}
    )
    task = repository.memory_repository.create_workspace_task(
        project_id=project.id,
        title="用户明确选择的 Task",
        goal="",
        priority="normal",
        metadata={},
    )
    payload = {
        "mode": "project_run",
        "instruction": "只在用户明确选择的任务上运行",
        "project_id": project.id,
        "task": {"kind": "existing", "task_id": task.id},
    }
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/research-commands",
            json=payload,
            headers={"Idempotency-Key": "existing-task-command-key-001"},
        )
        assert created.status_code == 202
        assert worker.run_once()
        command = client.get(f"/api/v1/research-commands/{created.json()['id']}").json()

    assert command["task_id"] == task.id
    assert _table_count(repository, "workspace_tasks") == 1


def test_failed_project_orchestration_preserves_resources_and_resumes_same_ids(tmp_path) -> None:
    repository, agents, commands, worker, app = _stack(tmp_path, agent_llm=None)
    # Force the Research plugin's live-LLM gate to fail only after Task and Snapshot creation.
    from app.llms.provider import MockLLMClient

    agents.llm = MockLLMClient()
    project = repository.memory_repository.create_project(
        name="Recoverable Command", goal="", domain="research", metadata={}
    )
    payload = {
        "mode": "project_run",
        "instruction": "中断后继续同一个项目研究指令",
        "project_id": project.id,
        "task": {"kind": "new", "priority": "normal"},
    }
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/research-commands",
            json=payload,
            headers={"Idempotency-Key": "recover-command-key-001"},
        ).json()
        assert worker.run_once()
        failed = client.get(f"/api/v1/research-commands/{created['id']}").json()
        assert failed["status"] == "failed"
        assert failed["task_id"] and failed["snapshot_id"]
        task_id, snapshot_id = failed["task_id"], failed["snapshot_id"]

        agents.llm = _LiveFixtureLLM()
        retried = client.post(f"/api/v1/research-commands/{created['id']}/retry")
        assert retried.status_code == 202
        assert worker.run_once()
        recovered = client.get(f"/api/v1/research-commands/{created['id']}").json()

    assert recovered["status"] == "queued"
    assert recovered["task_id"] == task_id
    assert recovered["snapshot_id"] == snapshot_id
    assert _table_count(repository, "workspace_tasks") == 1
    assert _table_count(repository, "context_snapshots") == 1
    assert _table_count(repository, "agent_runs") == 1
    assert commands.get(created["id"]).target_resource_id == recovered["target_resource_id"]
