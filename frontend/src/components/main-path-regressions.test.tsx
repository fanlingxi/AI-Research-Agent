import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, type ContextSnapshotSummary, type KnowledgeIngestion, type Project, type ResearchCommand, type ResearchReport, type WorkspaceTask } from "../lib/api";
import { KnowledgePage } from "../pages/KnowledgePage";
import { ProjectWorkspacePage } from "../pages/ProjectWorkspacePage";
import { QuickReportPage } from "../pages/QuickReportPage";
import { emptyIngestionStatistics, historyPage } from "../test/historyFixtures";
import { ResearchCommandLauncher } from "./ResearchCommandLauncher";

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); localStorage.clear(); });

const project: Project = {
  id: "project-1", name: "研究项目", goal: "研究目标", domain: "test", status: "active", metadata: {}, revision: 1,
  created_at: "2026-01-01", updated_at: "2026-01-01",
};
const tasks: WorkspaceTask[] = ["task-1", "task-2"].map((id) => ({
  id, project_id: project.id, title: id, goal: "研究问题", status: "ready", priority: "normal", metadata: {}, revision: 1,
  created_at: project.created_at, updated_at: project.updated_at,
}));
const snapshot: ContextSnapshotSummary = {
  id: "snapshot-1", persisted: true, project_id: project.id, task_id: "task-1", project_revision: 1, task_revision: 1,
  builder_version: "b", package_schema_version: "1.2", package_sha256: "hash", token_budget: 16000, used_tokens: 1000,
  collection_scopes: ["papers"], item_counts: { knowledge_claim_bundles: 4 },
  diagnostics: { knowledge_coverage: "available", selected_claim_bundles: 4, notices: [] }, created_at: project.created_at,
};
const command = { id: "command-1", status: "accepted", orchestration_stage: "accepted", target_route: null } as ResearchCommand;

function renderWithQuery(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>), client };
}

function mockWorkspace() {
  vi.spyOn(api, "project").mockResolvedValue(project);
  vi.spyOn(api, "tasks").mockResolvedValue(tasks);
  vi.spyOn(api, "decisions").mockResolvedValue([]);
  vi.spyOn(api, "artifacts").mockResolvedValue([]);
  vi.spyOn(api, "scopes").mockResolvedValue([{ project_id: project.id, collection_slug: "papers", created_at: project.created_at }]);
  vi.spyOn(api, "projectRuns").mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, "taskRuns").mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, "proposals").mockResolvedValue([]);
  vi.spyOn(api, "collections").mockResolvedValue([]);
}

function renderWorkspace() {
  return renderWithQuery(<MemoryRouter initialEntries={["/projects/project-1/overview"]}><Routes>
    <Route path="/projects/:projectId/:section" element={<ProjectWorkspacePage />} />
  </Routes></MemoryRouter>);
}

async function tick(ms = 50) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe("project scope and snapshot regressions", () => {
  it("blocks scope saves when collections fail, then preserves the selected scope after retry", async () => {
    mockWorkspace();
    vi.mocked(api.collections).mockRejectedValue(new ApiError("Unavailable", 503));
    const replace = vi.spyOn(api, "replaceScopes").mockResolvedValue([]);
    renderWorkspace();
    await userEvent.click(await screen.findByRole("button", { name: "管理知识范围" }));
    await screen.findByRole("alert");
    const save = screen.getByRole("button", { name: "保存范围" });
    expect(save).toBeDisabled();
    fireEvent.submit(save.closest("form")!);
    expect(replace).not.toHaveBeenCalled();
    vi.mocked(api.collections).mockResolvedValue([{ slug: "papers", name: "论文库", is_system: false, ingestion_count: 1, updated_at: null }]);
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("checkbox", { name: /论文库/ })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "保存范围" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith(project.id, project.revision, ["papers"]));
  });

  it("keeps the scope editor unavailable until existing scopes load successfully", async () => {
    mockWorkspace();
    vi.mocked(api.scopes).mockRejectedValue(new ApiError("Scope not found", 404));
    renderWorkspace();
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "管理知识范围" })).not.toBeInTheDocument();
  });

  it("ignores old preview and freeze responses after switching tasks and uses only the new task snapshot", async () => {
    mockWorkspace();
    let resolveOld!: (value: ContextSnapshotSummary) => void;
    let resolvePreview!: (value: { snapshot: ContextSnapshotSummary }) => void;
    const freeze = vi.spyOn(api, "createSnapshot").mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; })).mockResolvedValue({ ...snapshot, id: "snapshot-2", task_id: "task-2" });
    vi.spyOn(api, "previewContext").mockImplementation(() => new Promise((resolve) => { resolvePreview = resolve; }));
    const start = vi.spyOn(api, "startRun").mockRejectedValue(new Error("测试结束"));
    renderWorkspace();
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "选择任务" }), "task-1");
    await userEvent.click(screen.getByRole("button", { name: "预览" }));
    await userEvent.click(screen.getByRole("button", { name: "冻结快照" }));
    await waitFor(() => expect(freeze).toHaveBeenCalledWith(project.id, "task-1"));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "选择任务" }), "task-2");
    await act(async () => { resolveOld(snapshot); resolvePreview({ snapshot: { ...snapshot, persisted: false } }); });
    await waitFor(() => expect(screen.queryByText(/已冻结：|预览：/)).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "开始研究" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "冻结快照" }));
    await screen.findByText(/已冻结：/);
    await userEvent.click(screen.getByRole("button", { name: "开始研究" }));
    await waitFor(() => expect(start).toHaveBeenCalledWith(project.id, "task-2", "snapshot-2", true));
  });

  it("rejects a returned snapshot belonging to a different task before starting a run", async () => {
    mockWorkspace();
    vi.spyOn(api, "createSnapshot").mockResolvedValue(snapshot);
    const start = vi.spyOn(api, "startRun");
    renderWorkspace();
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "选择任务" }), "task-2");
    await userEvent.click(screen.getByRole("button", { name: "冻结快照" }));
    await screen.findByText(/已冻结：/);
    await userEvent.click(screen.getByRole("button", { name: "开始研究" }));
    await screen.findByText(/快照与当前任务不匹配/);
    expect(start).not.toHaveBeenCalled();
  });
});

