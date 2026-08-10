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
      expect.objectContaining({ method: "POST" }),
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
});
