import { SemanticObservation } from "../components/SemanticObservation";
import { MetricHelp } from "../components/MetricHelp";
import { useQuery } from "@tanstack/react-query";
import { FlaskConical, Search } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { useSearchParams } from "react-router-dom";
import remarkGfm from "remark-gfm";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { api, type ExperimentSummary, type ExperimentVariant } from "../lib/api";
import { cn } from "../lib/utils";

const STRATEGIES: Record<string, string> = { legacy: "旧策略", "bm25-v1": "BM25 单路", "hybrid-v1": "混合 RRF" };
const STATUSES: Record<string, string> = { completed: "完成", failed: "失败", missing: "记录缺失", not_attempted: "未尝试", running: "进行中", preparing: "准备中" };
const selectClass = "mt-1 w-full min-w-0 rounded-lg border bg-white px-3 py-2 text-sm";
const percent = (value: number | null | undefined) => value == null ? "未评分" : `${(value * 100).toFixed(1)}%`;
const money = (value: number | null) => value == null ? "未知" : `¥${value.toFixed(6)}`;
const splitLabel = (split: string) => ({ dev: "开发集 dev", holdout: "历史保留集 holdout" })[split] ?? split;

function BatchContext({ experiment }: { experiment: ExperimentSummary }) {
  const retrieval = experiment.kind === "retrieval";
  return (
    <section aria-label="当前批次说明" className="space-y-3 rounded-xl border bg-panel p-4 text-sm">
      <h2 className="font-semibold">{retrieval ? "历史检索对照" : "历史真实生成"} · {experiment.splits.map(splitLabel).join(" / ")} · {experiment.task_count} 题</h2>
      <p className="break-all text-xs text-muted-ink">批次：{experiment.id}</p>
      <p>{retrieval
        ? "计分对象是本批次最终选中的原文证据：跨度覆盖、Recall、Hit、MRR 与 nDCG 均为检索指标。本批次没有生成报告，完成表示检索流程完成。"
        : "本批次运行了真实模型并归档报告；跨度覆盖衡量输入证据，不能作为报告正确率。完成表示运行完成，完整报告语义质量仍以另行核验为准。"}</p>
      {experiment.splits.includes("dev") && <p>开发集用于分析问题和调整方案，结果不能作为独立泛化验证。</p>}
      {experiment.splits.includes("holdout") && <p>这是当时冻结的保留集结果；历史记录不会随代码更新而重跑。后续改进需要新运行与独立验证，不能将这批结果理解为最新系统表现。</p>}
      <p className="text-muted-ink">0% 表示该项已评分且得分为零；“未评分”表示没有可用评分。两者都不直接说明完整报告的语义质量。</p>
    </section>
  );
}
const METRIC_HELP = {
  span: "全部已选证据完整覆盖了多少比例的标注原文跨度。要求覆盖完整区间，不是出现相同关键词。没有适用标注时显示“未评分”。",
  recall: "本项目按原文跨度计算：最终选中证据的前10项，完整覆盖的标注跨度数 ÷ 标注跨度总数。受容量或每篇段数限制，实际可能不足10项；不是候选池召回率，也不是回答准确率。",
  hit: "最终选中证据的前10项只要有一项与标注原文区间相交，就记为100%，否则为0%。没有适用标注时为“未评分”。命中一部分不等于完整覆盖。",
  mrr: "前10项中第一个相关结果的排名倒数：第1名为100%，第5名为20%，没有命中为0%。这里相关指与标注原文区间相交，越高表示首次命中越靠前。",
  ndcg: "衡量相关结果是否排在前面，并与理想排序比较。越靠后的命中贡献越小；本项目按与标注原文区间相交计为相关。范围0%–100%，无命中为0%，不是回答正确率。",
  latency: "这条策略记录的耗时。仅检索实验包含检索、校验和证据装配；生成实验展示归档的运行耗时。单次本机测量不代表线上平均速度。",
  calls: "本条记录中的模型调用次数。纯检索对照为0；未知表示归档不足以确定次数。它不是当前账号的累计调用数。",
  cost: "归档记录的实际人民币费用。¥0表示已记录为零，“未知”表示没有可确认的金额，不能当作免费。",
  upper: "按归档用量与计价政策估算的费用上界，不是最终账单；信息不足时显示“未知”。",
};