describe("research command query recovery", () => {
  it.each([404, 409, 422])("stops automatic requests on HTTP %s and supports explicit retry and clearing", async (status) => {
    vi.useFakeTimers();
    localStorage.setItem("research-workspace.pending-command", JSON.stringify({ commandId: command.id, key: "key" }));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    const query = vi.spyOn(api, "researchCommand").mockRejectedValue(new ApiError("Command unavailable", status));
    const { unmount, client } = renderWithQuery(<MemoryRouter><ResearchCommandLauncher /></MemoryRouter>);
    await tick();
    expect(screen.getByText("无法读取研究指令")).toBeInTheDocument();
    expect(query).toHaveBeenCalledTimes(1);
    await tick(5_000);
    expect(query).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await tick();
    expect(query).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "清除失效记录" }));
    await tick(3_000);
    expect(localStorage.getItem("research-workspace.pending-command")).toBeNull();
    expect(screen.queryByText("无法读取研究指令")).not.toBeInTheDocument();
    expect(query).toHaveBeenCalledTimes(2);
    unmount(); client.clear();
  });

  it("hides stale preparation feedback when a submitted command cannot be queried, and resumes after retry", async () => {
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "submitResearchCommand").mockResolvedValue(command);
    const query = vi.spyOn(api, "researchCommand").mockRejectedValueOnce(new ApiError("Not found", 404)).mockResolvedValue({ ...command, status: "queued", target_route: "/reports/new" });
    renderWithQuery(<MemoryRouter><Routes>
      <Route path="/" element={<ResearchCommandLauncher />} />
      <Route path="/reports/new" element={<p>新报告</p>} />
    </Routes></MemoryRouter>);
    await userEvent.type(screen.getByRole("textbox", { name: "研究指令" }), "研究可靠性");
    await userEvent.click(screen.getByRole("button", { name: "提交并开始研究" }));
    await screen.findByText("无法读取研究指令");
    expect(screen.queryByText(/指令已保存/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByText("新报告");
    expect(query).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem("research-workspace.pending-command")).toBeNull();
  });
});

describe("ingestion and report actions", () => {
  it("renders an interrupted ingestion with a Chinese status and retries it in place", async () => {
    const ingestion: KnowledgeIngestion = {
      id: "ingestion-1", topic: "论文", topic_slug: "papers", collection: "论文", collection_slug: "papers", sources: ["accuracy 95%.pdf"],
      pdf_max_pages: 20, status: "interrupted", document_count: 0, candidate_count: 0, published_count: 0,
      queue_position: null, job_attempts: 1, error: null, created_at: project.created_at, updated_at: project.updated_at,
    };
    const ingestions = vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion]));
    vi.spyOn(api, "ingestionStatistics").mockResolvedValue({ ...emptyIngestionStatistics, total: 1 });
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeDocuments").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue({ status: "ok", services: {}, knowledge: { live_llm_configured: true } } as Awaited<ReturnType<typeof api.knowledgeHealth>>);
    const retry = vi.spyOn(api, "retryIngestion").mockImplementation(async () => {
      ingestions.mockResolvedValue(historyPage([{ ...ingestion, status: "queued" }]));
      return { ...ingestion, status: "queued" };
    });
    renderWithQuery(<MemoryRouter><KnowledgePage /></MemoryRouter>);
    await screen.findByText("已中断");
    expect(screen.getByText("accuracy 95%.pdf")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新执行" }));
    await waitFor(() => expect(retry).toHaveBeenCalledWith(ingestion.id));
    await screen.findByText("排队中");
    expect(screen.queryByRole("button", { name: "重新执行" })).not.toBeInTheDocument();
  });

  it.each([
    ["failed", "保留的正文", false], ["queued", "保留的正文", false], ["completed", "已完成正文", true], ["completed", "   ", false],
  ])("allows download only for completed reports with content (%s, %s)", async (status, content, canDownload) => {
    vi.spyOn(api, "report").mockResolvedValue({
      id: "report-1", query: "研究问题", topic_slugs: [], top_k: 8, report_depth: "standard", status, content, evidence: [],
      evaluation: null, run_metadata: {}, error: null, created_at: project.created_at, updated_at: project.updated_at,
    } as ResearchReport);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "knowledgeHealth").mockResolvedValue({ status: "ok", services: {}, knowledge: {} } as Awaited<ReturnType<typeof api.knowledgeHealth>>);
    renderWithQuery(<MemoryRouter initialEntries={["/reports/report-1"]}><Routes><Route path="/reports/:reportId" element={<QuickReportPage />} /></Routes></MemoryRouter>);
    await screen.findByRole("heading", { name: "研究问题" });
    expect(Boolean(screen.queryByRole("link", { name: "下载 Markdown" }))).toBe(canDownload);
  });
});
