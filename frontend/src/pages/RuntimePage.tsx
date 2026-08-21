import { useQuery } from "@tanstack/react-query";
import { Activity, Database, FileClock, ShieldAlert } from "lucide-react";
import { Link } from "react-router-dom";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { api } from "../lib/api";
import { appConfig } from "../lib/config";
import { formatDate } from "../lib/utils";

const SERVICE_LABELS: Record<string, string> = {
  neo4j: "图关系数据库",
  qdrant: "向量检索服务",
  ingestion_dispatcher: "文献入库执行器",
  report_dispatcher: "报告执行器",
};

export function RuntimePage() {
  const health = useQuery({ queryKey: ["knowledge-health"], queryFn: api.knowledgeHealth, refetchInterval: 10_000 });
  const ingestions = useQuery({
    queryKey: ["knowledge-ingestions"],
    queryFn: api.ingestions,
    refetchInterval: (query) => query.state.data?.some((item) =>
      ["queued", "running", "publishing"].includes(item.status)) ? 5_000 : false,
  });
  const reports = useQuery({
    queryKey: ["reports"],
    queryFn: api.reports,
    refetchInterval: (query) => query.state.data?.some((item) => (
      item.status === "running"
      || (
        item.status === "queued"
        && item.run_metadata.current_stage === "queued_for_dispatch"
      )
    )) ? 2_500 : false,
  });
  if (health.isPending || ingestions.isPending || reports.isPending) return <div className="p-6 lg:p-8"><LoadingBlock label="读取运行记录…" /></div>;
  if (health.error instanceof Error || ingestions.error instanceof Error || reports.error instanceof Error) return <div className="p-6 lg:p-8"><ErrorBlock error={(health.error ?? ingestions.error ?? reports.error) as Error} onRetry={() => { void health.refetch(); void ingestions.refetch(); void reports.refetch(); }} /></div>;
  const services = Object.entries(health.data?.services ?? {});
  const work = [
    ...(ingestions.data ?? []).map((item) => ({ id: item.id, title: item.collection, kind: "PDF 入库", status: item.status, updated_at: item.updated_at, detail: `${item.document_count}/${item.sources.length} 篇 · ${item.candidate_count} 个候选`, error: item.error, href: "/knowledge" })),
    ...(reports.data ?? []).map((item) => ({ id: item.id, title: item.query, kind: "研究报告", status: item.status, updated_at: item.updated_at, detail: `${item.evidence.length} 条证据`, error: item.error, href: `/reports?report=${encodeURIComponent(item.id)}` })),
  ].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  return <div className="mx-auto max-w-7xl p-6 lg:p-8"><header className="flex flex-wrap items-end justify-between gap-4 border-b pb-6"><div><p className="text-sm font-medium text-brand">运行可观测性</p><h1 className="mt-1 text-3xl font-semibold tracking-tight">运行记录</h1><p className="mt-2 text-sm text-muted-ink">任务状态、队列与服务健康在此集中呈现；点击任务即可继续执行或查看结果。</p></div><Activity className="text-brand" size={28} /></header><section className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{services.map(([name, service]) => <Card key={name}><CardContent><div className="flex items-center justify-between gap-3"><p className="text-sm font-medium">{SERVICE_LABELS[name] ?? name}</p><span className={`size-2 rounded-full ${service.available ? "bg-emerald-500" : "bg-red-500"}`} /></div><p className="mt-2 text-xs text-muted-ink">{service.detail || (service.available ? "可用" : "不可用")}</p></CardContent></Card>)}</section><section className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]"><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">最近任务</p><h2 className="mt-1 text-lg font-semibold">任务时间线</h2></div><FileClock className="text-brand" size={20} /></CardHeader><CardContent className="max-h-[38rem] space-y-3 overflow-y-auto overscroll-contain pr-2" data-testid="runtime-work-list">{work.map((item) => <Link className="block rounded-lg border bg-white p-4 transition hover:border-brand/40" key={item.id} to={item.href}><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">{item.title}</p><p className="mt-1 text-xs text-muted-ink">{item.kind} · {item.detail}</p></div><StatusBadge status={item.status} /></div><p className="mt-3 text-xs text-muted-ink">更新于 {formatDate(item.updated_at)}</p>{item.error ? <p className="mt-3 rounded bg-danger-soft p-2 text-xs text-red-900">{item.error}</p> : null}</Link>)}{!work.length ? <EmptyBlock title="尚无运行记录">提交入库或研究报告后，状态会自动刷新。</EmptyBlock> : null}</CardContent></Card><aside className="space-y-5"><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">队列</p><h2 className="mt-1 font-semibold">后台工作</h2></div><Database className="text-brand" size={20} /></CardHeader><CardContent className="space-y-3">{Object.entries(health.data?.knowledge.jobs ?? {}).map(([status, count]) => <div className="flex items-center justify-between rounded-lg border bg-white px-3 py-2" key={status}><StatusBadge status={status} /><span className="font-semibold">{count}</span></div>)}</CardContent></Card><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">高级诊断</p><h2 className="mt-1 font-semibold">需要底层诊断？</h2></div><ShieldAlert className="text-brand" size={20} /></CardHeader><CardContent><p className="text-sm leading-6 text-muted-ink">报告启动与失败重试已经回到主工作台；Streamlit 仅保留投影恢复和原始服务诊断。</p><a className="mt-4 inline-flex text-sm font-medium text-brand hover:underline" href={appConfig.operationsUrl} rel="noreferrer" target="_blank">打开运营控制台</a></CardContent></Card></aside></section></div>;
}
