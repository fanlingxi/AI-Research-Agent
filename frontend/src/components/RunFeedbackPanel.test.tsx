import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, type AgentRun, type AgentOutput, type FeedbackList, type RunFeedback, type RunRecheck } from "../lib/api";
import { RunFeedbackPanel } from "./RunFeedbackPanel";
import { SemanticObservation } from "./SemanticObservation";
import { TaskGoalEditor } from "./TaskGoalEditor";

const run = { id: "old-run", status: "completed", revision: 4, token_budget: 16000, context_snapshot_id: "old-snapshot", project_id: "project", task_id: "task" } as AgentRun;
const output: AgentOutput = { id: "output", run_id: run.id, output_type: "research", rendered_text: "", output_sha256: "hash", validation: {}, created_at: "", structured: { draft: { findings: [{ assertion: "A qualified conclusion", evidence_ids: ["evidence-1"] }] } } };
const issue: RunFeedback = { id: "feedback-1", run_id: run.id, status: "pending", revision: 1,
  request: { idempotency_key: "a", category: "incomplete", note: "Missing limitation", reporter: "Human", finding_index: 1, evidence_ids: ["evidence-1"] },
  anchor: { snapshot_id: "old-snapshot", snapshot_sha256: "old-hash", output_sha256: "report-hash", artifact_id: "old-artifact", assertion: "Original assertion", run_revision: 4, run_status: "completed", evidence: [{ evidence_id: "evidence-1", quote: "Original quote", source_version: "v1", location: { page: 2 } }] },
  decision: null, created_at: "", updated_at: "" };
const link: RunRecheck = { id: "recheck-1", feedback_id: issue.id, parent_run_id: run.id, child_run_id: "new-run", child_status: "completed", child_snapshot_id: "new-snapshot", child_output_id: "out", child_artifact_id: "new-artifact", request: { expected_revision: 2, idempotency_key: "b", resolution_note: "Added limitation" }, recheck: null, recheck_anchor: null, created_at: "", checked_at: null };
let history: FeedbackList;
function wrap(element = <RunFeedbackPanel run={run} output={output} />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter>{element}</MemoryRouter></QueryClientProvider>);
}
beforeEach(() => { sessionStorage.clear(); history = { feedback: [], links: [] }; vi.spyOn(api, "feedback").mockImplementation(async () => structuredClone(history)); });
afterEach(() => vi.restoreAllMocks());

it("registers finding-bound evidence and retries an uncertain response after remount with the same body", async () => {
  const user = userEvent.setup();
  const create = vi.spyOn(api, "createFeedback").mockRejectedValueOnce(new Error("Response lost")).mockImplementation(async () => { history.feedback = [issue]; return issue; });
  const view = wrap();
  await screen.findByText("尚无反馈");
  await user.click(screen.getByText("登记反馈"));
  await user.selectOptions(screen.getByLabelText("反馈对象"), "1");
  await user.click(screen.getByRole("checkbox"));
  await user.type(screen.getByLabelText("登记人"), "Operator");
  await user.type(screen.getByLabelText("问题说明"), "Missing scope");
  await user.click(screen.getByRole("button", { name: "提交反馈" }));
  await screen.findByText("Response lost");
  const original = create.mock.calls[0][1];
  expect(original).toMatchObject({ finding_index: 1, evidence_ids: ["evidence-1"], reporter: "Operator" });
  view.unmount(); wrap();
  await user.click(screen.getByText("登记反馈"));
  await user.click(screen.getByRole("button", { name: "重试同一提交" }));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  expect(create.mock.calls[1][1]).toEqual(original);
  await screen.findByText("Missing limitation");
  expect(sessionStorage.getItem("run-review:create:old-run")).toBeNull();
});

it("refreshes stale review revisions without silently accepting a different decision", async () => {
  history.feedback = [issue]; const user = userEvent.setup();
  const review = vi.spyOn(api, "reviewFeedback").mockRejectedValue(new ApiError("Revision changed", 409));
  wrap(); await screen.findByText("待复核");
  await user.type(screen.getByLabelText("复核人"), "Reviewer");
  await user.type(screen.getByLabelText("复核说明"), "Confirmed");
  await user.click(screen.getByRole("button", { name: "提交复核" }));
  await screen.findByText("Revision changed");
  expect(review.mock.calls[0][2]).toMatchObject({ expected_revision: 1, decision: "accepted" });
  history.feedback = [{ ...issue, status: "rejected", revision: 2, decision: { expected_revision: 1, decision: "rejected", reviewer: "Other", note: "Already reviewed" } }];
  await user.click(screen.getByRole("button", { name: "刷新状态后重新填写" }));
  await screen.findByText("不受理");
  expect(screen.queryByRole("button", { name: "创建修订运行" })).not.toBeInTheDocument();
  expect(review).toHaveBeenCalledTimes(1);
});

