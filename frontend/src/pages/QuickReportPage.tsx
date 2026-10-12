import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileDown, LoaderCircle, Play, RotateCcw } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "../components/AsyncState";
import { ReportLayout, RailSection } from "../components/report/ReportLayout";
import { extractCitations, ReportMarkdown } from "../components/report/ReportMarkdown";
import { pageLabel, SourceSheet } from "../components/report/SourceDrawer";
import { SourceList } from "../components/report/SourceList";
import { StatusBadge } from "../components/StatusBadge";
import { Badge } from "../components/ui/badge";
import { Button, buttonVariants } from "../components/ui/button";
import { Disclosure, Page } from "../components/ui/page";
import { api, type ResearchReport } from "../lib/api";
import { documentTitle, usePaperTitles, useSystemStatus } from "../lib/hooks";
import { DEPTH_LABELS } from "../lib/labels";
import { formatDate } from "../lib/utils";

const STAGE_LABELS: Record<string, string> = {
  queued_for_dispatch: "已提交，等待后台执行器领取",
  scheduled: "执行器已接收任务",
  retrieving_evidence: "正在检索并筛选已发布的证据",
  generating_draft: "正在撰写报告初稿",
  validating_citations: "正在校验引用与结构",
  revising_draft: "质量检查未通过，正在进行一次修订",
  completed: "报告已完成",
  failed: "生成未完成",
};

const STAGE_PROGRESS: Record<string, number> = {
  scheduled: 16,
  retrieving_evidence: 32,
  generating_draft: 55,
  validating_citations: 76,
  revising_draft: 88,
};

const ACTIVE = new Set(["queued", "running"]);

