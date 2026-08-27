import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpenCheck, FileUp, Layers3, RotateCcw, SearchCheck } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { KnowledgeExplorer } from "../components/KnowledgeExplorer";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { api } from "../lib/api";
import { formatDate } from "../lib/utils";

const PAGE_LIMITS = [20, 50, 100, 150] as const;

function IngestionForm() {
  const client = useQueryClient();
  const [collection, setCollection] = useState("");
  const [sources, setSources] = useState("");
  const [pageLimit, setPageLimit] = useState<number>(20);
  const submit = useMutation({
    mutationFn: () => api.submitAndExecuteIngestion({
      collection: collection.trim() || undefined,
      sources: sources.split("\n").map((source) => source.trim()).filter(Boolean),
      pdf_max_pages: pageLimit,
    }),
    onSuccess: () => {
      setSources("");
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
      void client.invalidateQueries({ queryKey: ["knowledge-collections"] });
      void client.invalidateQueries({ queryKey: ["knowledge-health"] });
    },
  });
  const sourceCount = sources.split("\n").filter((source) => source.trim()).length;
  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (sourceCount) submit.mutate();
  };
  return (
    <Card>
      <CardHeader>
        <div>
          <p className="text-xs font-semibold tracking-[0.1em] text-muted-ink">文献入库</p>
          <h2 className="mt-1 text-lg font-semibold">添加研究文献</h2>
        </div>
        <FileUp className="text-brand" size={20} />
      </CardHeader>
      <CardContent>
        <p className="text-sm leading-6 text-muted-ink">提交后会由独立 Worker 依次完成解析、分块、索引与候选抽取；页面会持续更新进度。默认只处理每篇前 20 页，长文档应显式选择策略。</p>
        <form className="mt-5 space-y-4" onSubmit={onSubmit}>
          <label className="block text-sm font-medium">知识集合（可选）
            <input className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2 outline-none focus:border-brand" placeholder="例如：LLM Agent 的工具使用" value={collection} onChange={(event) => setCollection(event.target.value)} />
          </label>
          <label className="block text-sm font-medium">PDF 路径或 URL（每行一个）
            <textarea className="mt-1.5 min-h-36 w-full rounded-lg border bg-white px-3 py-2 font-mono text-xs leading-5 outline-none focus:border-brand" minLength={3} placeholder={"data/raw_papers/paper.pdf\nhttps://arxiv.org/pdf/2302.04761"} required value={sources} onChange={(event) => setSources(event.target.value)} />
          </label>
          <div className="rounded-lg border bg-slate-50 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm font-medium">单篇处理页数</p><span className="text-xs text-muted-ink">{sourceCount || 0} 篇待提交</span></div>
            <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
              {PAGE_LIMITS.map((limit) => <button className={`rounded-lg border px-3 py-2 text-left text-sm ${pageLimit === limit ? "border-brand bg-brand-soft text-brand" : "bg-white text-slate-600 hover:border-brand/40"}`} key={limit} onClick={() => setPageLimit(limit)} type="button"><span className="block font-medium">{limit} 页</span><span className="text-xs opacity-80">{limit === 20 ? "快速预览" : limit === 150 ? "长文档完整覆盖" : "平衡模式"}</span></button>)}
            </div>
            <p className="mt-3 text-xs leading-5 text-muted-ink">当前单篇处理上限为 150 页，可根据部署资源和研究需求继续配置或调整。系统会完整索引已处理页；候选抽取采用分层采样，不等同于将全文直接放入模型上下文。</p>
          </div>
          {submit.error instanceof Error ? <ErrorBlock error={submit.error} /> : null}
          <Button className="w-full" disabled={!sourceCount || submit.isPending} type="submit">{submit.isPending ? "正在创建并启动…" : "提交并开始入库"}</Button>
        </form>
      </CardContent>
    </Card>
  );
}

function MoveInboxIngestion({
  ingestion,
  collections,
}: {
  ingestion: Awaited<ReturnType<typeof api.ingestions>>[number];
  collections: Awaited<ReturnType<typeof api.collections>>;
}) {
  const client = useQueryClient();
  const [targetSlug, setTargetSlug] = useState("");
  const formalCollections = collections.filter((collection) => !collection.is_system);
  const move = useMutation({
    mutationFn: () => {
      const target = formalCollections.find((collection) => collection.slug === targetSlug);
      if (!target) throw new Error("请选择正式知识集合。");
      return api.moveIngestionCollection(ingestion.id, target.name);
    },
    onSuccess: () => {
      setTargetSlug("");
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
      void client.invalidateQueries({ queryKey: ["knowledge-collections"] });
      void client.invalidateQueries({ queryKey: ["knowledge-health"] });
    },
  });
  if (ingestion.collection_slug !== "inbox" || ["queued", "running", "publishing"].includes(ingestion.status)) return null;
  return (
    <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3">
      <p className="text-xs font-medium text-amber-950">此文献仍在系统收件箱，不会进入 Project 正式研究范围。</p>
      <div className="mt-2 flex flex-wrap gap-2">
        <select
          aria-label={`移动 ${ingestion.collection} 到正式知识集合`}
          className="min-w-0 flex-1 rounded-lg border bg-white px-3 py-2 text-xs"
          value={targetSlug}
          onChange={(event) => setTargetSlug(event.target.value)}
        >
          <option value="">选择正式知识集合</option>
          {formalCollections.map((collection) => <option key={collection.slug} value={collection.slug}>{collection.name}</option>)}
        </select>
        <Button disabled={!targetSlug || move.isPending} size="sm" variant="outline" onClick={() => move.mutate()}>{move.isPending ? "移动中…" : "移动"}</Button>
      </div>
      {!formalCollections.length ? <p className="mt-2 text-xs text-amber-900">请先以正式集合名称提交一次入库，创建可选目标集合。</p> : null}
      {move.error instanceof Error ? <div className="mt-2"><ErrorBlock error={move.error} /></div> : null}
    </div>
  );
}

