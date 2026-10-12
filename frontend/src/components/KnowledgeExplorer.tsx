import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Check, Search } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

import { api, type KnowledgeSearchResult } from "../lib/api";
import { documentTitle, usePaperTitles } from "../lib/hooks";
import { relationTypeLabel } from "../lib/labels";
import { cn } from "../lib/utils";
import { EmptyBlock, ErrorBlock } from "./AsyncState";
import { pageLabel } from "./report/SourceDrawer";
import { Button } from "./ui/button";
import { Input } from "./ui/field";

function evidenceExcerpt(text: string, maximumLength = 360) {
  const normalized = text.replace(/\s+/g, " ").trim();
  return normalized.length <= maximumLength ? normalized : `${normalized.slice(0, maximumLength).trimEnd()}…`;
}

function SearchResults({ result }: { result: KnowledgeSearchResult }) {
  const titles = usePaperTitles();
  const relations = result.graph.filter((relation) => relation.edge_id && relation.source_name && relation.target_name && relation.relation_type);
  return (
    <div className="mt-5 space-y-5">
      {result.warnings?.length ? <div className="flex gap-2 rounded-lg border border-amber-200 bg-warning-soft p-3 text-[13px] text-amber-950" role="status"><AlertTriangle className="mt-0.5 shrink-0" size={15} /><p>{result.warnings.join(" ")}</p></div> : null}
      <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_280px]">
        <section className="min-w-0">
          <h3 className="mb-2 text-[13px] font-medium text-muted-ink">原文证据 · {result.evidence.length}</h3>
          <div className="space-y-2.5">
            {result.evidence.map((evidence) => (
              <article className="rounded-lg border bg-panel p-3.5" key={`${evidence.id}:${evidence.chunk_id}`}>
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                  <p className="min-w-0 text-[13.5px] font-medium text-ink">{documentTitle(evidence.title, evidence.paper_id, titles)}</p>
                  <span className="text-xs tabular-nums text-muted-ink">{pageLabel(evidence.page_start, evidence.page_end)} · 相关度 {Math.round(evidence.score * 100)}%</span>
                </div>
                <p className="mt-2 text-[13px] leading-6 text-ink-soft">{evidenceExcerpt(evidence.text)}</p>
              </article>
            ))}
            {!result.evidence.length ? <EmptyBlock className="py-7" title="没有找到相关原文">换个说法试试，或确认相关资料已审核发布。</EmptyBlock> : null}
          </div>
        </section>
        <section className="min-w-0">
          <h3 className="mb-2 text-[13px] font-medium text-muted-ink">相关的知识关系 · {relations.length}</h3>
          <div className="space-y-2">
            {relations.map((relation, index) => (
              <div className="rounded-lg border bg-panel px-3 py-2.5 text-[13px]" key={`${relation.edge_id ?? index}:${relation.source_name}:${relation.target_name}`}>
                <span className="font-medium text-ink">{relation.source_name}</span>
                <span className="mx-1.5 inline-flex items-center gap-1 text-xs text-brand">{relationTypeLabel(relation.relation_type)}<ArrowRight size={11} /></span>
                <span className="font-medium text-ink">{relation.target_name}</span>
              </div>
            ))}
            {!relations.length ? <p className="rounded-lg border border-dashed p-3 text-[13px] text-muted-ink">没有匹配的已发布关系。关系只辅助阅读，不影响报告的证据约束。</p> : null}
          </div>
        </section>
      </div>
    </div>
  );
}

/** Search approved evidence within explicitly chosen collections. */
export function KnowledgeExplorer({ collections, title = "检索证据", description }: {
  collections: Array<{ slug: string; name: string }>;
  title?: string;
  description?: string;
}) {
  const [searchQuery, setSearchQuery] = useState("");
  const [selected, setSelected] = useState<string[]>(() => collections.map((item) => item.slug));
  const key = collections.map((item) => item.slug).join("|");
  useEffect(() => { setSelected(collections.map((item) => item.slug)); }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const search = useMutation({ mutationFn: () => api.searchKnowledge(searchQuery.trim(), selected) });
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (searchQuery.trim().length >= 2 && selected.length) search.mutate();
  };
  return (
    <section className="min-w-0 rounded-xl border bg-panel p-4 shadow-[var(--shadow-card)] sm:p-5" aria-label={title}>
      <h2 className="text-[15px] font-semibold">{title}</h2>
      <p className="mt-0.5 text-[13px] leading-5 text-muted-ink">{description ?? "在已审核发布的知识中查找原文片段，用于浏览和核对；不会改变任何研究结果。"}</p>
      {collections.length ? (
        <>
          {collections.length > 1 ? (
            <div className="mt-3 flex flex-wrap gap-1.5" role="group" aria-label="检索范围">
              {collections.map((collection) => {
                const on = selected.includes(collection.slug);
                return (
                  <button
                    aria-pressed={on}
                    className={cn("inline-flex h-7 items-center gap-1 rounded-full border px-2.5 text-xs", on ? "border-brand bg-brand-soft text-brand-strong" : "border-line-strong text-muted-ink hover:text-ink")}
                    key={collection.slug}
                    onClick={() => setSelected((current) => on ? current.filter((slug) => slug !== collection.slug) : [...current, collection.slug])}
                    type="button"
                  >{on ? <Check size={12} /> : null}{collection.name}</button>
                );
              })}
            </div>
          ) : null}
          <form className="mt-3 flex gap-2" onSubmit={submit}>
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-ink" size={15} />
              <Input aria-label="检索知识" className="pl-9" minLength={2} placeholder="输入问题或关键词，例如：查询扩展" required value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} />
            </div>
            <Button disabled={!selected.length || search.isPending} type="submit">{search.isPending ? "检索中…" : "检索"}</Button>
          </form>
        </>
      ) : (
        <EmptyBlock className="mt-4 py-7" title="没有可检索的知识集合">先导入资料并完成审核，或为项目选择知识范围。</EmptyBlock>
      )}
      {search.error instanceof Error ? <div className="mt-4"><ErrorBlock error={search.error} /></div> : null}
      {search.data ? <SearchResults result={search.data} /> : null}
    </section>
  );
}
