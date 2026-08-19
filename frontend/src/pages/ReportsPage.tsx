import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BarChart3, FileDown, FileText, ShieldCheck } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { api, type ResearchReport } from "../lib/api";
import { formatDate } from "../lib/utils";

function score(value: number | undefined) {
  return `${Math.round((value ?? 0) * 100)}%`;
}

function ReportQuality({ report }: { report: ResearchReport }) {
  const evaluation = report.evaluation;
  if (!evaluation) return <p className="text-sm text-muted-ink">报告完成后会显示证据、引用和结构质量门结果。</p>;
  const metrics = [
    ["证据落地", evaluation.evidence_grounding],
    ["引用覆盖", evaluation.citation_coverage],
    ["引用忠实", evaluation.citation_fidelity],
    ["结构完整", evaluation.structure_score],
  ];
  return <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">{metrics.map(([label, value]) => <div className="rounded-lg border bg-white p-3" key={String(label)}><p className="text-xs text-muted-ink">{label}</p><p className="mt-1 text-lg font-semibold">{score(Number(value))}</p></div>)}</div>;
}

function ReportComposer({ collections }: { collections: Array<{ slug: string; name: string }> }) {
  const client = useQueryClient();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [topK, setTopK] = useState(8);
  const [depth, setDepth] = useState<"brief" | "standard" | "deep">("standard");
  const submit = useMutation({
    mutationFn: () => api.submitReport({ query: query.trim(), collection_slugs: selected, top_k: topK, report_depth: depth }),
    onSuccess: () => {
      setQuery("");
      void client.invalidateQueries({ queryKey: ["reports"] });
    },
  });
  const toggle = (slug: string) => setSelected((current) => current.includes(slug) ? current.filter((item) => item !== slug) : [...current, slug]);
  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (query.trim().length >= 2) submit.mutate();
  };
  return <Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Grounded report</p><h2 className="mt-1 text-lg font-semibold">新建研究报告</h2></div><ShieldCheck className="text-brand" size={20} /></CardHeader><CardContent><p className="text-sm leading-6 text-muted-ink">报告只消费已发布的 PDF 证据。质量门要求引用完整、忠实且结构合格；未通过会保留失败原因而不是伪装为完成。</p><form className="mt-5 space-y-4" onSubmit={onSubmit}><label className="block text-sm font-medium">研究问题<textarea className="mt-1.5 min-h-28 w-full rounded-lg border bg-white px-3 py-2 outline-none focus:border-brand" minLength={2} placeholder="例如：对比文献中工具使用与规划如何影响智能体可靠性。" required value={query} onChange={(event) => setQuery(event.target.value)} /></label><fieldset><legend className="text-sm font-medium">知识集合范围</legend><p className="mt-1 text-xs text-muted-ink">留空表示全部已发布知识；选择集合可限制证据范围。</p><div className="mt-3 flex flex-wrap gap-2">{collections.map((collection) => <label className={`cursor-pointer rounded-full border px-3 py-1.5 text-xs ${selected.includes(collection.slug) ? "border-brand bg-brand-soft text-brand" : "bg-white text-slate-600"}`} key={collection.slug}><input checked={selected.includes(collection.slug)} className="sr-only" onChange={() => toggle(collection.slug)} type="checkbox" />{collection.name}</label>)}</div></fieldset><div className="grid gap-4 sm:grid-cols-2"><label className="text-sm font-medium">证据切片数<input className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2" max={30} min={1} onChange={(event) => setTopK(Number(event.target.value))} type="number" value={topK} /></label><label className="text-sm font-medium">报告深度<select className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2" onChange={(event) => setDepth(event.target.value as typeof depth)} value={depth}><option value="brief">Brief</option><option value="standard">Standard</option><option value="deep">Deep</option></select></label></div><p className="rounded-lg bg-slate-50 p-3 text-xs leading-5 text-muted-ink">质量提示：本次会检索最多 {topK} 条证据；引用覆盖门按实际证据集计算。证据越多，报告需要显式覆盖的引用也越多。</p>{submit.error instanceof Error ? <ErrorBlock error={submit.error} /> : null}<Button className="w-full" disabled={submit.isPending} type="submit">{submit.isPending ? "正在排队…" : "生成有据可查的报告"}</Button></form></CardContent></Card>;
}

