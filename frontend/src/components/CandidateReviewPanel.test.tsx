import { emptyIngestionStatistics, historyPage } from "../test/historyFixtures";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  api,
  type BulkCandidateDecisionResult,
  type CandidateDecisionResult,
  type DuplicateGroups,
  type EntityCandidate,
  type KnowledgeCandidatePage,
  type KnowledgeIngestion,
} from "../lib/api";
import { CandidateReviewPanel } from "./CandidateReviewPanel";

const ingestion: KnowledgeIngestion = {
  id: "ingestion-1",
  topic: "检索增强",
  topic_slug: "rag",
  collection: "检索增强",
  collection_slug: "rag",
  sources: ["a.pdf", "b.pdf"],
  pdf_max_pages: 20,
  status: "needs_review",
  document_count: 2,
  candidate_count: 4,
  published_count: 1,
  queue_position: null,
  job_attempts: 1,
  error: null,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const target = { entity_id: "entity-rag", name: "检索增强生成", type: "Method", similarity: 1, match_kind: "exact" };

function entity(id: string, paper: string, extra: Partial<EntityCandidate> = {}): EntityCandidate {
  return {
    id,
    ingestion_id: ingestion.id,
    topic_slug: "rag",
    name: "检索增强生成",
    type: "Method",
    summary: "先检索再生成的方法。",
    confidence: 0.9,
    status: "draft",
    evidence: { paper_id: paper, chunk_id: `${paper}:chunk`, page_start: 2, page_end: 2, quote: "检索增强生成把检索到的证据交给模型。" },
    merge_suggestions: [target],
    ...extra,
  };
}

function renderPanel(initialIngestionId?: string, collectionSlug?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><CandidateReviewPanel collectionSlug={collectionSlug} initialIngestionId={initialIngestionId} /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => vi.restoreAllMocks());

describe("candidate merge review", () => {
  it("resolves an old collection's pending batch without opening another collection", async () => {
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion], 105));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "ingestionStatistics").mockResolvedValue({ ...emptyIngestionStatistics, pending_ingestions: [{ id: "old-collection-batch", collection: "旧集合", collection_slug: "old" }] });
    const direct = vi.spyOn(api, "ingestion").mockResolvedValue({ ...ingestion, id: "old-collection-batch", collection: "旧集合", collection_slug: "old" });
    const candidates = vi.spyOn(api, "candidatePage").mockResolvedValue({ items: [], total: 0, offset: 0, limit: 25, next_offset: null });
    vi.spyOn(api, "duplicateGroups").mockResolvedValue({ groups: [], candidate_count: 0 });
    renderPanel(undefined, "old");
    await waitFor(() => expect(candidates).toHaveBeenCalledWith("old-collection-batch", expect.any(Object)));
    expect(direct).toHaveBeenCalledWith("old-collection-batch");
    expect(candidates.mock.calls.every(([id]) => id === "old-collection-batch")).toBe(true);
    expect(await screen.findByRole("option", { name: /旧集合/ })).toBeInTheDocument();
  });

  it("reads an old linked ingestion directly without selecting the latest batch", async () => {
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion], 105));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    let resolveOld!: (value: KnowledgeIngestion) => void;
    const direct = vi.spyOn(api, "ingestion").mockReturnValue(new Promise((resolve) => { resolveOld = resolve; }));
    const candidates = vi.spyOn(api, "candidatePage").mockResolvedValue({ items: [], total: 0, offset: 0, limit: 25, next_offset: null });
    const duplicates = vi.spyOn(api, "duplicateGroups").mockResolvedValue({ groups: [], candidate_count: 0 });
    renderPanel("ingestion-old");
    expect(await screen.findByText("读取指定的导入记录…")).toBeInTheDocument();
    expect(direct).toHaveBeenCalledWith("ingestion-old");
    expect(candidates).not.toHaveBeenCalled();
    expect(duplicates).not.toHaveBeenCalled();
    resolveOld({ ...ingestion, id: "ingestion-old", collection: "旧资料", candidate_count: 0, status: "completed" });
    await waitFor(() => expect(candidates).toHaveBeenCalledWith("ingestion-old", expect.any(Object)));
    expect(candidates.mock.calls.every(([id]) => id === "ingestion-old")).toBe(true);
    expect(duplicates).toHaveBeenCalledWith("ingestion-old");
    expect(await screen.findByRole("option", { name: /旧资料/ })).toBeInTheDocument();
  });

  it("shows a Chinese error for an unreadable linked batch and never falls back", async () => {
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "ingestion").mockRejectedValue(new Error("Knowledge ingestion not found."));
    const candidates = vi.spyOn(api, "candidatePage");
    const duplicates = vi.spyOn(api, "duplicateGroups");
    renderPanel("missing");
    expect(await screen.findByText("无法读取指定的导入记录，请确认记录存在后重试。")).toBeInTheDocument();
    expect(candidates).not.toHaveBeenCalled();
    expect(duplicates).not.toHaveBeenCalled();
    expect(screen.queryByText("Knowledge ingestion not found.")).not.toBeInTheDocument();
  });

  it("can load older reviewable batches when the first page has none", async () => {
    const pages = vi.spyOn(api, "ingestionPage")
      .mockResolvedValueOnce(historyPage([{ ...ingestion, status: "completed", candidate_count: 0 }], 26))
      .mockResolvedValueOnce(historyPage([{ ...ingestion, id: "older", collection: "旧批次" }], 26, 25));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "candidatePage").mockResolvedValue({ items: [], total: 0, offset: 0, limit: 25, next_offset: null });
    vi.spyOn(api, "duplicateGroups").mockResolvedValue({ groups: [], candidate_count: 0 });
    renderPanel();
    expect(await screen.findByText("还没有可审核的资料")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "加载更多导入记录" }));
    expect(await screen.findByText("旧批次")).toBeInTheDocument();
    expect(pages).toHaveBeenLastCalledWith(25);
    expect(screen.queryByRole("button", { name: "加载更多导入记录" })).not.toBeInTheDocument();
  });

  it.each([
    ["single", false], ["bulk", false], ["single", true], ["bulk", true],
  ])("returns to a valid page after the last item on page two is reviewed (%s, direct link: %s)", async (mode, directLink) => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage(directLink ? [] : [ingestion]));
    const direct = vi.spyOn(api, "ingestion").mockResolvedValue(ingestion);
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "duplicateGroups").mockResolvedValue({ groups: [], candidate_count: 0 });
    let reviewed = false;
    const candidates = vi.spyOn(api, "candidatePage").mockImplementation(async (_id, params) => {
      const offset = params?.offset ?? 0;
      return {
        items: offset === 25 && reviewed ? [] : [{ kind: "entity", candidate: entity(offset ? "last" : "first", "paper:a", { name: offset ? "末页候选" : "首页候选", merge_suggestions: [] }) }],
        total: reviewed ? 25 : 26, offset, limit: 25, next_offset: !reviewed && !offset ? 25 : null,
      };
    });
    vi.spyOn(api, "decideCandidate").mockImplementation(async () => { reviewed = true; return {} as CandidateDecisionResult; });
    vi.spyOn(api, "decideCandidatesBulk").mockImplementation(async () => {
      reviewed = true;
      return { decision: "approve", applied: 1, replayed: 0, skipped: [] } as unknown as BulkCandidateDecisionResult;
    });
    renderPanel(directLink ? ingestion.id : undefined);
    await user.click(await screen.findByRole("button", { name: "下一页" }));
    await screen.findByText("末页候选");
    if (mode === "single") {
      await user.click(screen.getByRole("button", { name: "批准" }));
    } else {
      await user.click(screen.getByRole("checkbox", { name: "全选当前页候选" }));
      await user.click(screen.getByRole("button", { name: "批量批准" }));
    }
    await screen.findByText("首页候选");
    await waitFor(() => expect(screen.queryByText("末页候选")).not.toBeInTheDocument());
    expect(candidates.mock.calls.at(-1)?.[1]?.offset).toBe(0);
    expect(candidates.mock.calls.every(([id]) => id === ingestion.id)).toBe(true);
    if (directLink) expect(direct).toHaveBeenCalledWith(ingestion.id);
  });

  it("merges an entity candidate into the suggested existing entity", async () => {
    const page: KnowledgeCandidatePage = { items: [{ kind: "entity", candidate: entity("candidate-b", "paper:b") }], total: 1, offset: 0, limit: 25, next_offset: null };
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "candidatePage").mockResolvedValue(page);
    vi.spyOn(api, "duplicateGroups").mockResolvedValue({ groups: [], candidate_count: 0 });
    const decide = vi.spyOn(api, "decideCandidate").mockResolvedValue({} as CandidateDecisionResult);

    renderPanel();

    expect(await screen.findByText("可能重复 · 可合并到已有实体")).toBeInTheDocument();
    expect(screen.getByRole("radio")).toBeChecked();
    expect(screen.getByRole("button", { name: /作为新实体批准/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /合并到「检索增强生成」/ }));

    await waitFor(() => expect(decide).toHaveBeenCalledWith("candidate-b", "merge", "entity-rag"));
    expect(await screen.findByText(/已合并到已有实体/)).toBeInTheDocument();
  });

  it("labels a suggestion of a different but interchangeable type and still requires an explicit merge", async () => {
    const methodTarget = { ...target, type_differs: true };
    const candidate = entity("candidate-c", "paper:c", { type: "Concept", merge_suggestions: [methodTarget] });
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "candidatePage").mockResolvedValue({ items: [{ kind: "entity", candidate }], total: 1, offset: 0, limit: 25, next_offset: null });
    vi.spyOn(api, "duplicateGroups").mockResolvedValue({ groups: [], candidate_count: 0 });
    const decide = vi.spyOn(api, "decideCandidate").mockResolvedValue({} as CandidateDecisionResult);

    renderPanel();

    expect(await screen.findByText("类型不同（概念/方法）")).toBeInTheDocument();
    expect(screen.getByText(/所选实体类型为「方法」，合并后保留这个类型/)).toBeInTheDocument();
    expect(decide).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /合并到「检索增强生成」/ }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith("candidate-c", "merge", "entity-rag"));
  });

  it("works through a duplicate group from the 疑似重复 view", async () => {
    const groups: DuplicateGroups = {
      candidate_count: 3,
      groups: [
        { key: "entity:entity-rag", name: "检索增强生成", type: "Method", target, candidates: [entity("candidate-b", "paper:b"), entity("candidate-c", "paper:c")] },
        { key: "draft:Concept:大语言模型", name: "大语言模型", type: "Concept", target: null, candidates: [entity("candidate-d", "paper:d", { name: "大语言模型", type: "Concept", merge_suggestions: [] }), entity("candidate-e", "paper:e", { name: "大语言模型", type: "Concept", merge_suggestions: [] })] },
      ],
    };
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "candidatePage").mockResolvedValue({ items: [], total: 0, offset: 0, limit: 25, next_offset: null });
    vi.spyOn(api, "duplicateGroups").mockResolvedValue(groups);
    const bulkResult = { ingestion, decision: "merge", requested: 2, applied: 2, replayed: 0, skipped: [] } as BulkCandidateDecisionResult;
    const bulk = vi.spyOn(api, "decideCandidatesBulk").mockResolvedValue(bulkResult);
    const decide = vi.spyOn(api, "decideCandidate").mockResolvedValue({
      candidate: { kind: "entity", candidate: { ...entity("candidate-d", "paper:d"), canonical_id: "entity-llm" } },
      applied: true,
      replayed: false,
      projection_status: "queued",
    } as CandidateDecisionResult);

    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: /疑似重复/ }));
    const ragGroup = await screen.findByRole("article", { name: "疑似重复：检索增强生成" });
    expect(within(ragGroup).getByText("2 条候选 · 2 篇文献")).toBeInTheDocument();
    await userEvent.click(within(ragGroup).getByRole("button", { name: /全部合并到「检索增强生成」/ }));
    await waitFor(() => expect(bulk).toHaveBeenCalledWith("ingestion-1", ["candidate-b", "candidate-c"], "merge", "entity-rag"));
    expect(await screen.findByText(/2 条候选已合并到「检索增强生成」/)).toBeInTheDocument();

    const llmGroup = screen.getByRole("article", { name: "疑似重复：大语言模型" });
    await userEvent.click(within(llmGroup).getAllByRole("button", { name: /设为规范实体并合并其余/ })[0]);
    await waitFor(() => expect(decide).toHaveBeenCalledWith("candidate-d", "approve"));
    await waitFor(() => expect(bulk).toHaveBeenLastCalledWith("ingestion-1", ["candidate-e"], "merge", "entity-llm"));
  });

  it("merges every exact same-name, same-type group after one confirmation", async () => {
    const tool = { entity_id: "entity-tool", name: "工具调用", type: "Paper", similarity: 1, match_kind: "exact" };
    const near = { entity_id: "entity-near", name: "检索增强", type: "Method", similarity: 0.9, match_kind: "similar" };
    const groups: DuplicateGroups = {
      candidate_count: 4,
      groups: [
        { key: "entity:entity-rag", name: "检索增强生成", type: "Method", target, candidates: [entity("c-rag", "paper:b")] },
        { key: "entity:entity-tool", name: "工具调用", type: "Method", target: tool, candidates: [entity("c-tool", "paper:b", { name: "工具调用", type: "Paper" })] },
        { key: "entity:entity-near", name: "检索增强", type: "Method", target: near, candidates: [entity("c-near", "paper:b", { name: "检索增强方法" })] },
        { key: "entity:cross", name: "工具调用", type: "Method", target: tool, candidates: [entity("c-cross", "paper:c", { name: "工具调用", type: "Concept" })] },
      ],
    };
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "candidatePage").mockResolvedValue({ items: [], total: 0, offset: 0, limit: 25, next_offset: null });
    vi.spyOn(api, "duplicateGroups").mockResolvedValue(groups);
    const bulk = vi.spyOn(api, "decideCandidatesBulk").mockResolvedValue({ ingestion, decision: "merge", requested: 1, applied: 1, replayed: 0, skipped: [] } as BulkCandidateDecisionResult);

    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: /疑似重复/ }));
    await userEvent.click(await screen.findByRole("button", { name: "全部合并到同名实体" }));
    expect(bulk).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "确认合并" }));
    await waitFor(() => expect(bulk).toHaveBeenCalledTimes(2));
    expect(bulk).toHaveBeenCalledWith("ingestion-1", ["c-rag"], "merge", "entity-rag");
    expect(bulk).toHaveBeenCalledWith("ingestion-1", ["c-tool"], "merge", "entity-tool");
    expect(await screen.findByText(/2 组同名候选中 2 条已合并/)).toBeInTheDocument();
  });

  it("warns when the evidence is a chunk excerpt because the model quote was not found", async () => {
    const items: KnowledgeCandidatePage["items"] = [
      { kind: "entity", candidate: entity("located", "paper:a", { merge_suggestions: [] }) },
      { kind: "entity", candidate: entity("unlocated", "paper:b", { name: "未定位实体", merge_suggestions: [], metadata: { evidence_located: false } }) },
    ];
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "candidatePage").mockResolvedValue({ items, total: 2, offset: 0, limit: 25, next_offset: null });
    vi.spyOn(api, "duplicateGroups").mockResolvedValue({ groups: [], candidate_count: 0 });

    renderPanel();

    const notes = await screen.findAllByRole("note");
    expect(notes).toHaveLength(1);
    expect(notes[0]).toHaveTextContent("模型给出的引文未能在原文中找到");
    expect(notes[0].closest("article")).toHaveTextContent("未定位实体");
  });

  it("runs AI pre-review in batches after confirmation and reports the outcome", async () => {
    vi.spyOn(api, "ingestionPage").mockResolvedValue(historyPage([ingestion]));
    vi.spyOn(api, "collections").mockResolvedValue([]);
    vi.spyOn(api, "candidatePage").mockResolvedValue({ items: [{ kind: "entity", candidate: entity("left", "paper:a", { merge_suggestions: [], metadata: { prereview: { verdict: "unsure", reason: "引文只提到名称" } } }) }], total: 1, offset: 0, limit: 25, next_offset: null });
    vi.spyOn(api, "duplicateGroups").mockResolvedValue({ groups: [], candidate_count: 0 });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const prereview = vi.spyOn(api, "aiPrereview")
      .mockResolvedValueOnce({ approved: 3, merged: 1, rejected: 2, unsure: 1, remaining: 4, llm_calls: 1, items: [{ candidate_id: "a", name: "a", verdict: "approved", reason: "" }] })
      .mockResolvedValueOnce({ approved: 1, merged: 0, rejected: 1, unsure: 2, remaining: 0, llm_calls: 1, items: [{ candidate_id: "b", name: "b", verdict: "rejected", reason: "" }] });

    renderPanel();

    expect(await screen.findByText(/AI 预审未能确定：引文只提到名称/)).toBeInTheDocument();
    await userEvent.click(screen.getByText("批量快捷处理"));
    await userEvent.click(screen.getByRole("button", { name: "AI 预审" }));
    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(prereview).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/批准 4 条、合并到已有实体 1 条、驳回 3 条，3 条拿不准留给你审核/)).toBeInTheDocument();
  });
});
