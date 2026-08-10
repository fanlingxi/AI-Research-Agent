import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ApiError, api, type AgentRun, type AgentTrace, type MemoryProposal, type Project } from "../lib/api";
import { AgentRunPage } from "../pages/AgentRunPage";
import { ReviewPage } from "../pages/ReviewPage";
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

    expect(await screen.findByText("此 Run 未完成，因此没有可展示的输出。")).toBeInTheDocument();
    expect(screen.queryByText("读取输出摘要…")).not.toBeInTheDocument();
    expect(screen.queryByText(/invalid TaskAnalysis JSON/)).not.toBeInTheDocument();
  });
});
