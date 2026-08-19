import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, Network, Search, ShieldCheck } from "lucide-react";
import { type FormEvent, useState } from "react";

import { api, type KnowledgeSearchResult } from "../lib/api";
import { EmptyBlock, ErrorBlock } from "./AsyncState";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader } from "./ui/card";

function evidenceExcerpt(text: string, maximumLength = 520) {
  const normalized = text.replace(/\s+/g, " ").trim();
  return normalized.length <= maximumLength ? normalized : `${normalized.slice(0, maximumLength).trimEnd()}…`;
}

function SearchResults({ result }: { result: KnowledgeSearchResult }) {
  const relations = result.graph.filter((relation) => relation.edge_id && relation.source_name && relation.target_name && relation.relation_type);
  return (
    <div className="mt-5">
      {result.warnings?.length ? <div className="mb-5 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950" role="status"><div className="flex gap-2"><AlertTriangle className="shrink-0" size={18} /><p>{result.warnings.join(" ")}</p></div></div> : null}
      <div className="grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
      <Card>
        <CardHeader>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Evidence</p>
            <h3 className="mt-1 font-semibold">Retrieved source passages</h3>
          </div>
          <span className="text-xs text-muted-ink">{result.evidence.length}</span>
        </CardHeader>
        <CardContent className="space-y-3">
          {result.evidence.map((evidence) => (
            <article className="rounded-lg border bg-white p-3" key={`${evidence.id}:${evidence.chunk_id}`}>
              <div className="flex items-start justify-between gap-3"><p className="font-medium">{evidence.title}</p><Badge tone="neutral">{Math.round(evidence.score * 100)}%</Badge></div>
              <p className="mt-2 text-sm leading-6 text-slate-700">{evidenceExcerpt(evidence.text)}</p>
              <p className="mt-2 font-mono text-[11px] text-muted-ink">{evidence.paper_id} · p. {evidence.page_start}{evidence.page_end !== evidence.page_start ? `–${evidence.page_end}` : ""}</p>
            </article>
          ))}
          {!result.evidence.length ? <EmptyBlock title="未找到 Evidence">没有与当前已授权 Collection 匹配的可定位证据。</EmptyBlock> : null}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Relations</p>
            <h3 className="mt-1 font-semibold">Bounded graph hints</h3>
          </div>
          <Network className="text-brand" size={18} />
        </CardHeader>
        <CardContent className="space-y-3">
          {relations.map((relation, index) => (
            <div className="rounded-lg border bg-white p-3" key={`${relation.edge_id ?? index}:${relation.source_name}:${relation.target_name}`}>
              <p className="font-medium">{relation.source_name}</p>
              <p className="my-1 text-xs font-semibold uppercase tracking-[0.08em] text-brand">{relation.relation_type}</p>
              <p className="font-medium">{relation.target_name}</p>
            </div>
          ))}
          {!relations.length ? <EmptyBlock title="未找到关系">当前范围内没有已发布关系；图投影只提供辅助阅读，不影响 ContextSnapshot 的正式证据约束。</EmptyBlock> : null}
        </CardContent>
      </Card>
      </div>
    </div>
  );
}

export function KnowledgeExplorer({ collectionSlugs }: { collectionSlugs: string[] }) {
  const [searchQuery, setSearchQuery] = useState("");
  const search = useMutation({
    mutationFn: () => api.searchKnowledge(searchQuery.trim(), collectionSlugs),
  });
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (searchQuery.trim().length >= 2 && collectionSlugs.length) search.mutate();
  };
  return (
    <Card>
      <CardHeader>
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Knowledge Explorer</p>
          <h2 className="mt-1 text-lg font-semibold">Project-scoped research</h2>
        </div>
        <ShieldCheck className="text-brand" size={20} />
      </CardHeader>
      <CardContent>
        <p className="max-w-3xl text-sm leading-6 text-muted-ink">查询只会提交当前 Project 已绑定的 Collection。结果用于人工探索；Agent 仍只能使用 immutable ContextSnapshot。</p>
        {collectionSlugs.length ? <div className="mt-4 flex flex-wrap gap-2">{collectionSlugs.map((slug) => <Badge key={slug} tone="brand">{slug}</Badge>)}</div> : <EmptyBlock title="未绑定 Collection">先在 Overview 绑定 Knowledge Collection，避免跨 scope 检索。</EmptyBlock>}
        <form className="mt-5 flex gap-2" onSubmit={submit}>
          <input aria-label="Search knowledge" className="min-w-0 flex-1 rounded-lg border bg-white px-3 py-2 text-sm outline-none focus:border-brand" disabled={!collectionSlugs.length} minLength={2} placeholder="搜索 Entity、Claim 或 Evidence" required value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} />
          <Button disabled={!collectionSlugs.length || search.isPending} type="submit"><Search size={16} /> {search.isPending ? "检索中…" : "Search"}</Button>
        </form>
        {search.error instanceof Error ? <div className="mt-5"><ErrorBlock error={search.error} /></div> : null}
        {search.data ? <SearchResults result={search.data} /> : null}
      </CardContent>
    </Card>
  );
}