it("shows revision lineage then resolves through the parent endpoint even on the child page", async () => {
  history.links = [link]; const user = userEvent.setup();
  const check = vi.spyOn(api, "recheckFeedback").mockImplementation(async (_parent, id, body) => {
    history.links = [{ ...link, recheck: body, recheck_anchor: { status: "completed", run_revision: 5, snapshot_id: "new-snapshot", output_sha256: "new-hash" } }];
    return { id, recheck: body };
  });
  wrap(<RunFeedbackPanel run={{ ...run, id: "new-run" }} output={output} />);
  await screen.findByText("新旧运行关联");
  expect(screen.getByRole("link", { name: /原运行/ })).toHaveAttribute("href", "/agent-runs/old-run#feedback");
  await user.selectOptions(screen.getByLabelText("复检结论"), "resolved");
  await user.type(screen.getByLabelText("复检人"), "Reviewer");
  await user.type(screen.getByLabelText("复检说明"), "Compared reports");
  await user.click(screen.getByRole("button", { name: "提交复检" }));
  await screen.findByText("复检：已解决");
  expect(check).toHaveBeenCalledWith("old-run", "recheck-1", { decision: "resolved", reviewer: "Reviewer", note: "Compared reports" });
});

it.each(["queued", "failed"])("does not resolve a %s child or start another attempt before recheck", async (status) => {
  history = { feedback: [{ ...issue, status: "accepted", revision: 3 }], links: [{ ...link, child_status: status }] };
  wrap(); await screen.findByText("新旧运行关联");
  expect(screen.queryByRole("button", { name: "创建修订运行" })).not.toBeInTheDocument();
  if (status === "queued") expect(screen.queryByLabelText("复检结论")).not.toBeInTheDocument();
  else expect(screen.getByRole("option", { name: "已解决（需报告完成）" })).toBeDisabled();
});

it("starts a single linked rerun and exports only an explicit candidate", async () => {
  history.feedback = [{ ...issue, status: "accepted", revision: 2 }];
  const user = userEvent.setup();
  const rerun = vi.spyOn(api, "rerunFeedback").mockImplementation(async () => { history.links = [{ ...link, child_status: "queued" }]; return { ...run, id: "new-run" }; });
  const exported = vi.spyOn(api, "exportFeedback").mockResolvedValue({ gold_label: null, automatic_import: false });
  wrap(); await screen.findByText("已受理");
  await user.type(screen.getByLabelText("已完成的修订"), "Updated task");
  await user.click(screen.getByText("调整生成用量上限"));
  await user.type(screen.getByLabelText("新运行 Token 上限（可选）"), "1048576");
  await user.type(screen.getByLabelText("新快照 Token 上限（可选）"), "262144");
  await user.click(screen.getByRole("button", { name: "创建修订运行" }));
  await screen.findByText("新旧运行关联");
  expect(rerun).toHaveBeenCalledTimes(1);
  expect(rerun.mock.calls[0][2]).toMatchObject({ expected_revision: 2, resolution_note: "Updated task", token_budget: 1048576, context_max_tokens: 262144 });
  await user.click(screen.getByText("导出开发集候选"));
  await user.type(screen.getByLabelText("目标开发集版本"), "dev-next");
  await user.click(screen.getByRole("button", { name: "生成候选文件" }));
  await screen.findByRole("link", { name: "下载候选 JSON" });
  expect(exported).toHaveBeenCalledWith("old-run", "feedback-1", "dev-next");
});

it("shows history read failure and supports explicit retry", async () => {
  vi.mocked(api.feedback).mockRejectedValueOnce(new Error("Offline")); wrap();
  await userEvent.click(await screen.findByRole("button", { name: "重试" }));
  await screen.findByText("尚无反馈");
});

it("keeps semantic missing scores and supervised disagreement visible", async () => {
  vi.spyOn(api, "semanticObservation").mockResolvedValue({ available: true, annotation_mode: "user_supervised_ai_assisted", summary: { planned_runs: 2, total_findings: 1, human_labeled: 1, unlabeled: 0, judge_unavailable_on_labeled: 1, agreement_on_labeled: 0, uncovered_categories: ["contradicted"] }, records: [{ task_id: "case", run_id: "archived", status: "failed", snapshot_sha256: "hash", output_sha256: "hash", findings: [{ id: "1", assertion: "Claim", verdict: null, reason: "", human_label: "insufficient_evidence", human_note: "Missing direction", citations: [{ evidence_id: "e", quote: "Original citation", title: "Paper", source_version: "v1", page_start: "1", page_end: "1" }] }] }] });
  wrap(<SemanticObservation />);
  await userEvent.click(await screen.findByText("case · failed · 1 条结论"));
  expect(screen.getByText("模型观察：未评分")).toBeInTheDocument();
  expect(screen.getByText("监督审核：证据不足")).toBeInTheDocument();
  expect(screen.getByText("Original citation")).toBeInTheDocument();
  expect(screen.getByText(/非独立盲标/)).toBeInTheDocument();
  expect(screen.getByText(/已标注一致率：0.0%/)).toBeInTheDocument();
});

it("saves task corrections with the displayed revision", async () => {
  const update = vi.spyOn(api, "updateTaskGoal").mockResolvedValue({} as never);
  wrap(<TaskGoalEditor task={{ id: "task", revision: 7, goal: "Old goal" } as never} />);
  const user = userEvent.setup(); await user.click(screen.getByText("修订任务目标"));
  await user.clear(screen.getByLabelText("任务目标")); await user.type(screen.getByLabelText("任务目标"), "Qualified goal");
  await user.click(screen.getByRole("button", { name: "保存任务目标" }));
  expect(update).toHaveBeenCalledWith("task", 7, "Qualified goal");
});