export function KnowledgePage() {
  const client = useQueryClient();
  const ingestions = useQuery({
    queryKey: ["knowledge-ingestions"],
    queryFn: api.ingestions,
    refetchInterval: (query) => query.state.data?.some((item) =>
      ["queued", "running", "publishing"].includes(item.status)) ? 2_500 : false,
  });
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections });
  const health = useQuery({ queryKey: ["knowledge-health"], queryFn: api.knowledgeHealth, refetchInterval: 10_000 });
  const retry = useMutation({
    mutationFn: (ingestionId: string) => api.retryIngestion(ingestionId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
      void client.invalidateQueries({ queryKey: ["knowledge-health"] });
    },
  });
  const collectionSlugs = useMemo(() => collections.data?.filter((item) => !item.is_system).map((item) => item.slug) ?? [], [collections.data]);
  if (ingestions.isPending || collections.isPending) return <div className="p-6 lg:p-8"><LoadingBlock label="读取知识库…" /></div>;
  if (ingestions.error instanceof Error || collections.error instanceof Error) return <div className="p-6 lg:p-8"><ErrorBlock error={(ingestions.error ?? collections.error) as Error} onRetry={() => { void ingestions.refetch(); void collections.refetch(); }} /></div>;
  const items = ingestions.data ?? [];
  const waitingReview = items.filter((item) => item.status === "needs_review").reduce((total, item) => total + Math.max(0, item.candidate_count - item.published_count), 0);
  return (
    <div className="mx-auto max-w-7xl p-6 lg:p-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b pb-6">
        <div><p className="text-sm font-medium text-brand">证据驱动的知识库</p><h1 className="mt-1 text-3xl font-semibold tracking-tight">知识库</h1><p className="mt-2 max-w-3xl text-sm leading-6 text-muted-ink">从 PDF 入库、证据检索到人工审核的主工作台。正式知识仍以审核后的事实为准。</p></div>
        <div className="flex items-center gap-2 rounded-lg border bg-panel px-3 py-2 text-xs text-muted-ink"><span className={`size-2 rounded-full ${health.data?.knowledge.live_llm_configured ? "bg-emerald-500" : "bg-amber-500"}`} />{health.data?.knowledge.live_llm_configured ? `${health.data.knowledge.llm_provider} 已配置` : "真实 LLM 未配置"}</div>
      </header>
      <section className="mt-6 grid gap-4 sm:grid-cols-3">
        <Card><CardContent><p className="text-xs text-muted-ink">知识集合</p><p className="mt-1 text-2xl font-semibold">{collections.data?.filter((item) => !item.is_system).length ?? 0}</p></CardContent></Card>
        <Card><CardContent><p className="text-xs text-muted-ink">已提交入库</p><p className="mt-1 text-2xl font-semibold">{items.length}</p></CardContent></Card>
        <Card><CardContent><p className="text-xs text-muted-ink">待人工审核</p><p className="mt-1 text-2xl font-semibold">{waitingReview}</p></CardContent></Card>
      </section>
      <section className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]"><IngestionForm /><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">最近任务</p><h2 className="mt-1 text-lg font-semibold">入库任务</h2></div><Layers3 className="text-brand" size={20} /></CardHeader><CardContent className="max-h-[34rem] space-y-3 overflow-y-auto overscroll-contain pr-2" data-testid="ingestion-history-list">{items.map((item) => <article className="rounded-lg border bg-white p-4" key={item.id}><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">{item.collection}</p><p className="mt-1 text-xs text-muted-ink">{item.document_count} / {item.sources.length} 篇 PDF · 每篇最多 {item.pdf_max_pages} 页</p></div><StatusBadge status={item.status} /></div><div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-ink"><span>{item.candidate_count} 个候选</span><span>{item.published_count} 条已发布</span><span>{formatDate(item.updated_at)}</span>{item.queue_position ? <span>队列第 {item.queue_position} 位</span> : null}</div>{item.error ? <p className="mt-3 rounded bg-danger-soft p-2 text-xs text-red-900">{item.error}</p> : null}{item.status === "failed" ? <div className="mt-3"><Button disabled={retry.isPending && retry.variables === item.id} size="sm" variant="outline" onClick={() => retry.mutate(item.id)}><RotateCcw size={14} />{retry.isPending && retry.variables === item.id ? "正在重新启动…" : "重新执行"}</Button>{retry.error instanceof Error && retry.variables === item.id ? <div className="mt-3"><ErrorBlock error={retry.error} /></div> : null}</div> : null}<MoveInboxIngestion collections={collections.data ?? []} ingestion={item} /></article>)}{!items.length ? <EmptyBlock title="还没有入库任务">从左侧提交本地或远程 PDF；提交后会显示可追溯的处理状态。</EmptyBlock> : null}</CardContent></Card></section>
      <section className="mt-6"><KnowledgeExplorer collectionSlugs={collectionSlugs} /></section>
      <div className="mt-4 flex items-center gap-2 text-xs text-muted-ink"><BookOpenCheck size={14} /><span>检索只返回已审核、已发布的正文证据；候选内容请到审核中心处理。</span><SearchCheck size={14} /></div>
    </div>
  );
}
