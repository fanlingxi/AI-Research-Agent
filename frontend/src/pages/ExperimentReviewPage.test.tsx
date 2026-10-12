import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { api, type ReviewDetail, type ReviewVerdict } from "../lib/api";
import { ExperimentReviewPage } from "./ExperimentReviewPage";

afterEach(() => vi.restoreAllMocks());

it("独立打分后保存并自动下一题，展示上一题的 AI 差异，支持跳过与修改", async () => {
  const packet: ReviewDetail["packet"] = {
    question: "第一题的证据是否充分？", expected_outcome: "qualified_answer",
    outcome_guide: { qualified_answer: "应回答但保留限定条件" },
    required_conclusions: [{ id: "c1", statement: "保留限定条件" }],
    forbidden_conclusions: ["任何情形都成立"], missing_information: [], report: "这是一份真实归档报告。", instructions: "逐条判断",
  };
  let human: ReviewVerdict | null = null;
  const ai: ReviewVerdict = { covered: { c1: false }, violated: [true], outcome_ok: false, note: "仅提交后可见的 AI 理由" };
  vi.spyOn(api, "reviewRuns").mockImplementation(async () => ({ runs: [{ run_ref: "run", dataset: "holdout-v3", name: "测试运行", question_count: 2, item_count: 2, human_count: human ? 1 : 0, ai_judge_count: 2 }] }));
  vi.spyOn(api, "reviewItems").mockImplementation(async () => ({ items: [
    { key: "q1__A", human_reviewed: !!human, ai_agree: false, ai_judge_count: 2 },
    { key: "q2__A", human_reviewed: false, ai_agree: true, ai_judge_count: 2 },
  ], total: 2, human_count: human ? 1 : 0 }));
  vi.spyOn(api, "reviewItem").mockImplementation(async (_, key) => ({ key,
    packet: key === "q1__A" ? packet : { ...packet, question: "第二题的证据是否充分？" },
    human: key === "q1__A" ? human : null,
    ...(key === "q1__A" && human ? { ai_judges: { api: ai } } : {}),
  }));
  vi.spyOn(api, "reviewHuman").mockRejectedValueOnce(new Error("暂时无法写入，请重试"))
    .mockImplementation(async (_, key, value) => { human = { ...value, judge: "human" }; return { key, human }; });
  vi.spyOn(api, "reviewAgreement").mockImplementation(async () => ({ human_count: human ? 1 : 0, judges: {} }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><ExperimentReviewPage /></MemoryRouter></QueryClientProvider>);
  const user = userEvent.setup();
  await user.selectOptions(await screen.findByLabelText("选择评测运行"), "run");
  await screen.findByRole("heading", { name: packet.question });
  expect(screen.getByText("应回答但保留限定条件")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "AI 裁判对照" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "提交并下一题" })).toBeDisabled();
  // Shortcuts toggle both required and forbidden conclusions. No defaults are preselected.
  await user.keyboard("1");
  await user.click(screen.getByRole("button", { name: "未断言" }));
  await user.keyboard("y");
  await user.type(screen.getByLabelText("一句备注（可选）"), "ny1");
  expect(within(screen.getByRole("group", { name: "整体处理方式是否正确" })).getByRole("button", { name: "是" })).toHaveAttribute("aria-pressed", "true");
  await user.click(screen.getByRole("button", { name: "提交并下一题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法写入");
  expect(screen.queryByRole("region", { name: "AI 裁判对照" })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: packet.question })).toBeInTheDocument();
  await user.click(screen.getByRole("heading", { name: packet.question }));
  await user.keyboard("{Enter}");
  await screen.findByRole("heading", { name: "第二题的证据是否充分？" });
  expect(api.reviewHuman).toHaveBeenLastCalledWith("run", "q1__A", { covered: { c1: true }, violated: [false], outcome_ok: true, note: "ny1" });
  const comparison = await screen.findByRole("region", { name: "AI 裁判对照" });
  await waitFor(() => expect(comparison).toHaveTextContent("仅提交后可见的 AI 理由"));
  expect(within(comparison).getAllByText(/不一致/)).toHaveLength(3);
  expect(screen.getByRole("status", { name: "核验进度" })).toHaveTextContent("已标 1 / 2 项");
  expect(screen.getByRole("button", { name: "提交并下一题" })).toBeDisabled();
  await user.keyboard("{ArrowRight}");
  expect(await screen.findByText("本轮核验已结束")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "继续核验跳过的题目" }));
  await screen.findByRole("heading", { name: "第二题的证据是否充分？" });
  await user.selectOptions(screen.getByLabelText("核验项"), "q1__A");
  await screen.findByRole("heading", { name: packet.question });
  await user.click(screen.getByRole("button", { name: "未覆盖" }));
  await user.click(screen.getByRole("button", { name: "提交并下一题" }));
  await waitFor(() => expect(api.reviewHuman).toHaveBeenCalledTimes(3));
  expect(api.reviewHuman).toHaveBeenLastCalledWith("run", "q1__A", expect.objectContaining({ covered: { c1: false } }));
});
