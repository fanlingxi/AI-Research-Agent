import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Search } from "lucide-react";
import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import { useSearchParams } from "react-router-dom";
import remarkGfm from "remark-gfm";

import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "../components/AsyncState";
import { MetricHelp } from "../components/MetricHelp";
import { SemanticObservation } from "../components/SemanticObservation";
import { Badge } from "../components/ui/badge";
import { Card, CardContent } from "../components/ui/card";
import { Field, Input, Select } from "../components/ui/field";
import { Disclosure, Page, PageHeader, Stat } from "../components/ui/page";
import { api, type ExperimentSummary, type ExperimentVariant } from "../lib/api";
import { cn } from "../lib/utils";

const STRATEGIES: Record<string, string> = { legacy: "旧策略", "bm25-v1": "BM25 单路", "hybrid-v1": "混合 RRF" };
const STATUSES: Record<string, string> = { completed: "完成", failed: "失败", missing: "记录缺失", not_attempted: "未尝试", running: "进行中", preparing: "准备中" };
const percent = (value: number | null | undefined) => value == null ? "未评分" : `${(value * 100).toFixed(1)}%`;
const money = (value: number | null) => value == null ? "未知" : `¥${value.toFixed(6)}`;
const splitLabel = (split: string) => ({ dev: "开发集 dev", holdout: "历史保留集 holdout" })[split] ?? split;

