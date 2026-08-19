import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BarChart3,
  FileDown,
  FileText,
  LoaderCircle,
  Play,
  RotateCcw,
  ShieldCheck,
} from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { useSearchParams } from "react-router-dom";
import remarkGfm from "remark-gfm";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { api, type ResearchReport } from "../lib/api";
import { formatDate } from "../lib/utils";

const ACTIVE_REPORT_STATUSES = new Set(["queued", "running"]);
const STAGE_LABELS: Record<string, string> = {
  queued_for_dispatch: "任务已提交，正在等待执行器领取",
  scheduled: "执行器已接收任务",
  retrieving_evidence: "正在检索并筛选正式证据",
  generating_draft: "正在生成报告初稿",
  validating_citations: "正在校验引用与结构",
  revising_draft: "质量门未通过，正在进行一次受控修订",
  completed: "报告与证据包已完成",
  failed: "任务未完成，可原位重试",
};

function score(value: number | undefined) {
  return `${Math.round((value ?? 0) * 100)}%`;
}

function upsertReport(current: ResearchReport[] | undefined, report: ResearchReport) {
  return [report, ...(current ?? []).filter((item) => item.id !== report.id)];
}

function ReportQuality({ report }: { report: ResearchReport }) {
  const evaluation = report.evaluation;
  if (!evaluation) {
    return <p className="text-sm text-muted-ink">报告完成后会显示证据、引用和结构质量门结果。</p>;
  }
  const metrics = [
    ["证据落地", evaluation.evidence_grounding],
    ["引用覆盖", evaluation.citation_coverage],
    ["引用忠实", evaluation.citation_fidelity],
    ["结构完整", evaluation.structure_score],
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {metrics.map(([label, value]) => (
        <div className="rounded-lg border bg-white p-3" key={String(label)}>
          <p className="text-xs text-muted-ink">{label}</p>
          <p className="mt-1 text-lg font-semibold">{score(Number(value))}</p>
        </div>
      ))}
    </div>
  );
}

function ReportProgress({ report }: { report: ResearchReport }) {
  const stage = String(report.run_metadata.current_stage ?? "");
  const stageProgress: Record<string, number> = {
    scheduled: 16,
    retrieving_evidence: 32,
    generating_draft: 55,
    validating_citations: 76,
    revising_draft: 88,
    completed: 100,
    failed: 100,
  };
  const progress = report.status === "queued" ? 6 : (stageProgress[stage] ?? 20);
  const label = report.status === "queued"
    ? (STAGE_LABELS[stage] ?? "等待手动启动")
    : (STAGE_LABELS[stage] ?? `报告处于 ${report.status} 状态`);
  if (!ACTIVE_REPORT_STATUSES.has(report.status)) return null;
  return (
    <div className="mb-5 rounded-xl border border-brand/20 bg-brand-soft/30 p-4" aria-live="polite">
      <div className="flex items-center gap-2 text-sm font-medium text-brand">
        <LoaderCircle className={report.status === "running" ? "animate-spin" : ""} size={16} />
        {label}
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-white">
        <div
          aria-label="报告生成进度"
          aria-valuemax={100}
          aria-valuemin={0}
          aria-valuenow={progress}
          className="h-full rounded-full bg-brand transition-[width] duration-500"
          role="progressbar"
          style={{ width: `${progress}%` }}
        />
      </div>
      <p className="mt-2 text-xs text-muted-ink">页面会自动刷新；可以离开此页，任务会继续执行。</p>
    </div>
  );
}

function ReportAction({
  report,
  dispatcherAvailable,
}: {
  report: ResearchReport;
  dispatcherAvailable: boolean | undefined;
}) {
  const client = useQueryClient();
  const execute = useMutation({
    mutationFn: () => api.executeReport(report.id),
    onSuccess: (next) => {
      client.setQueryData<ResearchReport[]>(["reports"], (current) => upsertReport(current, next));
      void client.invalidateQueries({ queryKey: ["reports"] });
    },
  });
  const retry = useMutation({
    mutationFn: () => api.retryReport(report.id),
    onSuccess: (next) => {
      client.setQueryData<ResearchReport[]>(["reports"], (current) => upsertReport(current, next));
      void client.invalidateQueries({ queryKey: ["reports"] });
    },
  });
  const mutation = report.status === "failed" ? retry : execute;
  const awaitingDispatcher = (
    report.status === "queued"
    && report.run_metadata.current_stage === "queued_for_dispatch"
  );
  if (awaitingDispatcher && dispatcherAvailable !== false) return null;
  if (!["queued", "failed"].includes(report.status)) return null;
  return (
    <div className="mt-5">
      {awaitingDispatcher ? (
        <p className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
          报告执行器当前离线；可以在这里安全地重新启动，不会创建重复任务。
        </p>
      ) : null}
      <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
        {report.status === "failed" || awaitingDispatcher
          ? <RotateCcw size={16} />
          : <Play size={16} />}
        {mutation.isPending
          ? "正在启动…"
          : awaitingDispatcher
            ? "重新启动报告执行器"
            : report.status === "failed"
            ? "清理失败结果并重新执行"
            : "立即执行"}
      </Button>
      {mutation.error instanceof Error ? (
        <div className="mt-3"><ErrorBlock error={mutation.error} /></div>
      ) : null}
    </div>
  );
}

