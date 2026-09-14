import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { api, type ProjectionRebuild } from "../lib/api";
import { ProjectionMaintenance } from "./ProjectionMaintenance";

afterEach(() => vi.restoreAllMocks());

const queued: ProjectionRebuild = {
  id: "rebuild-test", status: "queued", attempts: 0, last_error: null,
  payload: { target: "qdrant", collection_slug: null },
};

function show(initial = "/runtime") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initial]}><ProjectionMaintenance /></MemoryRouter></QueryClientProvider>);
}

it("requires confirmation, clears it on scope changes, and reuses the key after a network failure", async () => {
  vi.spyOn(api, "collections").mockResolvedValue([{ slug: "papers", name: "论文", is_system: false, ingestion_count: 1, updated_at: "2026-09-14" }]);
  const send = vi.spyOn(api, "submitRebuild").mockRejectedValueOnce(new Error("连接中断")).mockResolvedValue(queued);
  vi.spyOn(api, "rebuild").mockResolvedValue(queued);
  show();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "投影重建" }));
  await screen.findByRole("option", { name: "论文" });
  const submit = screen.getByRole("button", { name: "提交重建" });
  expect(submit).toBeDisabled();
  await user.click(screen.getByRole("checkbox"));
  await user.selectOptions(screen.getByLabelText("资料集合"), "papers");
  expect(submit).toBeDisabled();
  await user.selectOptions(screen.getByLabelText("投影目标"), "all");
  await user.click(screen.getByRole("checkbox"));
  await user.click(submit);
  await screen.findByText("连接中断");
  await user.click(submit);
  await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(send.mock.calls[0][0]).toEqual({ target: "all", collection_slug: "papers", confirmed: true });
  expect(send.mock.calls[1][1]).toBe(send.mock.calls[0][1]);
  expect(submit).toBeDisabled();
});

it("restores a failed request from the URL and displays its retry result", async () => {
  vi.spyOn(api, "rebuild").mockResolvedValue({ ...queued, status: "failed", attempts: 1, last_error: "索引离线" });
  const retry = vi.spyOn(api, "retryRebuild").mockResolvedValue({
    ...queued, status: "completed", attempts: 2,
    payload: { ...queued.payload, result: { chunks: 5, entities: 2, relations: 1, topics: 1 } },
  });
  show("/runtime?rebuild=rebuild-test");
  await screen.findByText("索引离线");
  await userEvent.setup().click(screen.getByRole("button", { name: "重试此重建" }));
  expect(retry).toHaveBeenCalledWith("rebuild-test");
  await screen.findByText("完成：5 个切片、2 个实体、1 条关系、1 个集合文档。");
});

it("loads raw read-only state only on request", async () => {
  const health = vi.spyOn(api, "knowledgeHealth").mockResolvedValue({ status: "ok", services: {}, knowledge: { schema_version: 20, live_llm_configured: false, llm_provider: "mock", jobs: {}, projections: {} } });
  vi.spyOn(api, "runtimeOverview").mockResolvedValue({ status: "ok", generated_at: "2026-09-14", services: {}, executors: [], work_counts: {}, projection_backlog: { queued: 0, running: 0, failed: 0, completed: 0, oldest_queued_at: null } });
  show();
  expect(health).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getByRole("button", { name: "原始状态" }));
  await screen.findByText("API / Knowledge");
  expect(screen.getAllByTestId("json-details")).toHaveLength(2);
  expect(health).toHaveBeenCalledTimes(1);
});
