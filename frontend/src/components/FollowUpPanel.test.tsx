import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { api, type AgentRun, type Artifact, type ArtifactContent, type ResearchCommand, type WorkspaceTask } from "../lib/api";
import { FollowUpLinks, FollowUpPanel } from "./FollowUpPanel";

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

it("shows parent/child report links and generates, edits, submits then opens the new run", async () => {
  const user = userEvent.setup();
  const draft = vi.spyOn(api, "followUpDraft").mockResolvedValue({ standalone_question: "方法 A 有哪些局限？", suggestions: ["方法 A 如何评估？", "方法 A 与 B 有何差异？", "方法 A 需要多少数据？"], fallback: false });
  vi.spyOn(api, "artifactContent").mockResolvedValue({ rendered_markdown: "# 父报告标题\n\n结论" } as ArtifactContent);
  vi.spyOn(api, "task").mockResolvedValue({ title: "父任务" } as WorkspaceTask);
  vi.spyOn(api, "followUps").mockResolvedValue([
    { task: { id: "child", title: "已有追问报告" } as WorkspaceTask, latest_run: { id: "child-run", status: "completed" } as AgentRun, artifact: { id: "child-report" } as Artifact },
    { task: { id: "queued", title: "进行中的追问" } as WorkspaceTask, latest_run: { id: "queued-run", status: "running" } as AgentRun, artifact: null },
  ]);
  const submit = vi.spyOn(api, "submitResearchCommand").mockResolvedValue({ id: "command", status: "accepted", target_route: null } as ResearchCommand);
  vi.spyOn(api, "researchCommand").mockResolvedValue({ id: "command", status: "queued", target_route: "/agent-runs/new-run" } as ResearchCommand);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/report"]}><Routes>
    <Route path="/report" element={<>
      <FollowUpLinks runId="run" task={{ id: "task", project_id: "project", title: "当前任务", goal: "", status: "completed", priority: "normal", revision: 1, created_at: "", updated_at: "", metadata: { follow_up_of: { run_id: "parent-run", artifact_id: "parent-report", parent_task_id: "parent-task" } } }} />
      <FollowUpPanel runId="run" artifactId="report" projectId="project" />
    </>} />
    <Route path="/agent-runs/new-run" element={<p>新的研究运行</p>} />
  </Routes></MemoryRouter></QueryClientProvider>);
  expect(await screen.findByRole("link", { name: "父报告标题" })).toHaveAttribute("href", "/artifacts/parent-report");
  expect(await screen.findByRole("link", { name: "已有追问报告" })).toHaveAttribute("href", "/artifacts/child-report");
  expect(screen.getByRole("link", { name: "进行中的追问" })).toHaveAttribute("href", "/agent-runs/queued-run");
  await user.type(screen.getByLabelText("你的追问"), "它有什么局限？");
  await user.click(screen.getByRole("button", { name: "生成问题" }));
  expect(draft).toHaveBeenCalledWith("run", "它有什么局限？");
  await screen.findByDisplayValue("方法 A 有哪些局限？");
  await user.click(screen.getByRole("button", { name: "方法 A 与 B 有何差异？" }));
  const standalone = screen.getByLabelText(/独立研究问题/);
  expect(standalone).toHaveValue("方法 A 与 B 有何差异？");
  await user.clear(standalone);
  await user.type(standalone, "方法 A 与 B 在长文本检索上有哪些差异？");
  await user.click(screen.getByRole("button", { name: "开始研究" }));
  await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
  expect(submit.mock.calls[0][0]).toMatchObject({ mode: "project_run", instruction: "方法 A 与 B 在长文本检索上有哪些差异？", project_id: "project", task: { kind: "new" }, follow_up_of: { run_id: "run", artifact_id: "report" } });
  expect(submit.mock.calls[0][0]).not.toHaveProperty("token_budget");
  await screen.findByText("新的研究运行");
  expect(localStorage.getItem("research-follow-up:run")).toBeNull();
});

it("restores a failed command and allows editing a new follow-up", async () => {
  const user = userEvent.setup();
  localStorage.setItem("research-follow-up:run", JSON.stringify({ key: "old-key", commandId: "failed-command", payload: { mode: "project_run", instruction: "原来的独立研究问题" } }));
  vi.spyOn(api, "researchCommand").mockResolvedValue({ id: "failed-command", status: "failed", error: "暂时没有可用证据", target_route: null } as ResearchCommand);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><FollowUpPanel runId="run" artifactId="report" projectId="project" /></MemoryRouter></QueryClientProvider>);
  await screen.findByText("研究未能启动");
  expect(screen.getByLabelText(/独立研究问题/)).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "修改问题" }));
  expect(screen.getByLabelText(/独立研究问题/)).toBeEnabled();
  await user.clear(screen.getByLabelText(/独立研究问题/));
  await user.type(screen.getByLabelText(/独立研究问题/), "修改后的独立研究问题");
  expect(screen.getByRole("button", { name: "开始研究" })).toBeEnabled();
  expect(localStorage.getItem("research-follow-up:run")).toBeNull();
});