export function ReportsPage() {
  const reports = useQuery({ queryKey: ["reports"], queryFn: api.reports });
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections });
  const [selectedId, setSelectedId] = useState<string>("");
  const selected = useMemo(() => {
    const items = reports.data ?? [];
    return items.find((item) => item.id === selectedId) ?? items[0];
  }, [reports.data, selectedId]);
  if (reports.isPending || collections.isPending) return <div className="p-6 lg:p-8"><LoadingBlock label="读取研究报告…" /></div>;
  if (reports.error instanceof Error || collections.error instanceof Error) return <div className="p-6 lg:p-8"><ErrorBlock error={(reports.error ?? collections.error) as Error} onRetry={() => { void reports.refetch(); void collections.refetch(); }} /></div>;
  return <div className="mx-auto max-w-7xl p-6 lg:p-8"><header className="flex flex-wrap items-end justify-between gap-4 border-b pb-6"><div><p className="text-sm font-medium text-brand">Evidence-first synthesis</p><h1 className="mt-1 text-3xl font-semibold tracking-tight">研究报告</h1><p className="mt-2 text-sm text-muted-ink">报告与证据包共存；质量门未通过时会明确标记为失败。</p></div><BarChart3 className="text-brand" size={28} /></header><section className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]"><ReportComposer collections={collections.data?.filter((item) => !item.is_system) ?? []} /><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">History</p><h2 className="mt-1 text-lg font-semibold">报告历史</h2></div><FileText className="text-brand" size={20} /></CardHeader><CardContent className="space-y-3">{reports.data?.map((report) => <button className={`w-full rounded-lg border p-4 text-left transition ${selected?.id === report.id ? "border-brand bg-brand-soft/40" : "bg-white hover:border-brand/40"}`} key={report.id} onClick={() => setSelectedId(report.id)}><div className="flex flex-wrap items-start justify-between gap-3"><p className="font-medium">{report.query}</p><StatusBadge status={report.status} /></div><p className="mt-2 text-xs text-muted-ink">{report.evidence.length} evidence · {formatDate(report.updated_at)}</p></button>)}{!reports.data?.length ? <EmptyBlock title="还没有报告">知识审核发布后，在左侧提交研究问题以创建报告。</EmptyBlock> : null}</CardContent></Card></section>{selected ? <section className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_300px]"><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Selected report</p><h2 className="mt-1 text-xl font-semibold">{selected.query}</h2></div><StatusBadge status={selected.status} /></CardHeader><CardContent>{selected.status === "failed" ? <div className="mb-5 rounded-lg border border-red-200 bg-danger-soft p-4 text-sm text-red-950">{selected.error || "报告未通过质量门。"}</div> : null}<ReportQuality report={selected} />{selected.status === "completed" ? <article className="prose-workspace mt-6 rounded-lg border bg-white p-5"><ReactMarkdown remarkPlugins={[remarkGfm]}>{selected.content}</ReactMarkdown></article> : <p className="mt-6 text-sm text-muted-ink">报告处于 {selected.status} 状态；可在运行记录中查看任务队列和最近错误。</p>}</CardContent></Card><aside className="space-y-5"><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Evidence pack</p><h2 className="mt-1 font-semibold">可定位证据</h2></div></CardHeader><CardContent className="space-y-3">{selected.evidence.map((evidence) => <article className="rounded-lg border bg-white p-3" key={evidence.id}><p className="font-medium text-sm">[{evidence.id}] {evidence.title}</p><p className="mt-1 text-xs text-muted-ink">p. {evidence.page_start}{evidence.page_end !== evidence.page_start ? `–${evidence.page_end}` : ""}</p><p className="mt-2 line-clamp-5 text-xs leading-5 text-slate-700">{evidence.text}</p></article>)}{!selected.evidence.length ? <p className="text-sm text-muted-ink">任务完成后将显示证据包。</p> : null}</CardContent></Card>{selected.status === "completed" ? <a className="inline-flex w-full items-center justify-center gap-2 rounded-lg border bg-white px-3.5 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50" href={`/api/reports/${encodeURIComponent(selected.id)}/download`}><FileDown size={16} />下载 Markdown</a> : null}</aside></section> : null}</div>;
}
