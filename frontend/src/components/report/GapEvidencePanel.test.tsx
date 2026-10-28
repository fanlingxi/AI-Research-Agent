import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { api, type AgentOutput, type AgentRun, type Artifact, type CandidateDecisionResult, type GapEvidence, type ResearchCommand } from "../../lib/api";
import { GapEvidencePanel, GapRerunLinks } from "./GapEvidencePanel";

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

const output = {
  id: "output", run_id: "run", output_type: "research_report", rendered_text: "", validation: {}, created_at: "",
  output_sha256: "sha-1",
  structured: { draft: { answer_status: "partial", limitations: ["没有说明两种方式都正确时如何处理。", "超出允许资料范围。"] } },
} as AgentOutput;

const found: GapEvidence = {
  run_id: "run", gap_index: 0, gap: "没有说明两种方式都正确时如何处理。", scopes: ["papers"],
  search: { method: "vector", notices: [] },
  reviewed: [{ claim_id: "claim-2", statement: "两种方式都正确时归入 D+。", evidence: [{ evidence_id: "e-2", quote: "If both are correct, q is put into D+.", document_title: "SKR", page_start: 4, page_end: 4 }] }],
  reviewed_in_snapshot_count: 1,
  pending: [{
    candidate_id: "cand-1", kind: "relation", title: "SKR —提出→ 自知识标签", summary: "SKR 用有无检索的回答差异标注自知识。",
    quote: "We collect self-knowledge labels...", paper_title: "SKR", page_start: 3, page_end: 3,
    ingestion_id: "ing-1", collection_slug: "papers", prereview: { decision: "uncertain", reason: "关系方向需要人工确认" },
  }],
  can_rerun: true,
};

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return { ...render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/report"]}><Routes>
    <Route path="/report" element={<GapEvidencePanel output={output} projectId="project" runId="run" taskId="task" />} />
    <Route path="/agent-runs/new-run" element={<p>新的研究运行</p>} />
  </Routes></MemoryRouter></QueryClientProvider>), client };
}

it("finds unused reviewed knowledge and pending candidates, reviews one and reruns with lineage", async () => {
  const user = userEvent.setup();
  const lookup = vi.spyOn(api, "gapEvidence").mockResolvedValue(found);
  const decide = vi.spyOn(api, "decideCandidate").mockResolvedValue({} as CandidateDecisionResult);
  const submit = vi.spyOn(api, "submitResearchCommand").mockResolvedValue({ id: "command", status: "accepted", target_route: null } as ResearchCommand);
  vi.spyOn(api, "researchCommand").mockResolvedValue({ id: "command", status: "queued", target_route: "/agent-runs/new-run" } as ResearchCommand);
  const { client } = renderPanel();
  const reviewKeys = [
    ["knowledge-candidates", "ing-1", "pending-total"], ["knowledge-duplicates", "ing-1"],
    ["knowledge-ingestions"], ["knowledge-collection", "papers"], ["knowledge-collections"],
    ["knowledge-map"], ["knowledge-map-node", "entity-1"], ["knowledge-health"],
  ];
  for (const key of reviewKeys) client.setQueryData(key, { cached: true });

  expect(screen.getByText("超出允许资料范围。")).toBeInTheDocument();
  await user.click(screen.getAllByRole("button", { name: "查找补充证据" })[0]);
  expect(lookup).toHaveBeenCalledWith("run", "sha-1", 0);
  expect(await screen.findByText("两种方式都正确时归入 D+。")).toBeInTheDocument();
  expect(screen.getByText("SKR · 第 4 页")).toBeInTheDocument();
  expect(screen.getByText("AI 预审：关系方向需要人工确认")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "在审核页查看" })).toHaveAttribute("href", "/review?collection=papers&ingestion=ing-1");

  await user.click(screen.getByRole("button", { name: "批准" }));
  expect(decide).toHaveBeenCalledWith("cand-1", "approve");
  await screen.findByText("已批准");
  for (const key of reviewKeys) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
  await waitFor(() => expect(lookup).toHaveBeenCalledTimes(2));

  expect(screen.getByRole("button", { name: "用最新知识重新研究" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: /两种方式都正确时归入 D\+/ }));
  await user.click(screen.getByRole("button", { name: "用选中的 1 条知识重新研究" }));
  await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
  expect(submit.mock.calls[0][0]).toMatchObject({
    mode: "project_run", project_id: "project", task: { kind: "existing", task_id: "task" },
    gap_evidence_of: { run_id: "run", output_sha256: "sha-1", gap_index: 0, claim_ids: ["claim-2"] },
  });
  await screen.findByText("新的研究运行");
  expect(localStorage.getItem("research-gap-rerun:run:0")).toBeNull();
});

it("explains when evidence is already in the snapshot or missing everywhere", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "gapEvidence")
    .mockResolvedValueOnce({ ...found, reviewed: [], pending: [], reviewed_in_snapshot_count: 2, can_rerun: false })
    .mockResolvedValueOnce({ ...found, gap_index: 1, reviewed: [], pending: [], reviewed_in_snapshot_count: 0, can_rerun: false, search: { method: "lexical", notices: ["vector_unavailable"] } });
  renderPanel();
  const [first, second] = screen.getAllByRole("button", { name: "查找补充证据" });
  await user.click(first);
  expect(await screen.findByText(/已经在这份报告的快照里/)).toBeInTheDocument();
  await user.click(second);
  expect(await screen.findByText(/可能需要导入新的资料/)).toBeInTheDocument();
  expect(screen.getByText(/只用关键词检索/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "用最新知识重新研究" })).not.toBeInTheDocument();
});

it("stays hidden for complete and classic reports", () => {
  const client = new QueryClient();
  for (const draft of [{ answer_status: "complete", limitations: ["x"] }, { limitations: ["仅使用已发布证据。"] }]) {
    const { container, unmount } = render(<QueryClientProvider client={client}><MemoryRouter>
      <GapEvidencePanel output={{ ...output, structured: { draft } } as AgentOutput} projectId="project" runId="run" taskId="task" />
    </MemoryRouter></QueryClientProvider>);
    expect(container).toBeEmptyDOMElement();
    unmount();
  }
});

it("links reruns started from this report's gaps", async () => {
  vi.spyOn(api, "gapReruns").mockResolvedValue([
    { gap_index: 0, command_id: "c1", run: { id: "r1", status: "completed" } as AgentRun, artifact: { id: "a1" } as Artifact },
    { gap_index: 1, command_id: "c2", run: { id: "r2", status: "running" } as AgentRun, artifact: null },
  ]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><GapRerunLinks runId="run" /></MemoryRouter></QueryClientProvider>);
  expect(await screen.findByRole("link", { name: "阅读新报告" })).toHaveAttribute("href", "/artifacts/a1");
  expect(screen.getByRole("link", { name: "查看研究运行" })).toHaveAttribute("href", "/agent-runs/r2");
});
