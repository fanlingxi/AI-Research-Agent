import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { BookOpen, Filter, Network, Search, X } from "lucide-react";
import { type FormEvent, lazy, Suspense, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { EmptyBlock, ErrorBlock } from "../../components/AsyncState";
import { Button } from "../../components/ui/button";
import { cn } from "../../lib/utils";
import { type KnowledgeMapGraph, knowledgeMapApi, type MapFilters, type MapNode } from "./api";
import type { GraphCanvasHandle } from "./GraphCanvas";
import { NodeDetailPanel } from "./NodeDetailPanel";
import { PENDING_COLOR, sortTypes, typeSwatch, typeLabel } from "./palette";

// sigma + graphology are only fetched when the map is actually rendered.
const GraphCanvas = lazy(() => import("./GraphCanvas"));

const LIMITS = [150, 300, 1000, 3000];
type View = "graph" | "papers";

function useMapFilters() {
  const [params, setParams] = useSearchParams();
  const filters: MapFilters = {
    collection: params.get("collection") ?? "",
    types: params.getAll("type"),
    q: params.get("q") ?? "",
    includePending: params.get("pending") === "1",
    limit: Number(params.get("limit")) || 300,
  };
  const selected = params.get("node");
  const view: View = params.get("view") === "papers" ? "papers" : "graph";
  const update = (changes: Partial<MapFilters> & { node?: string | null; view?: View }) => {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        const set = (key: string, value: string | null | undefined) => (value ? next.set(key, value) : next.delete(key));
        if ("collection" in changes) set("collection", changes.collection);
        if ("q" in changes) set("q", changes.q?.trim());
        if ("includePending" in changes) set("pending", changes.includePending ? "1" : null);
        if ("limit" in changes) set("limit", changes.limit && changes.limit !== 300 ? String(changes.limit) : null);
        if ("types" in changes) {
          next.delete("type");
          for (const type of changes.types ?? []) next.append("type", type);
        }
        if ("node" in changes) set("node", changes.node);
        if ("view" in changes) set("view", changes.view === "papers" ? "papers" : null);
        return next;
      },
      { replace: true },
    );
  };
  return { filters, selected, view, update };
}

function Legend({
  typeCounts,
  activeTypes,
  onToggle,
  showPending,
}: {
  typeCounts: Record<string, number>;
  activeTypes: string[];
  onToggle: (type: string) => void;
  showPending: boolean;
}) {
  const types = sortTypes(Object.keys(typeCounts));
  return (
    <div className="flex flex-wrap items-center gap-1.5" aria-label="实体类型图例">
      {types.map((type) => {
        const active = !activeTypes.length || activeTypes.includes(type);
        return (
          <button
            aria-pressed={activeTypes.includes(type)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition",
              active ? "bg-panel text-ink-soft" : "border-transparent bg-transparent text-muted-ink",
              activeTypes.includes(type) && "border-line-strong",
            )}
            key={type}
            onClick={() => onToggle(type)}
            title={activeTypes.includes(type) ? "取消仅显示此类型" : "只看此类型（可多选）"}
            type="button"
          >
            <span className="size-2.5 rounded-full" style={{ background: active ? typeSwatch(type) : "var(--color-line-strong)" }} />
            {typeLabel(type)}
            <span className="text-muted-ink">{typeCounts[type]}</span>
          </button>
        );
      })}
      <span className="ml-1 hidden items-center gap-3 text-xs text-muted-ink sm:inline-flex">
        <span className="inline-flex items-center gap-1"><span className="inline-block h-px w-5 bg-line-strong" />关系</span>
        <span className="inline-flex items-center gap-1"><span className="inline-block h-px w-5 bg-line-strong" />证据来源</span>
        {showPending ? (
          <span className="inline-flex items-center gap-1">
            <span className="size-2.5 rounded-full bg-panel" style={{ boxShadow: `inset 0 0 0 2px ${PENDING_COLOR}` }} />空心 = 待审核
          </span>
        ) : null}
      </span>
    </div>
  );
}

