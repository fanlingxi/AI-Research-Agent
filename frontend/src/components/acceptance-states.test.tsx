import { emptyIngestionStatistics, historyPage } from "../test/historyFixtures";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  ApiError,
  api,
  type AgentRun,
  type AgentTrace,
  type KnowledgeHealth,
  type KnowledgeIngestion,
  type MemoryProposal,
  type Project,
  type ResearchCommand,
  type ResearchReport,
  type RuntimeOverview,
  type RuntimeWorkItem,
  type WorkspaceTask,
} from "../lib/api";
import { AgentRunPage } from "../pages/AgentRunPage";
import { DashboardPage } from "../pages/DashboardPage";
import { KnowledgePage } from "../pages/KnowledgePage";
import { ProjectWorkspacePage } from "../pages/ProjectWorkspacePage";
import { QuickReportPage } from "../pages/QuickReportPage";
import { ReportsPage } from "../pages/ReportsPage";
import { ReviewPage } from "../pages/ReviewPage";
import { RuntimePage } from "../pages/RuntimePage";
import { ErrorBlock } from "./AsyncState";
import { CandidateReviewPanel } from "./CandidateReviewPanel";

function renderWithQuery(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const project: Project = {
  id: "project-1", name: "Project", goal: "Goal", domain: "test", status: "active", metadata: {}, revision: 1,
  created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00",
};

const committedProposal: MemoryProposal = {
  id: "proposal-1", project_id: project.id, task_id: null, proposal_type: "decision_create", payload: { proposal_type: "decision_create" },
  rationale: "Reviewed", status: "committed", review_note: null, committed_record_type: "decision", committed_record_id: "decision-1", revision: 3,
  created_at: "2026-01-01T00:00:00+00:00", reviewed_at: "2026-01-01T00:00:00+00:00", committed_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00",
};

const failedRun: AgentRun = {
  id: "run-1", project_id: project.id, task_id: "task-1", context_snapshot_id: "snapshot-1", context_sha256: "hash", workflow_name: "research_agent", workflow_version: "phase3b-v1", status: "failed", model_provider: "mock", model_name: "mock", max_steps: 10, max_tool_calls: 1, token_budget: 6000, tool_call_count: 0, repair_count: 0, current_node: "task_analysis", error_code: "runtime_execution_failed", error_message: "Research LLM returned invalid TaskAnalysis JSON: implementation detail", started_at: null, completed_at: null, created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00", revision: 1,
};

const failedTrace: AgentTrace = {
  run: failedRun, events: [], tool_calls: [], next_event_sequence: null, token_usage: {}, total_latency_ms: 0,
};

const queuedReport: ResearchReport = {
  id: "report-queued",
  query: "如何提高研究智能体的可靠性？",
  topic_slugs: ["papers"],
  top_k: 8,
  report_depth: "standard",
  status: "queued",
  content: "",
  evidence: [],
  evaluation: null,
  run_metadata: {},
  error: null,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const healthyKnowledge: KnowledgeHealth = {
  status: "ok",
  services: { worker: { available: true, detail: "在线" } },
  knowledge: {
    schema_version: 18,
    live_llm_configured: true,
    llm_provider: "fixture",
    jobs: {},
    projections: {},
  },
};

const healthyRuntime: RuntimeOverview = {
  status: "ok",
  generated_at: "2026-01-01T00:00:00+00:00",
  services: {
    api: { available: true, configured: true, detail: "在线", endpoint: null },
    worker: { available: true, configured: true, detail: "在线", endpoint: null },
    llm: { available: true, configured: true, detail: "已配置", endpoint: null },
  },
  executors: [],
  work_counts: {},
  projection_backlog: { queued: 0, running: 0, failed: 0, completed: 0, oldest_queued_at: null },
};

const queuedRuntimeAgent: RuntimeWorkItem = {
  id: "job-agent",
  kind: "agent_run",
  resource_id: "run-queued",
  title: "研究任务",
  detail_route: "/agent-runs/run-queued",
  business_status: "queued",
  job_status: "queued",
  current_stage: "queued",
  attempt: 0,
  priority: 100,
  queue_position: 2,
  lease_until: null,
  executor: null,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
  last_error: null,
  can_retry: false,
  can_cancel: true,
};

const failedIngestion: KnowledgeIngestion = {
  id: "ingestion-failed",
  topic: "论文",
  topic_slug: "papers",
  collection: "论文",
  collection_slug: "papers",
  sources: ["paper.pdf"],
  pdf_max_pages: 150,
  status: "failed",
  document_count: 0,
  candidate_count: 0,
  published_count: 0,
  queue_position: null,
  job_attempts: 1,
  error: "解析未完成",
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const workspaceTasks: WorkspaceTask[] = [
  {
    id: "task-1", project_id: project.id, title: "First task", goal: "First goal",
    status: "ready", priority: "normal", metadata: {}, revision: 1,
    created_at: project.created_at, updated_at: project.updated_at,
  },
  {
    id: "task-2", project_id: project.id, title: "Second task", goal: "Second goal",
    status: "ready", priority: "high", metadata: {}, revision: 1,
    created_at: project.created_at, updated_at: project.updated_at,
  },
];

const emptyDashboard = {
  active_projects: [],
  recent_workspace_tasks: [],
  recent_agent_runs: [],
  pending_memory_proposals: [],
  recent_artifacts: [],
};

const acceptedCommand: ResearchCommand = {
  id: "research-command-1",
  mode: "quick_report",
  status: "accepted",
  instruction: "研究可靠性",
  orchestration_stage: "accepted",
  project_id: null,
  task_id: null,
  snapshot_id: null,
  target_resource_type: null,
  target_resource_id: null,
  target_route: null,
  error: null,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("browser acceptance states", () => {
  beforeEach(() => { vi.spyOn(api, "ingestionStatistics").mockResolvedValue(emptyIngestionStatistics); });

  it("loads more quick reports while preserving the full report count", async () => {
    const pages = vi.spyOn(api, "reportPage")
      .mockResolvedValueOnce(historyPage([queuedReport], 26))
      .mockResolvedValueOnce(historyPage([{ ...queuedReport, id: "old-report", query: "旧报告" }], 26, 25));
    vi.spyOn(api, "projects").mockResolvedValue([]);
    renderWithQuery(<MemoryRouter><ReportsPage /></MemoryRouter>);
    await userEvent.click(await screen.findByRole("button", { name: "加载更多报告" }));
    expect(await screen.findByRole("link", { name: /旧报告/ })).toHaveAttribute("href", "/reports/old-report");
    expect(pages).toHaveBeenLastCalledWith(25);
    expect(screen.getByRole("button", { name: /快速报告.*26/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "加载更多报告" })).not.toBeInTheDocument();
  });

  it("loads older ingestion records without changing history-wide statistics", async () => {
    const old = { ...failedIngestion, id: "old-batch", collection: "旧资料" };
    const pages = vi.spyOn(api, "ingestionPage")
      .mockResolvedValueOnce(historyPage([failedIngestion], 26))
      .mockResolvedValueOnce(historyPage([old], 26, 25));
    vi.spyOn(api, "ingestionStatistics").mockResolvedValue({ ...emptyIngestionStatistics, total: 26, document_count: 125, published_count: 142, pending_count: 137 });
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeDocuments").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    renderWithQuery(<MemoryRouter><KnowledgePage /></MemoryRouter>);
    await userEvent.click(await screen.findByRole("button", { name: "加载更多导入记录" }));
    expect(await screen.findByText("旧资料")).toBeInTheDocument();
    expect(pages).toHaveBeenLastCalledWith(25);
    expect(screen.getByText("125")).toBeInTheDocument();
    expect(screen.getByText("142")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "137" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "加载更多导入记录" })).not.toBeInTheDocument();
  });

  it("redacts unavailable-service implementation details", () => {
    render(<ErrorBlock error={new ApiError("Couldn't connect to 127.0.0.1:9", 503)} />);

    expect(screen.getByText("相关检索服务当前不可用，请稍后重试。")).toBeInTheDocument();
    expect(screen.queryByText(/127\.0\.0\.1/)).not.toBeInTheDocument();
  });

  it("does not show a candidate loading state when no ingestion is selected", async () => {
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([]));

    renderWithQuery(<MemoryRouter><CandidateReviewPanel /></MemoryRouter>);

    expect(await screen.findByText("还没有可审核的资料")).toBeInTheDocument();
    expect(screen.queryByText("读取候选…")).not.toBeInTheDocument();
  });

  it("keeps committed Proposal deep links reviewable", async () => {
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "proposals").mockResolvedValue([committedProposal]);
    vi.spyOn(api, "proposal").mockResolvedValue(committedProposal);
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([]));

    renderWithQuery(<MemoryRouter initialEntries={["/review?proposal=proposal-1"]}><Routes><Route path="/review" element={<ReviewPage />} /></Routes></MemoryRouter>);

    expect(await screen.findByRole("dialog", { name: "审核记忆提案" })).toBeInTheDocument();
    expect(screen.getByText("已提交")).toBeInTheDocument();
  });

  it("shows a terminal failed Run without an output loading spinner or raw error", async () => {
    vi.spyOn(api, "feedback").mockResolvedValue({ feedback: [], links: [] });
    vi.spyOn(api, "run").mockResolvedValue(failedRun);
    vi.spyOn(api, "trace").mockResolvedValue(failedTrace);

    renderWithQuery(<MemoryRouter initialEntries={["/agent-runs/run-1"]}><Routes><Route path="/agent-runs/:runId" element={<AgentRunPage />} /></Routes></MemoryRouter>);

    expect(await screen.findByText("此运行未完成，因此没有可展示的输出。")).toBeInTheDocument();
    expect(screen.queryByText("读取输出摘要…")).not.toBeInTheDocument();
    expect(screen.queryByText(/invalid TaskAnalysis JSON/)).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-trace-grid")).toHaveClass("min-w-0");
  });

  it("offers only safe actions for a needs_review AgentRun", async () => {
    vi.spyOn(api, "feedback").mockResolvedValue({ feedback: [], links: [] });
    const user = userEvent.setup();
    const needsReview = {
      ...failedRun,
      status: "needs_review",
      error_code: "citation_validation_failed",
      error_message: "evidence-7 does not support claim-2",
    };
    vi.spyOn(api, "run").mockResolvedValue(needsReview);
    vi.spyOn(api, "trace").mockResolvedValue({ ...failedTrace, run: needsReview });
    const review = vi.spyOn(api, "reviewRun").mockResolvedValue({
      ...needsReview,
      id: "run-replacement",
      status: "queued",
    });

    renderWithQuery(<MemoryRouter initialEntries={["/agent-runs/run-1"]}><Routes><Route path="/agent-runs/:runId" element={<AgentRunPage />} /></Routes></MemoryRouter>);

    expect(await screen.findByText("evidence-7 does not support claim-2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "用新快照重新运行" }));
    expect(review).toHaveBeenCalledWith("run-1", "rerun");
    expect(screen.queryByRole("button", { name: /绕过|接受无效/ })).not.toBeInTheDocument();
  });

  it("explains exhausted run capacity with a path to a new revision", async () => {
    const exhausted = { ...failedRun, error_message: "AgentRun has exhausted its token budget." };
    vi.spyOn(api, "run").mockResolvedValue(exhausted);
    vi.spyOn(api, "trace").mockResolvedValue({ ...failedTrace, run: exhausted });
    vi.spyOn(api, "feedback").mockResolvedValue({ feedback: [], links: [] });
    renderWithQuery(<MemoryRouter initialEntries={["/agent-runs/run-1"]}><Routes><Route path="/agent-runs/:runId" element={<AgentRunPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("本次运行的总 Token 上限已耗尽。可登记反馈，在修订运行时提高生成用量上限。")).toBeInTheDocument();
  });

  it("starts one queued report directly from the report page", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "report").mockResolvedValue(queuedReport);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const execute = vi.spyOn(api, "executeReport").mockResolvedValue({
      ...queuedReport,
      status: "running",
      run_metadata: { current_stage: "scheduled" },
    });

    renderWithQuery(<MemoryRouter initialEntries={["/reports/report-queued"]}><Routes><Route path="/reports/:reportId" element={<QuickReportPage />} /></Routes></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "立即执行" }));
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith(queuedReport.id);
  });

  it("retries a failed report in place", async () => {
    const user = userEvent.setup();
    const failedReport: ResearchReport = {
      ...queuedReport,
      id: "report-failed",
      status: "failed",
      error: "引用覆盖不足",
    };
    vi.spyOn(api, "report").mockResolvedValue(failedReport);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const retry = vi.spyOn(api, "retryReport").mockResolvedValue({
      ...failedReport,
      status: "running",
      error: null,
    });

    renderWithQuery(<MemoryRouter initialEntries={["/reports/report-failed"]}><Routes><Route path="/reports/:reportId" element={<QuickReportPage />} /></Routes></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "清理失败结果并重新执行" }));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(retry).toHaveBeenCalledWith(failedReport.id);
  });

  it("lists quick reports and project reports together in the library", async () => {
    vi.spyOn(api, "reportPage").mockResolvedValue(historyPage([{ ...queuedReport, status: "completed" }]));
    vi.spyOn(api, "projectRuns").mockResolvedValue({ items: [], next_cursor: null });
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "tasks").mockResolvedValue(workspaceTasks);
    vi.spyOn(api, "artifacts").mockResolvedValue([{
      id: "artifact-1", project_id: project.id, task_id: "task-1", type: "research_report", reference: "ref", version: 1,
      status: "ready", supersedes_artifact_id: null, metadata: {}, revision: 1, created_at: project.created_at, updated_at: project.updated_at,
    }]);

    renderWithQuery(<MemoryRouter><ReportsPage /></MemoryRouter>);

    expect(await screen.findByRole("link", { name: /如何提高研究智能体的可靠性/ })).toHaveAttribute("href", "/reports/report-queued");
    expect(await screen.findByRole("link", { name: /First task/ })).toHaveAttribute("href", "/artifacts/artifact-1");
  });

  it("redirects legacy report links to the reader and opens a cited passage", async () => {
    vi.spyOn(api, "report").mockResolvedValue({
      ...queuedReport,
      status: "completed",
      content: "## 结论\n\n可靠性来自证据 [E1]。",
      evidence: [{ id: "E1", paper_id: "p1", chunk_id: "c1", title: "论文甲", text: "原文段落", page_start: 3, page_end: 3, score: 0.9 }],
    });
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    const documentSource = vi.spyOn(api, "documentSource").mockResolvedValue({
      document_id: "p1", title: "论文甲", local_path: "D:\\papers\\a.pdf", source_url: null, pages: 9,
    });
    const user = userEvent.setup();

    renderWithQuery(<MemoryRouter initialEntries={["/reports?report=report-queued"]}><Routes><Route path="/reports" element={<ReportsPage />} /><Route path="/reports/:reportId" element={<QuickReportPage />} /></Routes></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "查看引用 1 的原文" }));
    expect(await screen.findByText("原文段落")).toBeInTheDocument();
    expect(screen.getAllByText("第 3 页").length).toBeGreaterThan(0);
    const pdfLink = await screen.findByRole("link", { name: /打开原文 PDF（第 3 页）/ });
    expect(pdfLink).toHaveAttribute("href", `/api/knowledge/source-file?uri=${encodeURIComponent("D:\\papers\\a.pdf")}#page=3`);
    expect(documentSource).toHaveBeenCalledWith("p1");
  });

  it("offers an in-place recovery action when the unified worker is offline", async () => {
    const user = userEvent.setup();
    const awaitingReport: ResearchReport = {
      ...queuedReport,
      run_metadata: { current_stage: "queued_for_dispatch" },
    };
    vi.spyOn(api, "report").mockResolvedValue(awaitingReport);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue({
      ...healthyKnowledge,
      services: { worker: { available: false, detail: "离线" } },
    });
    const execute = vi.spyOn(api, "executeReport").mockResolvedValue({
      ...awaitingReport,
      status: "running",
      run_metadata: { current_stage: "scheduled" },
    });

    renderWithQuery(<MemoryRouter initialEntries={["/reports/report-queued"]}><Routes><Route path="/reports/:reportId" element={<QuickReportPage />} /></Routes></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "重新启动报告执行器" }));
    expect(execute).toHaveBeenCalledWith(awaitingReport.id);
  });

  it("places evidence scope before submit in the quick research flow", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "dashboard").mockResolvedValue(emptyDashboard);
    vi.spyOn(api, "collections").mockResolvedValue([
      { slug: "papers", name: "论文", is_system: false, ingestion_count: 1, updated_at: null },
    ]);
    const submit = vi.spyOn(api, "submitResearchCommand").mockResolvedValue(acceptedCommand);
    vi.spyOn(api, "researchCommand").mockResolvedValue(acceptedCommand);

    renderWithQuery(<MemoryRouter><DashboardPage /></MemoryRouter>);

    const input = await screen.findByRole("textbox", { name: "研究指令" });
    const scope = await screen.findByRole("checkbox", { name: /论文/ });
    const button = screen.getByRole("button", { name: "提交并开始研究" });
    expect(input.compareDocumentPosition(scope) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(scope.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.type(input, "研究可靠性");
    await user.click(scope);
    await user.click(button);

    expect(submit).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: "quick_report",
        instruction: "研究可靠性",
        collection_slugs: ["papers"],
      }),
      expect.any(String),
    );
  });

  it("blocks quick research when collection scope cannot be loaded", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "dashboard").mockResolvedValue(emptyDashboard);
    vi.spyOn(api, "collections").mockRejectedValue(new Error("集合读取失败"));
    const submit = vi.spyOn(api, "submitResearchCommand");

    renderWithQuery(<MemoryRouter><DashboardPage /></MemoryRouter>);

    await screen.findByText("集合读取失败");
    const button = screen.getByRole("button", { name: "提交并开始研究" });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(submit).not.toHaveBeenCalled();
  });

  it("requires an explicit Project before submitting project research", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "dashboard").mockResolvedValue(emptyDashboard);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "tasks").mockResolvedValue([]);
    const submit = vi.spyOn(api, "submitResearchCommand").mockResolvedValue({
      ...acceptedCommand,
      mode: "project_run",
      project_id: project.id,
      instruction: "研究项目运行",
    });
    vi.spyOn(api, "researchCommand").mockResolvedValue(acceptedCommand);

    renderWithQuery(<MemoryRouter><DashboardPage /></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "项目研究" }));
    const instruction = screen.getByRole("textbox", { name: "研究指令" });
    const projectSelect = await screen.findByRole("combobox", { name: "项目" });
    const submitButton = screen.getByRole("button", { name: "提交并开始研究" });
    await user.type(instruction, "研究项目运行");
    expect(submitButton).toBeDisabled();
    await user.selectOptions(projectSelect, project.id);
    await waitFor(() => expect(submitButton).toBeEnabled());
    await user.click(submitButton);

    await waitFor(() => {
      expect(submit).toHaveBeenCalledWith(
        expect.objectContaining({
          mode: "project_run",
          project_id: project.id,
          task: expect.objectContaining({ kind: "new" }),
        }),
        expect.any(String),
      );
    });
  });

  it("restores a persisted failed command and retries from the Dashboard", async () => {
    const user = userEvent.setup();
    const failedCommand: ResearchCommand = {
      ...acceptedCommand,
      status: "failed",
      orchestration_stage: "snapshot_ready",
      error: "Agent 执行器暂时不可用",
    };
    window.localStorage.setItem("research-workspace.pending-command", JSON.stringify({
      fingerprint: "saved",
      key: "saved-command-key",
      commandId: failedCommand.id,
    }));
    vi.spyOn(api, "dashboard").mockResolvedValue(emptyDashboard);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "researchCommand").mockResolvedValue(failedCommand);
    const retry = vi.spyOn(api, "retryResearchCommand").mockResolvedValue({
      ...failedCommand,
      status: "accepted",
      error: null,
    });

    renderWithQuery(<MemoryRouter><DashboardPage /></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "从失败阶段恢复" }));
    expect(retry).toHaveBeenCalledWith(failedCommand.id);
  });

  it("opens on knowledge candidates and keeps memory proposals one tab away", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "proposals").mockResolvedValue([{ ...committedProposal, status: "proposed", revision: 1, payload: { summary: "采用混合检索" } }]);
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([]));

    renderWithQuery(<MemoryRouter><ReviewPage /></MemoryRouter>);

    expect(await screen.findByText("还没有可审核的资料")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /记忆提案/ }));
    await user.click(await screen.findByRole("button", { name: /采用混合检索/ }));
    expect(await screen.findByRole("dialog", { name: "审核记忆提案" })).toBeInTheDocument();
  });

  it("hides system collections from Project knowledge scopes", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "project").mockResolvedValue(project);
    vi.spyOn(api, "tasks").mockResolvedValue([]);
    vi.spyOn(api, "decisions").mockResolvedValue([]);
    vi.spyOn(api, "artifacts").mockResolvedValue([]);
    vi.spyOn(api, "scopes").mockResolvedValue([{ project_id: project.id, collection_slug: "inbox", created_at: project.created_at }]);
    vi.spyOn(api, "projectRuns").mockResolvedValue({ items: [], next_cursor: null });
    vi.spyOn(api, "proposals").mockResolvedValue([]);
    vi.spyOn(api, "collections").mockResolvedValue([
      { slug: "inbox", name: "收件箱", is_system: true, ingestion_count: 0, updated_at: null },
      { slug: "papers", name: "论文库", is_system: false, ingestion_count: 2, updated_at: null },
    ]);
    const replace = vi.spyOn(api, "replaceScopes").mockResolvedValue([
      { project_id: project.id, collection_slug: "papers", created_at: project.created_at },
    ]);

    renderWithQuery(<MemoryRouter initialEntries={["/projects/project-1/overview"]}><Routes><Route path="/projects/:projectId/:section" element={<ProjectWorkspacePage />} /></Routes></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "管理知识范围" }));
    expect(await screen.findByRole("checkbox", { name: /论文库/ })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /收件箱/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /论文库/ }));
    await user.click(screen.getByRole("button", { name: "保存范围" }));
    expect(replace).toHaveBeenCalledWith(project.id, project.revision, ["papers"]);
  });

  it("requires an explicit Task selection on the Project Agent panel", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "project").mockResolvedValue(project);
    vi.spyOn(api, "tasks").mockResolvedValue(workspaceTasks);
    vi.spyOn(api, "decisions").mockResolvedValue([]);
    vi.spyOn(api, "artifacts").mockResolvedValue([]);
    vi.spyOn(api, "scopes").mockResolvedValue([]);
    vi.spyOn(api, "projectRuns").mockResolvedValue({ items: [], next_cursor: null });
    vi.spyOn(api, "proposals").mockResolvedValue([]);
    const taskRuns = vi.spyOn(api, "taskRuns").mockResolvedValue({ items: [], next_cursor: null });

    renderWithQuery(<MemoryRouter initialEntries={["/projects/project-1/overview"]}><Routes><Route path="/projects/:projectId/:section" element={<ProjectWorkspacePage />} /></Routes></MemoryRouter>);

    const selector = await screen.findByRole("combobox", { name: "选择任务" });
    expect(selector).toHaveValue("");
    expect(screen.getByText(/选择一个任务后，才能准备上下文并开始研究/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始研究" })).toBeDisabled();
    await user.selectOptions(selector, "task-2");
    expect(await screen.findByText("当前任务")).toBeInTheDocument();
    expect(taskRuns).toHaveBeenCalledWith(project.id, "task-2");
  });

  it("bounds long Knowledge and Runtime lists inside their cards", async () => {
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    vi.spyOn(api, "runtimeOverview").mockResolvedValue(healthyRuntime);
    vi.spyOn(api, "runtimeWork").mockResolvedValue({ items: [], next_cursor: null });

    const knowledge = renderWithQuery(<MemoryRouter><KnowledgePage /></MemoryRouter>);
    expect(await screen.findByTestId("ingestion-history-list")).toHaveClass("overflow-y-auto");
    knowledge.unmount();


    renderWithQuery(<MemoryRouter><RuntimePage /></MemoryRouter>);
    expect(await screen.findByTestId("runtime-work-list")).toHaveClass("overflow-y-auto");
  });

  it("explains why AgentRun is queued when the unified worker is offline", async () => {
    vi.spyOn(api, "runtimeOverview").mockResolvedValue({
      ...healthyRuntime,
      services: {
        ...healthyRuntime.services,
        worker: { available: false, configured: true, detail: "离线", endpoint: null },
      },
      work_counts: { agent_run: { queued: 1 } },
    });
    vi.spyOn(api, "runtimeWork").mockResolvedValue({ items: [queuedRuntimeAgent], next_cursor: null });

    renderWithQuery(<MemoryRouter><RuntimePage /></MemoryRouter>);

    expect(await screen.findByText(/1 个任务在排队，但 Worker 离线/)).toBeInTheDocument();
    expect(screen.getByText("python -m app.worker")).toBeInTheDocument();
    expect(screen.getByText("队列第 2 位")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "研究任务" })).toHaveAttribute("href", "/agent-runs/run-queued");
  });

  it("retries a failed projection from the Runtime page", async () => {
    const failedProjection: RuntimeWorkItem = {
      ...queuedRuntimeAgent,
      id: "projection-failed",
      kind: "projection",
      resource_id: "entity-1",
      title: "知识实体投影",
      detail_route: "/runtime?projection=projection-failed",
      business_status: "failed",
      job_status: "failed",
      current_stage: "entity_projection",
      queue_position: null,
      last_error: "Neo4j 端点缺失",
      can_retry: true,
      can_cancel: false,
    };
    vi.spyOn(api, "runtimeOverview").mockResolvedValue({
      ...healthyRuntime,
      projection_backlog: { queued: 0, running: 0, failed: 1, completed: 0, oldest_queued_at: null },
    });
    vi.spyOn(api, "runtimeWork").mockResolvedValue({ items: [failedProjection], next_cursor: null });
    const retry = vi.spyOn(api, "retryProjection").mockResolvedValue({
      ...failedProjection,
      job_status: "queued",
      can_retry: false,
    });
    const user = userEvent.setup();

    renderWithQuery(<MemoryRouter><RuntimePage /></MemoryRouter>);
    await user.click(await screen.findByRole("button", { name: "安全重试" }));

    expect(retry).toHaveBeenCalledWith("projection-failed");
  });

  it("submits and starts a knowledge ingestion in one action", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const submit = vi.spyOn(api, "submitAndExecuteIngestion").mockResolvedValue({
      ...failedIngestion,
      status: "queued",
      error: null,
    });

    renderWithQuery(<MemoryRouter><KnowledgePage /></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "导入资料" }));
    await user.type(await screen.findByRole("textbox", { name: /PDF 路径或 URL/ }), "paper.pdf");
    await user.click(screen.getByRole("button", { name: "提交并开始入库" }));
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      sources: ["paper.pdf"],
      pdf_max_pages: 20,
    }));
  });

  it("retries a failed knowledge ingestion in place", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([failedIngestion]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const retry = vi.spyOn(api, "retryIngestion").mockResolvedValue({
      ...failedIngestion,
      status: "queued",
      error: null,
    });

    renderWithQuery(<MemoryRouter><KnowledgePage /></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "重新执行" }));
    expect(retry).toHaveBeenCalledWith(failedIngestion.id);
  });

  it("moves a completed inbox ingestion into a formal Collection", async () => {
    const user = userEvent.setup();
    const inbox = {
      ...failedIngestion,
      id: "ingestion-inbox",
      topic: "收件箱",
      topic_slug: "inbox",
      collection: "收件箱",
      collection_slug: "inbox",
      status: "completed",
      error: null,
    };
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([inbox]));
    vi.spyOn(api, "collections").mockResolvedValue([
      { slug: "inbox", name: "收件箱", is_system: true, ingestion_count: 1, updated_at: null },
      { slug: "papers", name: "论文库", is_system: false, ingestion_count: 1, updated_at: null },
    ]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const move = vi.spyOn(api, "moveIngestionCollection").mockResolvedValue({
      ...inbox,
      topic: "论文库",
      topic_slug: "papers",
      collection: "论文库",
      collection_slug: "papers",
    });

    renderWithQuery(<MemoryRouter><KnowledgePage /></MemoryRouter>);

    await user.selectOptions(
      await screen.findByRole("combobox", { name: "移动 收件箱 到正式知识集合" }),
      "papers",
    );
    await user.click(screen.getByRole("button", { name: "移动" }));
    expect(move).toHaveBeenCalledWith("ingestion-inbox", "论文库");
  });
});
