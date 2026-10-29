import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type AgentRun, type Artifact, type Project, type ResearchReport } from "./api";
import { fileName, useReportLibrary } from "./hooks";
import { historyPage } from "../test/historyFixtures";

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

const project = { id: "project-1", name: "研究项目" } as Project;
const report = { id: "report-1", query: "研究问题", status: "queued", content: "", evidence: [], updated_at: "2026-01-01" } as unknown as ResearchReport;
const artifact = { id: "artifact-1", project_id: project.id, task_id: null, type: "research_report", status: "ready", created_at: "2026-01-01" } as Artifact;

function renderLibrary() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { ...renderHook(useReportLibrary, { wrapper }), client };
}

async function tick(ms = 50) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe("source filenames", () => {
  it.each([
    ["C:\\papers\\accuracy 95%.pdf", "accuracy 95%.pdf"],
    ["data/accuracy%20#1.pdf", "accuracy%20#1.pdf"],
    ["https://example.test/papers/%E4%B8%AD%E6%96%87%20paper.pdf?download=1#page=2", "中文 paper.pdf"],
    ["https://example.test/accuracy%95.pdf", "accuracy%95.pdf"],
    ["https://example.test/accuracy%20%.pdf", "accuracy%20%.pdf"],
    [null, ""],
  ])("safely renders %s", (uri, expected) => expect(fileName(uri)).toBe(expected));
});

describe("report library", () => {
  it("polls active reports on later pages while preserving pagination and totals", async () => {
    vi.useFakeTimers();
    const first: ResearchReport = { ...report, id: "report-first", status: "completed" };
    let completed = false;
    const reports = vi.spyOn(api, "reportPage").mockImplementation(async (offset = 0) => historyPage<ResearchReport>(
      [offset ? { ...report, status: completed ? "completed" : "queued" } : first], 26, offset,
    ));
    vi.spyOn(api, "projects").mockResolvedValue([]);
    const { result, unmount, client } = renderLibrary();
    await tick();
    expect(result.current.quickTotal).toBe(26);
    expect(result.current.hasNextPage).toBe(true);
    await tick(2_500);
    expect(reports).toHaveBeenCalledTimes(1);
    await act(async () => { await result.current.fetchNextPage(); });
    await tick();
    expect(reports).toHaveBeenLastCalledWith(25);
    expect(result.current.items.map((item) => item.id)).toEqual([first.id, report.id]);
    expect(result.current.hasNextPage).toBe(false);
    completed = true;
    await tick(2_600);
    expect(result.current.items.every((item) => item.status === "completed")).toBe(true);
    expect(reports.mock.calls.map(([offset]) => offset)).toEqual([0, 25, 0, 25]);
    expect(result.current.quickTotal).toBe(26);
    const count = reports.mock.calls.length;
    await tick(5_000);
    expect(reports).toHaveBeenCalledTimes(count);
    unmount(); client.clear();
  });

  it("polls queued quick reports through completion and then stops", async () => {
    vi.useFakeTimers();
    const reports = vi.spyOn(api, "reportPage").mockResolvedValueOnce(historyPage([report])).mockResolvedValueOnce(historyPage([{ ...report, status: "running" }])).mockResolvedValue(historyPage([{ ...report, status: "completed" }]));
    vi.spyOn(api, "projects").mockResolvedValue([]);
    const { result, unmount, client } = renderLibrary();
    await tick();
    expect(result.current.items[0]?.status).toBe("queued");
    await tick(2_500);
    expect(result.current.items[0]?.status).toBe("running");
    await tick(2_500);
    expect(result.current.items[0]?.status).toBe("completed");
    const count = reports.mock.calls.length;
    await tick(5_000);
    expect(reports).toHaveBeenCalledTimes(count);
    unmount(); client.clear();
  });

  it("refreshes project artifacts while a run is active and once more when it completes", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "reportPage").mockResolvedValue(historyPage([]));
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "tasks").mockResolvedValue([]);
    let completed = false;
    const runs = vi.spyOn(api, "projectRuns").mockImplementation(async () => {
      if (runs.mock.calls.length > 1) completed = true;
      return { items: [{ id: "run-1", status: completed ? "completed" : "running" } as AgentRun], next_cursor: null };
    });
    const artifacts = vi.spyOn(api, "artifacts").mockImplementation(async () => completed ? [artifact] : []);
    const { result, unmount, client } = renderLibrary();
    await tick();
    expect(result.current.isPending).toBe(false);
    expect(result.current.items).toEqual([]);
    await tick(2_600);
    expect(result.current.items[0]?.id).toBe(artifact.id);
    expect(artifacts.mock.calls.length).toBeGreaterThan(1);
    const count = artifacts.mock.calls.length;
    await tick(5_000);
    expect(artifacts).toHaveBeenCalledTimes(count);
    expect(runs).toHaveBeenCalledTimes(2);
    unmount(); client.clear();
  });

  it("includes artifact loading and errors, and retries the failed child query", async () => {
    vi.spyOn(api, "reportPage").mockResolvedValue(historyPage([]));
    vi.spyOn(api, "projects").mockResolvedValue([project]);
    vi.spyOn(api, "tasks").mockResolvedValue([]);
    vi.spyOn(api, "projectRuns").mockResolvedValue({ items: [], next_cursor: null });
    let rejectArtifacts!: (error: Error) => void;
    const artifacts = vi.spyOn(api, "artifacts").mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectArtifacts = reject; })).mockResolvedValue([artifact]);
    const { result } = renderLibrary();
    await waitFor(() => expect(artifacts).toHaveBeenCalledTimes(1));
    expect(result.current.isPending).toBe(true);
    const error = new Error("读取项目报告失败");
    await act(async () => rejectArtifacts(error));
    await waitFor(() => expect(result.current.error).toBe(error));
    expect(result.current.isPending).toBe(false);
    act(() => result.current.refetch());
    await waitFor(() => expect(result.current.items[0]?.id).toBe(artifact.id));
    expect(result.current.error).toBeNull();
    expect(artifacts).toHaveBeenCalledTimes(2);
  });
});
