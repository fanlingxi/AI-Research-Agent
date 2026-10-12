import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeMapGraph, MapNodeDetail } from "./api";
import { KnowledgeMapPage } from "./KnowledgeMapPage";

// WebGL is unavailable in jsdom; the canvas is replaced with a list of clickable nodes.
vi.mock("./GraphCanvas", () => ({
  default: ({ data, onSelect }: { data: KnowledgeMapGraph; onSelect: (id: string) => void }) => (
    <ul data-testid="graph-canvas">
      {data.nodes.map((node) => (
        <li key={node.id}><button onClick={() => onSelect(node.id)} type="button">{node.label}</button></li>
      ))}
    </ul>
  ),
}));

const graph: KnowledgeMapGraph = {
  nodes: [
    { id: "paper-1", label: "检索增强生成综述", type: "Paper", kind: "entity", status: "published", degree: 2, evidence_count: 1, paper_count: 1, score: 3, matched: false },
    { id: "method-1", label: "查询改写", type: "Method", kind: "entity", status: "published", degree: 1, evidence_count: 2, paper_count: 1, score: 3, matched: false },
    { id: "concept-1", label: "稠密检索", type: "Concept", kind: "entity", status: "published", degree: 1, evidence_count: 1, paper_count: 1, score: 2, matched: false },
  ],
  edges: [
    { id: "r1", source: "paper-1", target: "method-1", type: "PRESENTS", kind: "relation", status: "published", weight: 1, label: "论文提出查询改写" },
    { id: "e1", source: "paper-1", target: "concept-1", type: "EVIDENCE", kind: "evidence", status: "published", weight: 1, label: "" },
  ],
  stats: { total_nodes: 3, total_edges: 2, shown_nodes: 3, shown_edges: 2, pending_nodes: 0, pending_edges: 0, truncated: false, limit: 300 },
  type_counts: { Concept: 1, Method: 1, Paper: 1 },
};

const detail: MapNodeDetail = {
  node: { id: "method-1", label: "查询改写", type: "Method", kind: "entity", status: "published", summary: "通过改写查询提升召回。", aliases: ["Query Rewriting"], collections: ["rag"] },
  claims: [
    {
      id: "c1",
      statement: "查询改写可以提升检索召回率。",
      claim_type: "entity_definition",
      confidence: null,
      status: "published",
      evidence: [{ id: "ev1", quote: "实验表明查询改写显著提升了召回。", document_id: "doc-1", paper_title: "检索增强生成综述", paper_node_id: "paper-1", page_start: 3, page_end: 3 }],
    },
  ],
  relations: [
    { id: "r1", type: "PRESENTS", direction: "in", status: "published", statement: "论文提出查询改写", other: { id: "paper-1", label: "检索增强生成综述", type: "Paper", status: "published" }, evidence: [] },
  ],
  papers: [{ document_id: "doc-1", node_id: "paper-1", title: "检索增强生成综述", pages: 12, url: null, evidence_count: 1 }],
  review: null,
};

function respond(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
}

function renderPage(initialEntry = "/knowledge/map") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <KnowledgeMapPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("KnowledgeMapPage", () => {
  it("renders approved knowledge by default and drills down to evidence", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.startsWith("/api/knowledge/collections")) return respond([{ slug: "rag", name: "RAG 论文", is_system: false }]);
      if (url.startsWith("/api/knowledge/map/nodes/")) return respond(detail);
      if (url.startsWith("/api/knowledge/map")) return respond(graph);
      throw new Error(`unexpected ${url}`);
    });
    renderPage();

    const canvas = await screen.findByTestId("graph-canvas");
    expect(within(canvas).getByText("查询改写")).toBeInTheDocument();
    const mapCall = fetchMock.mock.calls.map(([input]) => String(input)).find((url) => url.startsWith("/api/knowledge/map?"));
    expect(mapCall).toBeDefined();
    expect(mapCall).not.toContain("include_pending");
    expect(screen.getByLabelText("实体类型图例")).toHaveTextContent("方法");

    await userEvent.click(within(canvas).getByText("查询改写"));
    const panel = await screen.findByTestId("knowledge-map-detail");
    expect(await within(panel).findByText("查询改写可以提升检索召回率。")).toBeInTheDocument();
    expect(within(panel).getByText("实验表明查询改写显著提升了召回。")).toBeInTheDocument();
    expect(within(panel).getByText("第 3 页")).toBeInTheDocument();
    expect(within(panel).getAllByText("《检索增强生成综述》").length).toBeGreaterThan(0);
    expect(within(panel).getByText("已审核")).toBeInTheDocument();
  });

  it("shows an empty state when the knowledge base has no approved knowledge", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.startsWith("/api/knowledge/collections")) return respond([]);
      return respond({ ...graph, nodes: [], edges: [], type_counts: {}, stats: { ...graph.stats, total_nodes: 0, shown_nodes: 0, total_edges: 0, shown_edges: 0 } });
    });
    renderPage();
    expect(await screen.findByText("知识库中还没有已审核的知识")).toBeInTheDocument();
  });

  it("explains pending nodes and edges when pending knowledge is enabled", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      respond(String(input).startsWith("/api/knowledge/collections") ? [] : graph));
    renderPage();
    await screen.findByTestId("graph-canvas");
    const legend = screen.getByLabelText("实体类型图例");
    expect(legend).not.toHaveTextContent("空心环节点");

    await userEvent.click(screen.getByRole("checkbox", { name: "显示待审核" }));
    expect(legend).toHaveTextContent("空心环节点 = 待审核（环色表示实体类型）");
    expect(legend).toHaveTextContent("琥珀色细线 = 待审核关系");

    await userEvent.click(screen.getByRole("checkbox", { name: "显示待审核" }));
    expect(legend).not.toHaveTextContent("琥珀色细线");
  });
});