function Variant({ variant }: { variant: ExperimentVariant }) {
  return (
    <article className="min-w-0 rounded-xl border bg-panel" aria-label={`${STRATEGIES[variant.strategy] ?? variant.strategy}结果`}>
      <div className="border-b p-4">
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-semibold">{STRATEGIES[variant.strategy] ?? variant.strategy}</h3>
          <span className={cn("rounded-full px-2 py-1 text-xs", variant.status === "completed" ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-900")}>
            {STATUSES[variant.status] ?? variant.status}
          </span>
        </div>
        <p className="mt-2 break-words text-xs text-muted-ink">{variant.model}</p>
        <dl className="mt-4 grid grid-cols-2 gap-x-3 gap-y-3 text-sm">
          <div><dt><MetricHelp label="已选证据跨度覆盖" description={METRIC_HELP.span} /></dt><dd className="mt-1 font-semibold">{percent(variant.span_recall)}</dd></div>
          <div><dt><MetricHelp label="检索 Recall@10" description={METRIC_HELP.recall} /></dt><dd className="mt-1 font-semibold">{percent(variant.metrics["recall@10"])}</dd></div>
          <div><dt><MetricHelp label="Hit@10" description={METRIC_HELP.hit} /></dt><dd>{percent(variant.metrics["hit@10"])}</dd></div>
          <div><dt className="flex flex-wrap gap-1"><MetricHelp label="MRR@10" description={METRIC_HELP.mrr} /><span className="text-xs text-muted-ink">·</span><MetricHelp label="nDCG@10" description={METRIC_HELP.ndcg} /></dt><dd>{percent(variant.metrics["mrr@10"])} · {percent(variant.metrics["ndcg@10"])}</dd></div>
          <div><dt><MetricHelp label="时延" description={METRIC_HELP.latency} /></dt><dd>{variant.latency_ms == null ? "未知" : `${(variant.latency_ms / 1000).toFixed(3)} 秒`}</dd></div>
          <div><dt><MetricHelp label="模型调用" description={METRIC_HELP.calls} /></dt><dd>{variant.model_calls ?? "未知"}</dd></div>
          <div><dt><MetricHelp label="实际费用" description={METRIC_HELP.cost} /></dt><dd>{money(variant.cost_cny)}</dd></div>
          <div><dt><MetricHelp label="费用估计上界" description={METRIC_HELP.upper} /></dt><dd>{money(variant.cost_upper_cny)}</dd></div>
        </dl>
        <p className="mt-3 text-xs text-muted-ink">语义正确性：{variant.semantic_review}</p>
      </div>
      <div className="space-y-4 p-4">
        {variant.error && <div className="break-words rounded-lg bg-danger-soft p-3 text-sm text-red-900" role="note"><p className="font-medium">失败原因</p><p className="mt-1">{variant.error}</p></div>}
        {variant.issues.map((issue) => <p className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900" key={issue}>{issue}</p>)}
        {variant.content ? <details><summary className="cursor-pointer text-sm font-medium text-brand">查看生成报告</summary><div className="prose-workspace mt-3 break-words"><ReactMarkdown remarkPlugins={[remarkGfm]}>{variant.content}</ReactMarkdown></div></details> : <p className="text-xs text-muted-ink">{variant.model === "未调用（仅检索）" ? "本次只测检索，没有生成报告。" : "无可用生成报告；失败记录仍计入分母。"}</p>}
        <div><h4 className="text-sm font-medium">已选原文 · {variant.evidence.length} 段</h4><p className="mt-1 text-xs text-muted-ink">按实际选择顺序展示；检索证据不等于语义验收。</p></div>
        {!variant.evidence.length && <p className="rounded-lg border border-dashed p-3 text-sm text-muted-ink">没有可核验的原文片段。</p>}
        {variant.evidence.map((evidence, index) => (
          <details className="rounded-lg border p-3" key={`${evidence.source_id}-${evidence.start}-${index}`} open={index === 0}>
            <summary className="cursor-pointer text-sm"><span className="font-medium">{index + 1}. {evidence.source_id} · 第 {evidence.page} 页</span><span className="mt-1 block break-words text-xs text-muted-ink">{evidence.title}</span></summary>
            <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6">{evidence.text}</p>
            <p className="mt-3 break-all border-t pt-2 text-xs text-muted-ink">版本 {evidence.version} · 字符区间 [{evidence.start}, {evidence.end})</p>
          </details>
        ))}
      </div>
    </article>
  );
}

export function ExperimentsPage() {
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const catalog = useQuery({ queryKey: ["experiments"], queryFn: api.experiments });
  const selectedId = params.get("experiment") ?? catalog.data?.experiments[0]?.id ?? "";
  const detail = useQuery({ queryKey: ["experiment", selectedId], queryFn: () => api.experiment(selectedId), enabled: Boolean(selectedId) });
  const experiment = detail.data?.experiment;
  const path = experiment?.kind === "generation" ? "project_run" : params.get("path") === "quick_report" ? "quick_report" : "project_run";
  const tasks = (detail.data?.tasks ?? []).filter((task) => {
    const variants = task.variants.filter((v) => v.path === path);
    return variants.length > 0 && `${task.id} ${task.question}`.toLowerCase().includes(search.toLowerCase()) &&
      (filter === "all" || (filter === "failed" ? variants.some((v) => v.status === "failed" || v.status === "missing") : variants.some((v) => v.span_recall == null)));
  });
  const taskId = tasks.find((task) => task.id === params.get("task"))?.id ?? tasks[0]?.id ?? "";
  const taskDetail = useQuery({ queryKey: ["experiment-task", selectedId, taskId, path], queryFn: () => api.experimentTask(selectedId, taskId, path), enabled: Boolean(selectedId && taskId) });
  function choose(values: Record<string, string | null>) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(values)) { if (value == null) next.delete(key); else next.set(key, value); }
    setParams(next);
  }

  return (
    <div className="space-y-6 p-4 md:p-8">
      <header><div className="flex items-center gap-2 text-brand"><FlaskConical size={20} /><span className="text-sm font-medium">研究质量与实验</span></div><h1 className="mt-2 text-2xl font-semibold tracking-tight">实验对比</h1><p className="mt-2 text-sm text-muted-ink">查看同题策略、原文证据与失败。本页只读，不会启动模型调用。</p></header>
      <SemanticObservation />
      <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900" role="note">
        <p className="font-medium">下方展示 A02 / A04 历史实验归档</p>
        <p className="mt-1">批次列表尚未接入 A08 后续实验与报告修订，不能据此判断当前系统质量。A05 语义观察在上方单独展示。</p>
      </div>
      {catalog.isPending && <LoadingBlock label="正在读取实验目录…" />}
      {catalog.isError && <ErrorBlock error={catalog.error} onRetry={() => void catalog.refetch()} />}
      {catalog.data?.unavailable.map((item) => <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900" key={item.id}>不可用记录：{item.id} · {item.reason}</p>)}
      {catalog.data && !catalog.data.experiments.length && <EmptyBlock title="尚无可查看的实验">完成一次受管评测后，结果会显示在这里。缺失或损坏的归档会单独提示。</EmptyBlock>}
      {!!catalog.data?.experiments.length && <section className="grid gap-4 rounded-xl border bg-panel p-4 md:grid-cols-[minmax(0,1fr)_220px]">
        <label className="min-w-0 text-xs font-medium text-muted-ink">实验批次<select aria-label="实验批次" className={selectClass} value={selectedId} onChange={(e) => { choose({ experiment: e.target.value, task: null }); setFilter("all"); setSearch(""); }}>
          {catalog.data.experiments.map((item) => <option key={item.id} value={item.id}>{item.kind === "retrieval" ? "历史检索对照" : "历史真实生成"} · {item.splits.map(splitLabel).join(" / ")} · {item.task_count} 题 · {item.name}</option>)}
        </select></label>
        <label className="text-xs font-medium text-muted-ink">检索路径<select aria-label="检索路径" className={selectClass} value={path} disabled={experiment?.kind === "generation"} onChange={(e) => choose({ path: e.target.value, task: null })}><option value="project_run">项目研究 · claim bundle</option><option value="quick_report">快速报告 · chunk</option></select></label>
      </section>}
      {selectedId && detail.isPending && <LoadingBlock label="正在读取实验结果…" />}
      {detail.isError && <ErrorBlock error={detail.error} onRetry={() => void detail.refetch()} />}
      {experiment && <>
        <BatchContext experiment={experiment} />
        <section className="grid gap-3 sm:grid-cols-3" aria-label="实验分母">
          {[ ["计划记录", `${experiment.planned} 次 · ${experiment.task_count} 题`], ["完成 / 失败", `${experiment.status_counts.completed ?? 0} / ${experiment.status_counts.failed ?? 0}`], ["记录缺失 / 未尝试", `${experiment.status_counts.missing ?? 0} / ${experiment.status_counts.not_attempted ?? 0}`] ].map(([label, value]) => <div className="rounded-xl border bg-panel p-4" key={label}><p className="text-xs text-muted-ink">{label}</p><p className="mt-2 text-lg font-semibold">{value}</p></div>)}
        </section>
        <div className="rounded-xl border bg-brand-soft/30 p-4 text-sm"><p className="font-medium">{experiment.decision}</p><p className="mt-1 text-muted-ink">{experiment.model}。{experiment.limitations.join(" ")} 上方分母包含所有策略和路径，不随下方筛选减少。</p></div>
        <div className="grid items-start gap-5 xl:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="min-w-0 rounded-xl border bg-panel p-3">
            <label className="flex items-center gap-2 rounded-lg border px-3 py-2"><Search size={15} className="shrink-0 text-muted-ink" /><input className="w-full min-w-0 bg-transparent text-sm outline-none" aria-label="搜索题目" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="题号或问题关键词" /></label>
            <select aria-label="结果筛选" className={selectClass} value={filter} onChange={(e) => setFilter(e.target.value)}><option value="all">全部题目</option><option value="failed">失败或缺失记录</option><option value="unscored">缺少跨度评分</option></select>
            <p className="my-3 px-1 text-xs text-muted-ink">当前显示 {tasks.length} / {detail.data?.tasks.length} 题</p>
            <div className="max-h-[600px] space-y-1 overflow-y-auto">{tasks.map((task) => <button key={task.id} onClick={() => choose({ task: task.id })} aria-pressed={taskId === task.id} className={cn("w-full rounded-lg p-3 text-left text-sm hover:bg-slate-50", taskId === task.id && "bg-brand-soft text-brand")}><span className="block text-xs font-semibold">{task.id} · {splitLabel(task.split)}</span><span className="mt-1 block">{task.question}</span>{task.variants.some((v) => v.path === path && v.status === "failed") && <span className="mt-1 block text-xs text-red-800">包含失败</span>}</button>)}</div>
          </aside>
          <section className="min-w-0 space-y-4">
            {!taskId && <EmptyBlock title="没有匹配的题目">调整关键词或筛选条件；筛选不会改变实验统计分母。</EmptyBlock>}
            {taskId && taskDetail.isPending && <LoadingBlock label="正在读取同题证据…" />}
            {taskDetail.isError && <ErrorBlock error={taskDetail.error} onRetry={() => void taskDetail.refetch()} />}
            {taskId && taskDetail.data && <><header><h2 className="text-lg font-semibold">{taskDetail.data.question}</h2><p className="mt-1 text-xs text-muted-ink">{taskId} · {splitLabel(taskDetail.data.split)} · {path === "project_run" ? "完整 claim bundle，受上下文预算约束" : "chunk 证据，每篇最多两段"}</p></header><div className={cn("grid items-start gap-4", taskDetail.data.variants.length > 1 && "2xl:grid-cols-3 lg:grid-cols-2")}>
              {taskDetail.data.variants.map((variant) => <Variant key={variant.strategy} variant={variant} />)}
            </div></>}
          </section>
        </div>
      </>}
    </div>
  );
}