function ReportComposer({
  collections,
  onStarted,
}: {
  collections: Array<{ slug: string; name: string }>;
  onStarted: (report: ResearchReport) => void;
}) {
  const client = useQueryClient();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [topK, setTopK] = useState(8);
  const [depth, setDepth] = useState<"brief" | "standard" | "deep">("standard");
  const submit = useMutation({
    mutationFn: () => api.submitAndExecuteReport({
      query: query.trim(),
      collection_slugs: selected,
      top_k: topK,
      report_depth: depth,
    }),
    onSuccess: (report) => {
      client.setQueryData<ResearchReport[]>(["reports"], (current) => upsertReport(current, report));
      setQuery("");
      onStarted(report);
    },
    onSettled: () => void client.invalidateQueries({ queryKey: ["reports"] }),
  });
  const toggle = (slug: string) => setSelected((current) => (
    current.includes(slug)
      ? current.filter((item) => item !== slug)
      : [...current, slug]
  ));
  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (query.trim().length >= 3) submit.mutate();
  };
  return (
    <Card>
      <CardHeader>
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Grounded report</p>
          <h2 className="mt-1 text-lg font-semibold">新建研究报告</h2>
        </div>
        <ShieldCheck className="text-brand" size={20} />
      </CardHeader>
      <CardContent>
        <p className="text-sm leading-6 text-muted-ink">
          输入问题后会立即开始执行，无需另开终端或通用 Worker。报告只消费已发布的 PDF 证据。
        </p>
        <form className="mt-5 space-y-4" onSubmit={onSubmit}>
          <label className="block text-sm font-medium">
            研究问题
            <textarea
              className="mt-1.5 min-h-28 w-full rounded-lg border bg-white px-3 py-2 outline-none focus:border-brand"
              minLength={3}
              placeholder="例如：对比文献中工具使用与规划如何影响智能体可靠性。"
              required
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <fieldset>
            <legend className="text-sm font-medium">知识集合范围</legend>
            <p className="mt-1 text-xs text-muted-ink">留空表示全部已发布知识；选择集合可限制证据范围。</p>
            <div className="mt-3 flex flex-wrap gap-2">
              {collections.map((collection) => (
                <label
                  className={`cursor-pointer rounded-full border px-3 py-1.5 text-xs ${
                    selected.includes(collection.slug)
                      ? "border-brand bg-brand-soft text-brand"
                      : "bg-white text-slate-600"
                  }`}
                  key={collection.slug}
                >
                  <input
                    checked={selected.includes(collection.slug)}
                    className="sr-only"
                    type="checkbox"
                    onChange={() => toggle(collection.slug)}
                  />
                  {collection.name}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-sm font-medium">
              最多引用的证据片段
              <input
                className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2"
                max={30}
                min={1}
                type="number"
                value={topK}
                onChange={(event) => setTopK(Number(event.target.value))}
              />
            </label>
            <label className="text-sm font-medium">
              报告深度
              <select
                className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2"
                value={depth}
                onChange={(event) => setDepth(event.target.value as typeof depth)}
              >
                <option value="brief">简报</option>
                <option value="standard">标准报告</option>
                <option value="deep">深度综述</option>
              </select>
            </label>
          </div>
          <p className="rounded-lg bg-slate-50 p-3 text-xs leading-5 text-muted-ink">
            这里限制的是最终报告引用的证据片段数，不是可检索的论文数量；15 篇文献仍会进入检索范围。
          </p>
          {submit.error instanceof Error ? <ErrorBlock error={submit.error} /> : null}
          <Button className="w-full" disabled={submit.isPending} type="submit">
            {submit.isPending ? <LoaderCircle className="animate-spin" size={16} /> : <Play size={16} />}
            {submit.isPending ? "正在创建并启动…" : "提交并开始生成"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

export function ReportsPage() {
  const [params, setParams] = useSearchParams();
  const reports = useQuery({
    queryKey: ["reports"],
    queryFn: api.reports,
    refetchInterval: (query) => (
      query.state.data?.some((report) => (
        report.status === "running"
        || (
          report.status === "queued"
          && report.run_metadata.current_stage === "queued_for_dispatch"
        )
      ))
        ? 2_500
        : false
    ),
  });
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections });
  const health = useQuery({
    queryKey: ["knowledge-health"],
    queryFn: api.knowledgeHealth,
    refetchInterval: 10_000,
  });
  const dispatcherAvailable = health.isPending
    ? undefined
    : health.data?.services.report_dispatcher?.available === true;
  const selectedId = params.get("report") ?? "";
  const selected = useMemo(() => {
    const items = reports.data ?? [];
    return selectedId ? items.find((item) => item.id === selectedId) : items[0];
  }, [reports.data, selectedId]);
  if (reports.isPending || collections.isPending) {
    return <div className="p-6 lg:p-8"><LoadingBlock label="读取研究报告…" /></div>;
  }
  if (reports.error instanceof Error || collections.error instanceof Error) {
    return (
      <div className="p-6 lg:p-8">
        <ErrorBlock
          error={(reports.error ?? collections.error) as Error}
          onRetry={() => { void reports.refetch(); void collections.refetch(); }}
        />
      </div>
    );
  }
  return (
    <div className="mx-auto max-w-7xl p-6 lg:p-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b pb-6">
        <div>
          <p className="text-sm font-medium text-brand">Evidence-first synthesis</p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight">研究报告</h1>
          <p className="mt-2 text-sm text-muted-ink">提交即执行、进度自动刷新，失败可在原位置重试。</p>
        </div>
        <BarChart3 className="text-brand" size={28} />
      </header>
      <section className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
        <ReportComposer
          collections={collections.data?.filter((item) => !item.is_system) ?? []}
          onStarted={(report) => setParams({ report: report.id })}
        />
        <Card>
          <CardHeader>
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">History</p>
              <h2 className="mt-1 text-lg font-semibold">报告历史</h2>
            </div>
            <FileText className="text-brand" size={20} />
          </CardHeader>
          <CardContent className="space-y-3">
            {reports.data?.map((report) => (
              <button
                className={`w-full rounded-lg border p-4 text-left transition ${
                  selected?.id === report.id
                    ? "border-brand bg-brand-soft/40"
                    : "bg-white hover:border-brand/40"
                }`}
                key={report.id}
                onClick={() => setParams({ report: report.id })}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <p className="font-medium">{report.query}</p>
                  <StatusBadge status={report.status} />
                </div>
                <p className="mt-2 text-xs text-muted-ink">
                  {report.evidence.length} 条证据 · {formatDate(report.updated_at)}
                </p>
              </button>
            ))}
            {!reports.data?.length ? (
              <EmptyBlock title="还没有报告">在左侧输入研究问题，工作台会创建并直接执行。</EmptyBlock>
            ) : null}
          </CardContent>
        </Card>
      </section>
      {selected ? (
        <section className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_300px]">
          <Card>
            <CardHeader>
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Selected report</p>
                <h2 className="mt-1 text-xl font-semibold">{selected.query}</h2>
              </div>
              <StatusBadge status={selected.status} />
            </CardHeader>
            <CardContent>
              <ReportProgress report={selected} />
              {selected.status === "failed" ? (
                <div className="mb-5 rounded-lg border border-red-200 bg-danger-soft p-4 text-sm text-red-950">
                  {selected.error || "报告未通过质量门。"}
                </div>
              ) : null}
              <ReportQuality report={selected} />
              <ReportAction report={selected} dispatcherAvailable={dispatcherAvailable} />
              {selected.status === "completed" ? (
                <article className="prose-workspace mt-6 rounded-lg border bg-white p-5">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{selected.content}</ReactMarkdown>
                </article>
              ) : null}
            </CardContent>
          </Card>
          <aside className="space-y-5">
            <Card>
              <CardHeader>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Evidence pack</p>
                  <h2 className="mt-1 font-semibold">可定位证据</h2>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                {selected.evidence.map((evidence) => (
                  <article className="rounded-lg border bg-white p-3" key={evidence.id}>
                    <p className="text-sm font-medium">[{evidence.id}] {evidence.title}</p>
                    <p className="mt-1 text-xs text-muted-ink">
                      p. {evidence.page_start}
                      {evidence.page_end !== evidence.page_start ? `–${evidence.page_end}` : ""}
                    </p>
                    <p className="mt-2 line-clamp-5 text-xs leading-5 text-slate-700">{evidence.text}</p>
                  </article>
                ))}
                {!selected.evidence.length ? (
                  <p className="text-sm text-muted-ink">检索完成后将逐步显示本次证据包。</p>
                ) : null}
              </CardContent>
            </Card>
            {selected.status === "completed" ? (
              <a
                className="inline-flex w-full items-center justify-center gap-2 rounded-lg border bg-white px-3.5 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
                href={`/api/reports/${encodeURIComponent(selected.id)}/download`}
              >
                <FileDown size={16} />下载 Markdown
              </a>
            ) : null}
          </aside>
        </section>
      ) : selectedId ? (
        <section className="mt-6">
          <EmptyBlock title="找不到这份报告">
            记录可能已被清理，请从上方报告历史重新选择。
          </EmptyBlock>
        </section>
      ) : null}
    </div>
  );
}
