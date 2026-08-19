import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
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
  type ResearchReport,
} from "../lib/api";
import { AgentRunPage } from "../pages/AgentRunPage";
import { DashboardPage } from "../pages/DashboardPage";
import { KnowledgePage } from "../pages/KnowledgePage";
import { ProjectWorkspacePage } from "../pages/ProjectWorkspacePage";
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
  services: { report_dispatcher: { available: true, detail: "在线" } },
  knowledge: {
    schema_version: 15,
    live_llm_configured: true,
    llm_provider: "fixture",
    jobs: {},
    projections: {},
  },
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

const emptyDashboard = {
  active_projects: [],
  recent_workspace_tasks: [],
  recent_agent_runs: [],
  pending_memory_proposals: [],
  recent_artifacts: [],
};

afterEach(() => vi.restoreAllMocks());

describe("browser acceptance states", () => {
  it("redacts unavailable-service implementation details", () => {
    render(<ErrorBlock error={new ApiError("Couldn't connect to 127.0.0.1:9", 503)} />);

    expect(screen.getByText("相关检索服务当前不可用，请稍后重试。")).toBeInTheDocument();
    expect(screen.queryByText(/127\.0\.0\.1/)).not.toBeInTheDocument();
  });

  it("does not show a candidate loading state when no ingestion is selected", async () => {
    vi.spyOn(api, "ingestions").mockResolvedValue([]);

    renderWithQuery(<CandidateReviewPanel />);

    expect(await screen.findByText("没有 Knowledge ingestion")).toBeInTheDocument();
    expect(screen.queryByText("读取 draft candidates…")).not.toBeInTheDocument();
  });

  it("keeps committed Proposal deep links reviewable", async () => {
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "proposals").mockResolvedValue([committedProposal]);
    vi.spyOn(api, "proposal").mockResolvedValue(committedProposal);
    vi.spyOn(api, "ingestions").mockResolvedValue([]);

    renderWithQuery(<MemoryRouter initialEntries={["/review?proposal=proposal-1"]}><Routes><Route path="/review" element={<ReviewPage />} /></Routes></MemoryRouter>);

    expect(await screen.findByRole("dialog", { name: "Review MemoryProposal" })).toBeInTheDocument();
    expect(screen.getByText("committed")).toBeInTheDocument();
  });

  it("shows a terminal failed Run without an output loading spinner or raw error", async () => {
    vi.spyOn(api, "run").mockResolvedValue(failedRun);
    vi.spyOn(api, "trace").mockResolvedValue(failedTrace);

    renderWithQuery(<MemoryRouter initialEntries={["/agent-runs/run-1"]}><Routes><Route path="/agent-runs/:runId" element={<AgentRunPage />} /></Routes></MemoryRouter>);

    expect(await screen.findByText("此运行未完成，因此没有可展示的输出。")).toBeInTheDocument();
    expect(screen.queryByText("读取输出摘要…")).not.toBeInTheDocument();
    expect(screen.queryByText(/invalid TaskAnalysis JSON/)).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-trace-grid")).toHaveClass("min-w-0");
  });

  it("starts one queued report directly from the report page", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "reports").mockResolvedValue([queuedReport]);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const execute = vi.spyOn(api, "executeReport").mockResolvedValue({
      ...queuedReport,
      status: "running",
      run_metadata: { current_stage: "scheduled" },
    });

    renderWithQuery(<MemoryRouter><ReportsPage /></MemoryRouter>);

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
    vi.spyOn(api, "reports").mockResolvedValue([failedReport]);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const retry = vi.spyOn(api, "retryReport").mockResolvedValue({
      ...failedReport,
      status: "running",
      error: null,
    });

    renderWithQuery(<MemoryRouter><ReportsPage /></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "清理失败结果并重新执行" }));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(retry).toHaveBeenCalledWith(failedReport.id);
  });

  it("submits a three-character research question and starts it immediately", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "reports").mockResolvedValue([]);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const submit = vi.spyOn(api, "submitAndExecuteReport").mockResolvedValue({
      ...queuedReport,
      status: "running",
    });

    renderWithQuery(<MemoryRouter><ReportsPage /></MemoryRouter>);
    const input = await screen.findByRole("textbox", { name: "研究问题" });
    expect(input).toHaveAttribute("minlength", "3");
    await user.type(input, "研究题");
    await user.click(screen.getByRole("button", { name: "提交并开始生成" }));

    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({ query: "研究题" }));
  });

  it("offers an in-place recovery action when the report dispatcher is offline", async () => {
    const user = userEvent.setup();
    const awaitingReport: ResearchReport = {
      ...queuedReport,
      run_metadata: { current_stage: "queued_for_dispatch" },
    };
    vi.spyOn(api, "reports").mockResolvedValue([awaitingReport]);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue({
      ...healthyKnowledge,
      services: { report_dispatcher: { available: false, detail: "离线" } },
    });
    const execute = vi.spyOn(api, "executeReport").mockResolvedValue({
      ...awaitingReport,
      status: "running",
      run_metadata: { current_stage: "scheduled" },
    });

    renderWithQuery(<MemoryRouter><ReportsPage /></MemoryRouter>);

    await user.click(await screen.findByRole("button", { name: "重新启动报告执行器" }));
    expect(execute).toHaveBeenCalledWith(awaitingReport.id);
  });

  it("places evidence scope before submit in the quick research flow", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "dashboard").mockResolvedValue(emptyDashboard);
    vi.spyOn(api, "collections").mockResolvedValue([
      { slug: "papers", name: "论文", is_system: false, ingestion_count: 1, updated_at: null },
    ]);
    const submit = vi.spyOn(api, "submitAndExecuteReport").mockResolvedValue(queuedReport);

    renderWithQuery(<MemoryRouter><DashboardPage /></MemoryRouter>);

    const input = await screen.findByRole("textbox", { name: "研究指令" });
    const scope = screen.getByRole("combobox", { name: /证据范围/ });
    const button = screen.getByRole("button", { name: "开始研究" });
    expect(input.compareDocumentPosition(scope) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(scope.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.type(input, "研究可靠性");
    await user.selectOptions(scope, "papers");
    await user.click(button);

    expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      query: "研究可靠性",
      collection_slugs: ["papers"],
    }));
  });

  it("blocks quick research when collection scope cannot be loaded", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "dashboard").mockResolvedValue(emptyDashboard);
    vi.spyOn(api, "collections").mockRejectedValue(new Error("集合读取失败"));
    const submit = vi.spyOn(api, "submitAndExecuteReport");

    renderWithQuery(<MemoryRouter><DashboardPage /></MemoryRouter>);

    await screen.findByText("集合读取失败");
    const button = screen.getByRole("button", { name: "开始研究" });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(submit).not.toHaveBeenCalled();
  });

  it("collapses an empty MemoryProposal queue so candidate review uses the workspace", async () => {
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "proposals").mockResolvedValue([]);
    vi.spyOn(api, "ingestions").mockResolvedValue([]);

    renderWithQuery(<MemoryRouter><ReviewPage /></MemoryRouter>);

    expect(await screen.findByTestId("memory-proposal-empty-banner")).toHaveTextContent("当前没有待审核的记忆提案");
    expect(screen.getByRole("heading", { name: "知识候选审核" })).toBeInTheDocument();
    expect(screen.queryByTestId("review-split-layout")).not.toBeInTheDocument();
  });

  it("uses a bounded proposal rail when proposals need review", async () => {
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "proposals").mockResolvedValue([{ ...committedProposal, status: "proposed", revision: 1 }]);
    vi.spyOn(api, "ingestions").mockResolvedValue([]);

    renderWithQuery(<MemoryRouter><ReviewPage /></MemoryRouter>);

    expect(await screen.findByTestId("review-split-layout")).toHaveClass("xl:grid-cols-[320px_minmax(0,1fr)]");
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

  it("bounds long Knowledge, report, and Runtime lists inside their cards", async () => {
    vi.spyOn(api, "ingestions").mockResolvedValue([]);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    vi.spyOn(api, "reports").mockResolvedValue([]);

    const knowledge = renderWithQuery(<KnowledgePage />);
    expect(await screen.findByTestId("ingestion-history-list")).toHaveClass("overflow-y-auto");
    knowledge.unmount();

    const reports = renderWithQuery(<MemoryRouter><ReportsPage /></MemoryRouter>);
    expect(await screen.findByTestId("report-history-list")).toHaveClass("overflow-y-auto");
    reports.unmount();

    renderWithQuery(<MemoryRouter><RuntimePage /></MemoryRouter>);
    expect(await screen.findByTestId("runtime-work-list")).toHaveClass("overflow-y-auto");
  });

  it("submits and starts a knowledge ingestion in one action", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "ingestions").mockResolvedValue([]);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const submit = vi.spyOn(api, "submitAndExecuteIngestion").mockResolvedValue({
      ...failedIngestion,
      status: "queued",
      error: null,
    });

    renderWithQuery(<KnowledgePage />);

    await user.type(await screen.findByRole("textbox", { name: /PDF 路径或 URL/ }), "paper.pdf");
    await user.click(screen.getByRole("button", { name: "提交并开始入库" }));
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      sources: ["paper.pdf"],
      pdf_max_pages: 20,
    }));
  });

  it("retries a failed knowledge ingestion in place", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "ingestions").mockResolvedValue([failedIngestion]);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue(healthyKnowledge);
    const retry = vi.spyOn(api, "retryIngestion").mockResolvedValue({
      ...failedIngestion,
      status: "queued",
      error: null,
    });

    renderWithQuery(<KnowledgePage />);

    await user.click(await screen.findByRole("button", { name: "重新执行" }));
    expect(retry).toHaveBeenCalledWith(failedIngestion.id);
  });
});