function PaperView({ data, onSelect, selectedId }: { data: KnowledgeMapGraph; onSelect: (id: string) => void; selectedId: string | null }) {
  const papers = useMemo(() => {
    const byId = new Map(data.nodes.map((node) => [node.id, node]));
    const linked = new Map<string, Set<string>>();
    for (const edge of data.edges) {
      for (const [paper, other] of [[edge.source, edge.target], [edge.target, edge.source]]) {
        if (byId.get(paper)?.type !== "Paper" || byId.get(other)?.type === "Paper") continue;
        if (!linked.has(paper)) linked.set(paper, new Set());
        linked.get(paper)!.add(other);
      }
    }
    return data.nodes
      .filter((node) => node.type === "Paper")
      .map((paper) => {
        const concepts = [...(linked.get(paper.id) ?? [])].map((id) => byId.get(id)!).filter(Boolean);
        const groups = new Map<string, MapNode[]>();
        for (const concept of concepts) groups.set(concept.type, [...(groups.get(concept.type) ?? []), concept]);
        return { paper, total: concepts.length, groups: sortTypes([...groups.keys()]).map((type) => [type, groups.get(type)!] as const) };
      })
      .sort((a, b) => b.total - a.total || a.paper.label.localeCompare(b.paper.label));
  }, [data]);
  if (!papers.length) {
    return <EmptyBlock title="当前范围内没有论文节点">调整筛选（例如取消实体类型限制）后再查看论文视图。</EmptyBlock>;
  }
  return (
    <div className="grid gap-3 p-3 md:grid-cols-2 2xl:grid-cols-3">
      {papers.map(({ paper, total, groups }) => (
        <article className={cn("rounded-xl border bg-panel p-4", selectedId === paper.id && "border-brand ring-1 ring-brand/30")} key={paper.id}>
          <button className="flex w-full items-start gap-2 text-left" onClick={() => onSelect(paper.id)} type="button">
            <BookOpen className="mt-0.5 shrink-0 text-brand" size={16} />
            <span className="font-medium leading-snug hover:text-brand">{paper.label}</span>
          </button>
          <p className="mt-1 pl-6 text-xs text-muted-ink">关联 {total} 个实体</p>
          <div className="mt-3 space-y-2 pl-6">
            {groups.map(([type, nodes]) => (
              <div key={type}>
                <p className="text-[11px] font-medium text-muted-ink">{typeLabel(type)}</p>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {nodes.map((node) => (
                    <button
                      className={cn(
                        "inline-flex max-w-full items-center gap-1 rounded-md border px-2 py-0.5 text-xs text-ink-soft hover:border-brand/50",
                        node.status === "pending" && "border-dashed border-amber-300 bg-amber-50/50",
                        selectedId === node.id && "border-brand bg-brand-soft text-brand",
                      )}
                      key={node.id}
                      onClick={() => onSelect(node.id)}
                      type="button"
                    >
                      <span className="size-2 shrink-0 rounded-full" style={{ background: typeSwatch(node.type) }} />
                      <span className="truncate">{node.label}</span>
                      {node.status === "pending" ? <span className="text-amber-700">待审核</span> : null}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </article>
      ))}
    </div>
  );
}

function GraphFallback({ label = "加载图谱组件…" }: { label?: string }) {
  return (
    <div className="grid h-full place-items-center text-sm text-muted-ink" role="status">
      <div className="flex flex-col items-center gap-3">
        <Network className="animate-pulse text-muted-ink" size={36} />
        {label}
      </div>
    </div>
  );
}

export function KnowledgeMapPage() {
  const { filters, selected, view, update } = useMapFilters();
  const [searchText, setSearchText] = useState(filters.q);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [sheetCollapsed, setSheetCollapsed] = useState(false);
  const canvasRef = useRef<GraphCanvasHandle>(null);
  const graphSectionRef = useRef<HTMLElement>(null);

  const collections = useQuery({ queryKey: ["knowledge-map-collections"], queryFn: knowledgeMapApi.collections });
  const graph = useQuery({
    queryKey: ["knowledge-map", filters],
    queryFn: () => knowledgeMapApi.graph(filters),
    placeholderData: keepPreviousData,
  });
  const data = graph.data;

  const suggestions = useMemo(() => {
    const needle = searchText.trim().toLocaleLowerCase();
    if (!needle || !data) return [];
    return data.nodes
      .filter((node) => node.label.toLocaleLowerCase().includes(needle))
      .map((node) => {
        const label = node.label.toLocaleLowerCase();
        // exact > prefix > contains; approved before pending; then importance.
        const rank = (label === needle ? 0 : label.startsWith(needle) ? 1 : 2) * 2 + (node.status === "pending" ? 1 : 0);
        return { node, rank };
      })
      .sort((a, b) => a.rank - b.rank || b.node.score - a.node.score)
      .map(({ node }) => node)
      .slice(0, 8);
  }, [data, searchText]);

  const select = (nodeId: string | null) => {
    setSheetCollapsed(false);
    update({ node: nodeId });
  };
  const focus = (nodeId: string) => {
    update({ node: nodeId, view: "graph" });
    // On narrow screens the detail sheet covers the graph: collapse it and bring the graph into view.
    if (typeof window.matchMedia === "function" && window.matchMedia("(max-width: 1023px)").matches) {
      setSheetCollapsed(true);
      graphSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    window.setTimeout(() => canvasRef.current?.focus(nodeId), view === "graph" ? 0 : 400);
  };
  const pick = (node: MapNode) => {
    setShowSuggestions(false);
    setSearchText(node.label);
    focus(node.id);
  };
  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    if (suggestions[0] && suggestions[0].label === searchText.trim()) return pick(suggestions[0]);
    setShowSuggestions(false);
    update({ q: searchText, node: null });
  };
  const toggleType = (type: string) =>
    update({ types: filters.types.includes(type) ? filters.types.filter((item) => item !== type) : [...filters.types, type] });
  const clearFilters = () => {
    setSearchText("");
    update({ types: [], q: "", collection: "", node: null });
  };

  const hasFilters = Boolean(filters.types.length || filters.q || filters.collection);
  const paperCount = data?.nodes.filter((node) => node.type === "Paper").length ?? 0;

  const controls = (
    <div className={cn("flex-wrap items-center gap-2", filtersOpen ? "flex" : "hidden md:flex")}>
      <label className="sr-only" htmlFor="knowledge-map-collection">知识集合</label>
      <select
        className="h-9 min-w-0 max-w-full rounded-lg border bg-panel px-2.5 text-sm outline-none focus:border-brand sm:max-w-56"
        id="knowledge-map-collection"
        onChange={(event) => update({ collection: event.target.value, node: null })}
        value={filters.collection}
      >
        <option value="">全部知识集合</option>
        {collections.data?.map((item) => <option key={item.slug} value={item.slug}>{item.name}</option>)}
      </select>
      <label className="inline-flex h-9 cursor-pointer items-center gap-2 rounded-lg border bg-panel px-3 text-sm text-ink-soft">
        <input
          checked={filters.includePending}
          className="size-4 accent-amber-600"
          onChange={(event) => update({ includePending: event.target.checked, node: null })}
          type="checkbox"
        />
        显示待审核
      </label>
      <label className="sr-only" htmlFor="knowledge-map-limit">节点上限</label>
      <select
        className="h-9 rounded-lg border bg-panel px-2.5 text-sm outline-none focus:border-brand"
        id="knowledge-map-limit"
        onChange={(event) => update({ limit: Number(event.target.value) })}
        value={filters.limit}
      >
        {LIMITS.map((limit) => <option key={limit} value={limit}>最多 {limit} 个节点</option>)}
      </select>
    </div>
  );

  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4 lg:p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-brand">知识库</p>
          <h1 className="mt-0.5 text-2xl font-semibold tracking-tight">知识地图</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-ink">
            浏览已审核的概念、方法与论文如何相互关联；点击任一节点，追溯到原文证据与页码。
          </p>
        </div>
        {data ? (
          <dl className="flex gap-4 text-sm">
            <div><dt className="text-xs text-muted-ink">实体</dt><dd className="font-semibold tabular-nums">{data.stats.shown_nodes - paperCount}</dd></div>
            <div><dt className="text-xs text-muted-ink">论文</dt><dd className="font-semibold tabular-nums">{paperCount}</dd></div>
            <div><dt className="text-xs text-muted-ink">连接</dt><dd className="font-semibold tabular-nums">{data.stats.shown_edges}</dd></div>
            {filters.includePending ? <div><dt className="text-xs text-amber-700">待审核</dt><dd className="font-semibold tabular-nums text-amber-800">{data.stats.pending_nodes + data.stats.pending_edges}</dd></div> : null}
          </dl>
        ) : null}
      </header>

      <div className="flex flex-col gap-2 rounded-xl border bg-panel p-3 shadow-sm">
        <div className="flex flex-wrap items-center gap-2">
          <form className="relative min-w-0 flex-1 basis-60" onSubmit={submitSearch} role="search">
            <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-ink" size={15} />
            <input
              aria-label="搜索并定位节点"
              className="h-9 w-full rounded-lg border bg-panel pl-9 pr-3 text-sm outline-none focus:border-brand"
              onBlur={() => window.setTimeout(() => setShowSuggestions(false), 150)}
              onChange={(event) => {
                setSearchText(event.target.value);
                setShowSuggestions(true);
              }}
              onFocus={() => setShowSuggestions(true)}
              placeholder="搜索概念、方法或论文，回车筛选相关子图"
              value={searchText}
            />
            {showSuggestions && suggestions.length ? (
              <ul className="absolute left-0 right-0 top-10 z-30 max-h-72 overflow-y-auto rounded-lg border bg-panel py-1 shadow-lg" role="listbox">
                {suggestions.map((node) => (
                  <li key={node.id}>
                    <button
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-subtle"
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => pick(node)}
                      role="option"
                      aria-selected={selected === node.id}
                      type="button"
                    >
                      <span className="size-2.5 shrink-0 rounded-full" style={{ background: typeSwatch(node.type) }} />
                      <span className="min-w-0 flex-1 truncate">{node.label}</span>
                      <span className="shrink-0 text-xs text-muted-ink">{typeLabel(node.type)}{node.status === "pending" ? " · 待审核" : ""}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </form>
          <div className="flex rounded-lg border bg-panel p-0.5 text-sm" role="tablist" aria-label="视图">
            <button aria-selected={view === "graph"} className={cn("inline-flex h-8 items-center gap-1.5 rounded-md px-3", view === "graph" ? "bg-brand-soft font-medium text-brand" : "text-ink-soft")} onClick={() => update({ view: "graph" })} role="tab" type="button"><Network size={14} />关系图</button>
            <button aria-selected={view === "papers"} className={cn("inline-flex h-8 items-center gap-1.5 rounded-md px-3", view === "papers" ? "bg-brand-soft font-medium text-brand" : "text-ink-soft")} onClick={() => update({ view: "papers" })} role="tab" type="button"><BookOpen size={14} />按论文</button>
          </div>
          <Button className="md:hidden" onClick={() => setFiltersOpen((value) => !value)} size="sm" variant="outline"><Filter size={14} />筛选</Button>
          {controls}
        </div>
        {data ? <Legend activeTypes={filters.types} onToggle={toggleType} showPending={filters.includePending} typeCounts={data.type_counts} /> : null}
        {hasFilters || data?.stats.truncated ? (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-ink">
            {filters.q ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-brand-soft px-2 py-0.5 text-brand">
                关键词：{filters.q}及其直接关联
                <button aria-label="清除关键词" onClick={() => { setSearchText(""); update({ q: "" }); }} type="button"><X size={12} /></button>
              </span>
            ) : null}
            {data?.stats.truncated ? (
              <span>
                按重要度（连接数 + 证据数）显示前 {data.stats.shown_nodes} / {data.stats.total_nodes} 个节点
                {filters.limit < 3000 ? (
                  <button className="ml-1 font-medium text-brand hover:underline" onClick={() => update({ limit: LIMITS.find((limit) => limit > filters.limit) ?? 3000 })} type="button">显示更多</button>
                ) : null}
              </span>
            ) : null}
            {hasFilters ? <button className="font-medium text-brand hover:underline" onClick={clearFilters} type="button">清除筛选</button> : null}
          </div>
        ) : null}
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <section
          ref={graphSectionRef}
          className={cn(
            "relative overflow-hidden rounded-xl border bg-panel shadow-sm",
            view === "graph" ? "h-[62vh] min-h-[380px] lg:h-[calc(100vh-260px)] lg:min-h-[520px]" : "min-h-[380px]",
          )}
          aria-busy={graph.isFetching}
        >
          {graph.isPending ? <GraphFallback label="正在读取知识库…" /> : null}
          {graph.error instanceof Error && !data ? (
            <div className="p-4"><ErrorBlock error={graph.error} onRetry={() => void graph.refetch()} /></div>
          ) : null}
          {data && !data.nodes.length ? (
            <div className="grid h-full place-items-center p-4">
              {hasFilters ? (
                <EmptyBlock title="没有匹配的知识">换个关键词，或<button className="font-medium text-brand hover:underline" onClick={clearFilters} type="button">清除筛选</button>后再试。</EmptyBlock>
              ) : (
                <EmptyBlock title="知识库中还没有已审核的知识">
                  先在<Link className="font-medium text-brand hover:underline" to="/knowledge">知识库</Link>导入论文，并在<Link className="font-medium text-brand hover:underline" to="/review">审核中心</Link>批准候选。
                  {!filters.includePending ? <> 也可以勾选“显示待审核”预览尚未批准的候选。</> : null}
                </EmptyBlock>
              )}
            </div>
          ) : null}
          {data && data.nodes.length ? (
            view === "graph" ? (
              <Suspense fallback={<GraphFallback />}>
                <GraphCanvas data={data} onSelect={select} ref={canvasRef} selectedId={selected} />
              </Suspense>
            ) : (
              <PaperView data={data} onSelect={select} selectedId={selected} />
            )
          ) : null}
          {graph.isFetching && data ? <div className="absolute inset-x-0 top-0 h-0.5 animate-pulse bg-brand/60" /> : null}
        </section>

        <aside
          className={cn(
            "rounded-xl border bg-panel shadow-sm",
            selected
              ? "fixed inset-x-0 bottom-0 z-40 max-h-[72vh] overflow-hidden rounded-b-none shadow-2xl lg:static lg:z-auto lg:max-h-none lg:rounded-b-xl lg:shadow-sm"
              : "hidden lg:block",
            "lg:h-[calc(100vh-260px)] lg:min-h-[520px]",
          )}
        >
          {selected ? (
            <>
            {sheetCollapsed ? (
              <button className="flex w-full items-center justify-between gap-3 px-5 py-3 text-left lg:hidden" onClick={() => setSheetCollapsed(false)} type="button">
                <span className="min-w-0 truncate text-sm font-medium">{data?.nodes.find((node) => node.id === selected)?.label ?? "节点详情"}</span>
                <span className="shrink-0 text-xs font-medium text-brand">展开详情</span>
              </button>
            ) : null}
            <div className={cn("h-full max-h-[72vh] flex-col lg:flex lg:max-h-none", sheetCollapsed ? "hidden" : "flex")}>
              <div className="mx-auto mt-2 h-1 w-10 rounded-full bg-line-strong lg:hidden" />
              <NodeDetailPanel
                includePending={filters.includePending}
                nodeId={selected}
                onClose={() => select(null)}
                onLocate={focus}
                onSelect={select}
              />
            </div>
            </>
          ) : (
            <div className="flex h-full flex-col justify-center gap-3 p-6 text-sm text-muted-ink">
              <Network className="text-muted-ink" size={32} />
              <p className="font-medium text-ink">点击节点查看证据</p>
              <ul className="list-disc space-y-1.5 pl-4 leading-6">
                <li>节点大小表示连接数与证据数，颜色表示实体类型。</li>
                <li>悬停连线查看关系说明；浅色连线表示该论文为实体提供了原文证据。</li>
                <li>默认只显示已审核知识；勾选“显示待审核”可预览候选（空心节点）。</li>
              </ul>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

export default KnowledgeMapPage;