function percent(value: number | undefined) {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function ReportProgress({ report }: { report: ResearchReport }) {
  if (!ACTIVE.has(report.status)) return null;
  const stage = String(report.run_metadata.current_stage ?? "");
  const progress = report.status === "queued" ? 6 : (STAGE_PROGRESS[stage] ?? 20);
  const label = STAGE_LABELS[stage] ?? (report.status === "queued" ? "等待开始" : "正在生成");
  return (
    <div className="rounded-xl border border-brand/20 bg-brand-tint p-4" aria-live="polite">
      <div className="flex items-center gap-2 text-sm font-medium text-brand-strong">
        <LoaderCircle className={report.status === "running" ? "animate-spin" : ""} size={16} />
        {label}
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-brand-soft">
        <div aria-label="报告生成进度" aria-valuemax={100} aria-valuemin={0} aria-valuenow={progress} className="h-full rounded-full bg-brand transition-[width] duration-500" role="progressbar" style={{ width: `${progress}%` }} />
      </div>
      <p className="mt-2 text-xs text-muted-ink">页面会自动刷新；离开此页不会中断生成。</p>
    </div>
  );
}

function ReportAction({ report, dispatcherAvailable }: { report: ResearchReport; dispatcherAvailable: boolean | undefined }) {
  const client = useQueryClient();
  const onDone = (next: ResearchReport) => {
    client.setQueryData(["report", next.id], next);
    void client.invalidateQueries({ queryKey: ["reports"] });
  };
  const execute = useMutation({ mutationFn: () => api.executeReport(report.id), onSuccess: onDone });
  const retry = useMutation({ mutationFn: () => api.retryReport(report.id), onSuccess: onDone });
  const mutation = report.status === "failed" ? retry : execute;
  const awaitingDispatcher = report.status === "queued" && report.run_metadata.current_stage === "queued_for_dispatch";
  if (awaitingDispatcher && dispatcherAvailable !== false) return null;
  if (!["queued", "failed"].includes(report.status)) return null;
  const label = mutation.isPending
    ? "正在启动…"
    : awaitingDispatcher ? "重新启动报告执行器" : report.status === "failed" ? "清理失败结果并重新执行" : "立即执行";
  return (
    <>
      <Button disabled={mutation.isPending} variant={report.status === "failed" ? "outline" : "default"} onClick={() => mutation.mutate()}>
        {report.status === "failed" || awaitingDispatcher ? <RotateCcw size={15} /> : <Play size={15} />}
        {label}
      </Button>
      {mutation.error instanceof Error ? <div className="w-full"><ErrorBlock error={mutation.error} /></div> : null}
    </>
  );
}

function QualityPanel({ report }: { report: ResearchReport }) {
  const evaluation = report.evaluation;
  if (!evaluation) return <p className="text-[13px] text-muted-ink">报告完成后会显示自动质量检查结果。</p>;
  const metrics: Array<[string, number | undefined, string]> = [
    ["证据落地", evaluation.evidence_grounding, "报告内容能在证据中找到依据的比例"],
    ["引用覆盖", evaluation.citation_coverage, "需要引用的段落中带有引用的比例"],
    ["引用忠实", evaluation.citation_fidelity, "引用编号都指向本次证据包"],
    ["结构完整", evaluation.structure_score, "报告包含要求的章节"],
    ["检索相关", evaluation.retrieval_relevance, "选入的证据与问题相关的比例"],
    ["来源多样", evaluation.source_diversity, "引用覆盖了多少不同文献"],
  ];
  return (
    <div>
      <p className={evaluation.passed ? "flex items-center gap-1.5 text-[13px] font-medium text-emerald-800" : "flex items-center gap-1.5 text-[13px] font-medium text-amber-800"}>
        {evaluation.passed ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
        {evaluation.passed ? "通过自动质量检查" : "未通过自动质量检查"}
      </p>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5">
        {metrics.map(([label, value, help]) => (
          <div key={label} title={help}>
            <dt className="text-xs text-muted-ink">{label}</dt>
            <dd className="text-sm font-semibold tabular-nums text-ink">{percent(value)}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-3 text-xs leading-5 text-muted-ink">自动检查只衡量引用与结构，不能代替人工判断结论是否正确。</p>
    </div>
  );
}

export function QuickReportPage() {
  const { reportId = "" } = useParams();
  const report = useQuery({
    queryKey: ["report", reportId],
    queryFn: () => api.report(reportId),
    refetchInterval: (query) => ACTIVE.has(query.state.data?.status ?? "") ? 2_500 : false,
  });
  const health = useSystemStatus();
  const titles = usePaperTitles();
  const [active, setActive] = useState<string | null>(null);
  const data = report.data;
  const citations = useMemo(() => extractCitations(data?.content ?? ""), [data?.content]);
  const evidenceById = useMemo(() => new Map((data?.evidence ?? []).map((item) => [item.id, item])), [data?.evidence]);
  const onCite = useCallback((id: string) => setActive(id), []);
  if (report.isPending) return <Page><LoadingBlock label="读取报告…" /></Page>;
  if (report.error instanceof Error || !data) {
    return <Page><ErrorBlock error={report.error ?? new Error("找不到这份报告。")} onRetry={() => void report.refetch()} title="无法打开这份报告" /></Page>;
  }
  const dispatcherAvailable = health.isPending ? undefined : health.data?.services.worker?.available === true;
  const hasContent = Boolean(data.content.trim());
  const cited = citations.filter((id) => evidenceById.has(id));
  const uncited = data.evidence.filter((item) => !citations.includes(item.id));
  const sourceItem = (id: string) => {
    const evidence = evidenceById.get(id);
    return {
      id,
      title: evidence ? documentTitle(evidence.title, evidence.paper_id, titles) : `引用 ${id}`,
      page: evidence ? pageLabel(evidence.page_start, evidence.page_end) : "未在证据包中找到",
    };
  };
  const activeEvidence = active ? evidenceById.get(active) : undefined;
  const activeIndex = active ? citations.indexOf(active) : -1;
  const meta = data.run_metadata as Record<string, unknown>;
  return (
    <>
      <ReportLayout
        kind={<><Badge tone="info">快速报告</Badge><StatusBadge status={data.status === "failed" && hasContent ? "quality_failed" : data.status} /></>}
        title={data.query}
        meta={<>
          <span>{formatDate(data.created_at)}</span>
          <span>深度：{DEPTH_LABELS[data.report_depth] ?? data.report_depth}</span>
          <span>{data.evidence.length} 条证据 · 引用 {cited.length} 处来源</span>
          <span>范围：{data.topic_slugs.length ? data.topic_slugs.join("、") : "全部已发布知识"}</span>
        </>}
        actions={<>
          {hasContent ? <a className={buttonVariants({ variant: "outline" })} href={`/api/reports/${encodeURIComponent(data.id)}/download`}><FileDown size={15} />下载 Markdown</a> : null}
          <ReportAction report={data} dispatcherAvailable={dispatcherAvailable} />
        </>}
        notices={<>
          <ReportProgress report={data} />
          {data.status === "queued" && data.run_metadata.current_stage === "queued_for_dispatch" && dispatcherAvailable === false ? (
            <Notice tone="warning" title="后台执行器当前未运行">可以在这里重新启动，不会创建重复任务。</Notice>
          ) : null}
          {data.status === "failed" && hasContent ? (
            <Notice tone="warning" icon={<AlertTriangle size={15} />} title="这份报告未通过自动质量检查">
              {data.error ? `${data.error} ` : ""}正文仍可阅读，请重点核对引用；也可以清理结果后重新生成。
            </Notice>
          ) : null}
          {data.status === "failed" && !hasContent ? (
            <Notice tone="danger" title="报告生成失败">{data.error || "生成过程中断，可以重新执行。"}</Notice>
          ) : null}
        </>}
        body={hasContent ? (
          <ReportMarkdown activeId={active} markdown={data.content} onCite={onCite} />
        ) : (
          <EmptyBlock className="border-0" title={ACTIVE.has(data.status) ? "报告正在生成" : "暂无正文"}>
            {ACTIVE.has(data.status) ? "完成后正文会显示在这里。" : "这份报告还没有可阅读的正文。"}
          </EmptyBlock>
        )}
        rail={<>
          <RailSection title={`引用来源 · ${cited.length}`}>
            <SourceList activeId={active} items={cited.map(sourceItem)} onSelect={setActive} emptyText={hasContent ? "正文中没有可定位的引用。" : "生成完成后显示。"} />
          </RailSection>
          {uncited.length ? (
            <Disclosure summary={`检索到但未引用的证据 · ${uncited.length}`}>
              <SourceList activeId={active} items={uncited.map((item) => sourceItem(item.id))} onSelect={setActive} />
            </Disclosure>
          ) : null}
          <RailSection title="质量检查"><QualityPanel report={data} /></RailSection>
          <Disclosure summary="生成详情">
            <dl className="space-y-1.5 text-xs text-muted-ink">
              <div className="flex justify-between gap-3"><dt>模型</dt><dd className="text-right text-ink-soft">{String(meta.llm_model ?? "未知")}</dd></div>
              <div className="flex justify-between gap-3"><dt>模型调用</dt><dd className="text-ink-soft">{String(meta.generation_calls ?? "未知")}</dd></div>
              <div className="flex justify-between gap-3"><dt>输入 / 输出 Token</dt><dd className="text-ink-soft">{String(meta.input_tokens ?? "—")} / {String(meta.output_tokens ?? "—")}</dd></div>
              <div className="flex justify-between gap-3"><dt>耗时</dt><dd className="text-ink-soft">{typeof meta.latency_ms === "number" ? `${(meta.latency_ms / 1000).toFixed(1)} 秒` : "未知"}</dd></div>
              <div className="flex justify-between gap-3"><dt>报告编号</dt><dd className="break-all text-right font-mono text-[11px] text-ink-soft">{data.id}</dd></div>
            </dl>
          </Disclosure>
        </>}
      />
      <SourceSheet
        number={activeIndex >= 0 ? activeIndex + 1 : 0}
        open={Boolean(active)}
        total={citations.length}
        onOpenChange={(open) => !open && setActive(null)}
        onStep={activeIndex >= 0 ? (delta) => setActive(citations[activeIndex + delta] ?? active) : undefined}
        error={active && !activeEvidence ? new Error("此引用编号不在本报告的证据包中。") : null}
        source={activeEvidence ? {
          title: documentTitle(activeEvidence.title, activeEvidence.paper_id, titles),
          page: pageLabel(activeEvidence.page_start, activeEvidence.page_end),
          passage: activeEvidence.text,
          meta: <>相关度 {Math.round(activeEvidence.score * 100)}% · 引用编号 {activeEvidence.id}</>,
        } : undefined}
      />
    </>
  );
}
