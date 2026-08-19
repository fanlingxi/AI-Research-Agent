import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

describe("workspace API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses the frozen /api/v1 snapshot preview contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ snapshot: { persisted: false } }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.previewContext("project one", "task one");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/projects/project%20one/workspace-tasks/task%20one/context-snapshots/preview",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("keeps AgentRun creation on its existing lifecycle API", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "run" }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.startRun("project", "task", "snapshot", false);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/project/workspace-tasks/task/agent-runs",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          context_snapshot_id: "snapshot",
          workflow: "research",
          create_memory_proposal: false,
          max_steps: 10,
          max_tool_calls: 3,
          token_budget: 6000,
        }),
      }),
    );
  });

  it("uses the existing revision-guarded Project scope API", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify([]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.replaceScopes("project one", 3, ["collection-a", "collection-b"]);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/project%20one/knowledge-scopes",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          expected_project_revision: 3,
          collection_slugs: ["collection-a", "collection-b"],
        }),
      }),
    );
  });

  it("keeps Knowledge Explorer queries constrained to selected collections", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ evidence: [], graph: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.searchKnowledge("grounded research", ["collection-a", "collection-b"]);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/knowledge/search?q=grounded+research&top_k=8&collection_slug=collection-a&collection_slug=collection-b",
      expect.any(Object),
    );
  });

  it("keeps a non-JSON proxy failure readable", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("connect ECONNREFUSED 127.0.0.1:8010", { status: 502 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.projects()).rejects.toMatchObject({
      name: "ApiError",
      status: 502,
      message: "connect ECONNREFUSED 127.0.0.1:8010",
    });
  });

  it("extracts a JSON API error after reading its body once", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "后端暂时不可用" }), { status: 503 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.projects()).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      message: "后端暂时不可用",
    });
  });

  it("submits an explicitly selected batch review to its ingestion", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ requested: 2, applied: 2, replayed: 0, skipped: [] }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.decideCandidatesBulk("ingestion one", ["candidate-a", "candidate-b"], "approve");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/knowledge/ingestions/ingestion%20one/candidates/bulk-decision",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ candidate_ids: ["candidate-a", "candidate-b"], decision: "approve" }),
      }),
    );
  });

  it("runs the strict high-confidence approval rule for one ingestion", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ requested: 3, applied: 2, skipped: [] }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.autoApproveHighConfidence("ingestion one");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/knowledge/ingestions/ingestion%20one/auto-approve-high-confidence",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