function BatchContext({ experiment }: { experiment: ExperimentSummary }) {
  const retrieval = experiment.kind === "retrieval";
  return (
    <section aria-label="当前批次说明" className="min-w-0 space-y-2 rounded-xl border bg-panel p-4 text-[13px] leading-6 text-ink-soft shadow-[var(--shadow-card)]">
      <h2 className="text-[15px] font-semibold text-ink">{retrieval ? "历史检索对照" : "历史真实生成"} · {experiment.splits.map(splitLabel).join(" / ")} · {experiment.task_count} 题</h2>
      <p className="break-all font-mono text-xs text-muted-ink">批次：{experiment.id}</p>
      <p>{retrieval
        ? "计分对象是本批次最终选中的原文证据：跨度覆盖、Recall、Hit、MRR 与 nDCG 均为检索指标。本批次没有生成报告，完成表示检索流程完成。"
        : "本批次运行了真实模型并归档报告；跨度覆盖衡量输入证据，不能作为报告正确率。完成表示运行完成，完整报告语义质量仍以另行核验为准。"}</p>
      {experiment.splits.includes("dev") && <p>开发集用于分析问题和调整方案，结果不能作为独立泛化验证。</p>}
      {experiment.splits.includes("holdout") && <p>这是当时冻结的保留集结果；历史记录不会随代码更新而重跑。后续改进需要新运行与独立验证，不能将这批结果理解为最新系统表现。</p>}
      <p className="text-xs text-muted-ink">0% 表示该项已评分且得分为零；“未评分”表示没有可用评分。两者都不直接说明完整报告的语义质量。</p>
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

function Metric({ label, help, children, strong }: { label: string; help: string; children: ReactNode; strong?: boolean }) {
  return (
    <div className="min-w-0">
      <dt><MetricHelp label={label} description={help} /></dt>
      <dd className={cn("mt-0.5 tabular-nums text-ink", strong ? "font-semibold" : "text-ink-soft")}>{children}</dd>
    </div>
  );
}

function Variant({ variant }: { variant: ExperimentVariant }) {
  const name = STRATEGIES[variant.strategy] ?? variant.strategy;
  return (
    <article className="min-w-0 rounded-xl border bg-panel shadow-[var(--shadow-card)]" aria-label={`${name}结果`}>
      <div className="border-b p-4">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-[15px] font-semibold text-ink">{name}</h3>
          <Badge tone={variant.status === "completed" ? "success" : variant.status === "failed" ? "danger" : "warning"}>{STATUSES[variant.status] ?? variant.status}</Badge>
        </div>
        <p className="mt-1 break-words text-xs text-muted-ink">{variant.model}</p>
        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2.5 text-[13px]">
          <Metric label="已选证据跨度覆盖" help={METRIC_HELP.span} strong>{percent(variant.span_recall)}</Metric>
          <Metric label="检索 Recall@10" help={METRIC_HELP.recall} strong>{percent(variant.metrics["recall@10"])}</Metric>
          <Metric label="Hit@10" help={METRIC_HELP.hit}>{percent(variant.metrics["hit@10"])}</Metric>
          <div className="min-w-0"><dt className="flex flex-wrap gap-1"><MetricHelp label="MRR@10" description={METRIC_HELP.mrr} /><span className="text-xs text-muted-ink">·</span><MetricHelp label="nDCG@10" description={METRIC_HELP.ndcg} /></dt><dd className="mt-0.5 tabular-nums text-ink-soft">{percent(variant.metrics["mrr@10"])} · {percent(variant.metrics["ndcg@10"])}</dd></div>
          <Metric label="时延" help={METRIC_HELP.latency}>{variant.latency_ms == null ? "未知" : `${(variant.latency_ms / 1000).toFixed(3)} 秒`}</Metric>
          <Metric label="模型调用" help={METRIC_HELP.calls}>{variant.model_calls ?? "未知"}</Metric>
          <Metric label="实际费用" help={METRIC_HELP.cost}>{money(variant.cost_cny)}</Metric>
          <Metric label="费用估计上界" help={METRIC_HELP.upper}>{money(variant.cost_upper_cny)}</Metric>
        </dl>
        <p className="mt-3 text-xs text-muted-ink">语义正确性：{variant.semantic_review}</p>
      </div>
      <div className="space-y-3 p-4">
        {variant.error ? <Notice tone="danger" title="失败原因"><span className="break-words">{variant.error}</span></Notice> : null}
        {variant.issues.map((issue) => <Notice key={issue} tone="warning">{issue}</Notice>)}
        {variant.content ? (
          <Disclosure summary="查看生成报告">
            <div className="prose-compact break-words"><ReactMarkdown remarkPlugins={[remarkGfm]}>{variant.content}</ReactMarkdown></div>
          </Disclosure>
        ) : <p className="text-xs text-muted-ink">{variant.model === "未调用（仅检索）" ? "本次只测检索，没有生成报告。" : "无可用生成报告；失败记录仍计入分母。"}</p>}
        <div>
          <h4 className="text-[13px] font-medium text-ink">已选原文 · {variant.evidence.length} 段</h4>
          <p className="mt-0.5 text-xs text-muted-ink">按实际选择顺序展示；检索证据不等于语义验收。</p>
        </div>
        {!variant.evidence.length && <p className="rounded-lg border border-dashed p-3 text-[13px] text-muted-ink">没有可核验的原文片段。</p>}
        {variant.evidence.map((evidence, index) => (
          <Disclosure
            defaultOpen={index === 0}
            key={`${evidence.source_id}-${evidence.start}-${index}`}
            summary={<><span className="text-ink">{index + 1}. {evidence.source_id} · 第 {evidence.page} 页</span><span className="mt-0.5 block break-words text-xs font-normal text-muted-ink">{evidence.title}</span></>}
          >
            <p className="whitespace-pre-wrap break-words text-[13px] leading-6 text-ink-soft">{evidence.text}</p>
            <p className="mt-2 break-all border-t pt-2 text-xs text-muted-ink">版本 {evidence.version} · 字符区间 [{evidence.start}, {evidence.end})</p>
          </Disclosure>
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
    <Page width="wide" className="space-y-5">
      <PageHeader title="实验对比" description="面向开发与评估：查看不同检索策略在同一批题目上的证据与失败记录。本页只读，不会调用模型。" />
      <Notice tone="warning" icon={<AlertTriangle size={15} />} title="下方展示 A02 / A04 历史实验归档">
        批次列表尚未接入 A08 后续实验与报告修订，不能据此判断当前系统质量。A05 语义观察在页面底部单独展示。
      </Notice>
      {catalog.isPending && <LoadingBlock label="正在读取实验目录…" />}
      {catalog.isError && <ErrorBlock error={catalog.error} onRetry={() => void catalog.refetch()} title="无法读取实验目录" />}
      {catalog.data?.unavailable.length ? (
        <Disclosure summary={`不可用的归档记录 · ${catalog.data.unavailable.length}`}>
          <ul className="space-y-1 text-xs leading-5 text-muted-ink">{catalog.data.unavailable.map((item) => <li className="break-all" key={item.id}><span className="font-mono">{item.id}</span> · {item.reason}</li>)}</ul>
        </Disclosure>
      ) : null}
      {catalog.data && !catalog.data.experiments.length && <EmptyBlock title="尚无可查看的实验">完成一次受管评测后，结果会显示在这里。缺失或损坏的归档会单独提示。</EmptyBlock>}
      {!!catalog.data?.experiments.length && (
        <Card>
          <CardContent className="grid gap-3 p-4 md:grid-cols-[minmax(0,1fr)_240px]">
            <Field label="实验批次">
              <Select aria-label="实验批次" value={selectedId} onChange={(e) => { choose({ experiment: e.target.value, task: null }); setFilter("all"); setSearch(""); }}>
                {catalog.data.experiments.map((item) => <option key={item.id} value={item.id}>{item.kind === "retrieval" ? "历史检索对照" : "历史真实生成"} · {item.splits.map(splitLabel).join(" / ")} · {item.task_count} 题 · {item.name}</option>)}
              </Select>
            </Field>
            <Field label="检索路径">
              <Select aria-label="检索路径" value={path} disabled={experiment?.kind === "generation"} onChange={(e) => choose({ path: e.target.value, task: null })}><option value="project_run">项目研究 · claim bundle</option><option value="quick_report">快速报告 · chunk</option></Select>
            </Field>
          </CardContent>
        </Card>
      )}
      {selectedId && detail.isPending && <LoadingBlock label="正在读取实验结果…" />}
      {detail.isError && <ErrorBlock error={detail.error} onRetry={() => void detail.refetch()} title="无法读取实验结果" />}
      {experiment && <>
        <BatchContext experiment={experiment} />
        <Card aria-label="实验分母">
          <CardContent className="grid gap-4 p-4 sm:grid-cols-3">
            <Stat label="计划记录" value={`${experiment.planned} 次 · ${experiment.task_count} 题`} />
            <Stat label="完成 / 失败" value={`${experiment.status_counts.completed ?? 0} / ${experiment.status_counts.failed ?? 0}`} />
            <Stat label="记录缺失 / 未尝试" value={`${experiment.status_counts.missing ?? 0} / ${experiment.status_counts.not_attempted ?? 0}`} />
          </CardContent>
        </Card>
        <Notice title={experiment.decision}>{experiment.model}。{experiment.limitations.join(" ")} 上方分母包含所有策略和路径，不随下方筛选减少。</Notice>
        <div className="grid items-start gap-5 xl:grid-cols-[280px_minmax(0,1fr)]">
          <aside className="min-w-0 space-y-2 rounded-xl border bg-panel p-3 shadow-[var(--shadow-card)]">
            <div className="relative">
              <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-ink" size={15} />
              <Input aria-label="搜索题目" className="pl-8" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="题号或问题关键词" />
            </div>
            <Select aria-label="结果筛选" value={filter} onChange={(e) => setFilter(e.target.value)}><option value="all">全部题目</option><option value="failed">失败或缺失记录</option><option value="unscored">缺少跨度评分</option></Select>
            <p className="px-1 pt-1 text-xs text-muted-ink">当前显示 {tasks.length} / {detail.data?.tasks.length} 题</p>
            <div className="max-h-72 space-y-0.5 overflow-y-auto overscroll-contain xl:max-h-[600px]">
              {tasks.map((task) => (
                <button key={task.id} onClick={() => choose({ task: task.id })} aria-pressed={taskId === task.id} className={cn("w-full rounded-lg px-3 py-2 text-left text-[13px] text-ink-soft hover:bg-subtle", taskId === task.id && "bg-brand-tint text-ink ring-1 ring-inset ring-brand/25")}>
                  <span className="block text-xs font-medium text-muted-ink">{task.id} · {splitLabel(task.split)}</span>
                  <span className="mt-0.5 block break-words leading-5">{task.question}</span>
                  {task.variants.some((v) => v.path === path && v.status === "failed") && <span className="mt-1 block text-xs text-red-800">包含失败</span>}
                </button>
              ))}
            </div>
          </aside>
          <section className="min-w-0 space-y-4">
            {!taskId && <EmptyBlock title="没有匹配的题目">调整关键词或筛选条件；筛选不会改变实验统计分母。</EmptyBlock>}
            {taskId && taskDetail.isPending && <LoadingBlock label="正在读取同题证据…" />}
            {taskDetail.isError && <ErrorBlock error={taskDetail.error} onRetry={() => void taskDetail.refetch()} title="无法读取这道题的证据" />}
            {taskId && taskDetail.data && <>
              <header className="min-w-0">
                <h2 className="break-words text-[17px] font-semibold leading-7 text-ink">{taskDetail.data.question}</h2>
                <p className="mt-0.5 text-xs text-muted-ink">{taskId} · {splitLabel(taskDetail.data.split)} · {path === "project_run" ? "完整 claim bundle，受上下文预算约束" : "chunk 证据，每篇最多两段"}</p>
              </header>
              <div className={cn("grid items-start gap-4", taskDetail.data.variants.length > 1 && "2xl:grid-cols-3 lg:grid-cols-2")}>
                {taskDetail.data.variants.map((variant) => <Variant key={variant.strategy} variant={variant} />)}
              </div>
            </>}
          </section>
        </div>
      </>}
      <SemanticObservation />
    </Page>
  );
}
