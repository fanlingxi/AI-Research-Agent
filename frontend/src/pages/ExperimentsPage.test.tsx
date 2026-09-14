import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type ExperimentSummary, type ExperimentVariant } from "../lib/api";
import { ExperimentsPage } from "./ExperimentsPage";

const variant: ExperimentVariant = {
  strategy: "legacy", path: "project_run", status: "completed", metrics: { "recall@10": 0 }, span_recall: null,
  latency_ms: 0, cost_cny: 0, cost_upper_cny: null, model_calls: 0, model: "未调用（仅检索）",
  semantic_success: null, semantic_review: "未测量", error: "", issues: [], content: null,
  evidence: [{ rank: 1, source_id: "paper-1", title: "Source paper", version: "v1", page: 3, start: 0, end: 12, text: "Original evidence" }],
};
const experiment: ExperimentSummary = {
  id: "a04--test", name: "test", kind: "retrieval", planned: 12, recorded: 12, task_count: 2,
  status_counts: { completed: 11, failed: 1 }, splits: ["dev"], model: "hash", decision: "保留旧策略默认", limitations: [],
};
function setup() {
  const failed = { ...variant, strategy: "hybrid-v1", status: "failed", error: "Model JSON invalid", cost_cny: null };
  vi.spyOn(api, "experiments").mockResolvedValue({ experiments: [experiment], unavailable: [] });
  vi.spyOn(api, "experiment").mockResolvedValue({ experiment, tasks: [
    { id: "q1", question: "First question", split: "dev", variants: [variant, { ...variant, path: "quick_report" }] },
    { id: "q2", question: "Failed question", split: "dev", variants: [failed] },
  ] });
  vi.spyOn(api, "experimentTask").mockImplementation(async (_id, task, path) => ({ task_id: task, question: task === "q1" ? "First question" : "Failed question", split: "dev", path, variants: task === "q1" ? [variant, { ...variant, strategy: "hybrid-v1", span_recall: 0.5 }] : [failed] }));
}
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><ExperimentsPage /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => { vi.spyOn(api, "semanticObservation").mockResolvedValue({ available: false, records: [], summary: null }); });
afterEach(() => vi.restoreAllMocks());

describe("experiment comparison", () => {
  it("keeps historical scoring context aligned when switching batches", async () => {
    setup();
    const holdout = { ...experiment, id: "a04--holdout", splits: ["holdout"], task_count: 15, planned: 90 };
    const generation = { ...experiment, id: "a02--generation", kind: "generation" as const };
    vi.mocked(api.experiments).mockResolvedValue({ experiments: [holdout, generation], unavailable: [] });
    vi.mocked(api.experiment).mockImplementation(async (id) => ({ experiment: id === holdout.id ? holdout : generation, tasks: [] }));
    mount();
    const user = userEvent.setup();
    const context = await screen.findByRole("region", { name: "当前批次说明" });
    expect(context).toHaveTextContent("历史保留集 holdout · 15 题");
    expect(context).toHaveTextContent("本批次没有生成报告");
    expect(context).toHaveTextContent("不能将这批结果理解为最新系统表现");
    expect(screen.getByText(/批次列表尚未接入 A08/)).toBeInTheDocument();
    expect(screen.getByText("90 次 · 15 题")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("实验批次"), generation.id);
    await waitFor(() => expect(screen.getByRole("region", { name: "当前批次说明" })).toHaveTextContent("历史真实生成 · 开发集 dev"));
    const generationContext = screen.getByRole("region", { name: "当前批次说明" });
    expect(generationContext).toHaveTextContent("本批次运行了真实模型并归档报告");
    expect(generationContext).toHaveTextContent("结果不能作为独立泛化验证");
    expect(generationContext).not.toHaveTextContent("本批次没有生成报告");
    expect(generationContext).not.toHaveTextContent("历史保留集");
    expect(screen.getByLabelText("检索路径")).toBeDisabled();
  });
  it("compares evidence and preserves zero, unscored and unknown values", async () => {
    setup(); mount();
    const card = await screen.findByRole("article", { name: "旧策略结果" });
    expect(within(card).getAllByText("未评分").length).toBeGreaterThan(0);
    expect(within(card).getByText("0.0%")).toBeInTheDocument();
    expect(within(card).getByText("¥0.000000")).toBeInTheDocument();
    expect(within(card).getByText("未知")).toBeInTheDocument();
    expect(within(card).getByText("Original evidence")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "混合 RRF结果" })).toHaveTextContent("50.0%");
  });
  it("filters failures without changing denominators and switches retrieval path", async () => {
    setup(); mount(); const user = userEvent.setup();
    await screen.findByRole("article", { name: "旧策略结果" });
    await user.selectOptions(screen.getByLabelText("检索路径"), "quick_report");
    await waitFor(() => expect(api.experimentTask).toHaveBeenCalledWith("a04--test", "q1", "quick_report"));
    await user.selectOptions(screen.getByLabelText("检索路径"), "project_run");
    await user.selectOptions(screen.getByLabelText("结果筛选"), "failed");
    expect(await screen.findByText("Model JSON invalid")).toBeInTheDocument();
    expect(screen.getByText("12 次 · 2 题")).toBeInTheDocument();
    expect(screen.getByText("11 / 1")).toBeInTheDocument();
    await user.type(screen.getByLabelText("搜索题目"), "no-match");
    expect(screen.getByText("没有匹配的题目")).toBeInTheDocument();
  });
  it("shows loading and an empty catalog", async () => {
    let resolve!: (value: { experiments: ExperimentSummary[]; unavailable: [] }) => void;
    vi.spyOn(api, "experiments").mockReturnValue(new Promise((done) => { resolve = done; }));
    mount(); expect(screen.getByText("正在读取实验目录…")).toBeInTheDocument();
    resolve({ experiments: [], unavailable: [] });
    expect(await screen.findByText("尚无可查看的实验")).toBeInTheDocument();
  });
  it("shows request failure and allows retry", async () => {
    vi.spyOn(api, "experiments").mockRejectedValueOnce(new Error("archive unavailable")).mockResolvedValue({ experiments: [], unavailable: [] });
    mount(); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /重试/ }));
    expect(await screen.findByText("尚无可查看的实验")).toBeInTheDocument();
  });
});
