import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { statusLabel } from "../lib/labels";
import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "./AsyncState";
import { Disclosure } from "./ui/page";

export const verdictNames: Record<string, string> = { supported: "支持", contradicted: "矛盾", insufficient_evidence: "证据不足", cannot_determine: "无法判断" };

export function SemanticObservation() {
  const result = useQuery({ queryKey: ["semantic-observation"], queryFn: api.semanticObservation });
  const data = result.data;
  return <section id="semantic-review" className="min-w-0 space-y-3 rounded-xl border bg-panel p-4 shadow-[var(--shadow-card)] md:p-5" aria-label="语义观察与监督审核">
    <header>
      <h2 className="text-[15px] font-semibold text-ink">语义观察与监督审核</h2>
      <p className="mt-0.5 text-[13px] leading-5 text-muted-ink">A05 历史归档，仅检查各结论自带的引用。生产流程保持观察式，不阻断报告；不代表完整报告质量，也不会启动模型调用。</p>
    </header>
    {result.isPending && <LoadingBlock label="正在读取语义观察…" />}
    {result.error && <ErrorBlock error={result.error} onRetry={() => void result.refetch()} title="无法读取语义观察" />}
    {data && !data.available && <EmptyBlock title="尚无语义观察归档">本机未保存 A05 结果，当前状态为未评分。</EmptyBlock>}
    {data?.summary && <>
      <Notice>{data.summary.planned_runs} 次观察 · {data.summary.total_findings} 条结论 · 已标注 {data.summary.human_labeled} · 未标注 {data.summary.unlabeled} · 裁判不可用 {data.summary.judge_unavailable_on_labeled}。已标注一致率：{data.summary.agreement_on_labeled == null ? "未测量" : `${(data.summary.agreement_on_labeled * 100).toFixed(1)}%`}。</Notice>
      <p className="text-[13px] leading-6 text-amber-900">标注方式：{data.annotation_mode === "user_supervised_ai_assisted" ? "用户监督下的AI辅助审核，非独立盲标" : "见归档记录，独立性未验证"}。未覆盖类别：{data.summary.uncovered_categories.map((v) => verdictNames[v] ?? v).join("、") || "无"}。失败与未评分均保留。</p>
      <div className="space-y-2">
        {data.records.map((record) => <Disclosure key={record.task_id} summary={`${record.task_id} · ${statusLabel(record.status)} · ${record.findings.length} 条结论`}>
          <p className="break-all text-xs text-muted-ink">隔离评测原运行：{record.run_id || "无"} · 输出指纹：{record.output_sha256 || "无"}</p>
          <p className="mt-0.5 text-xs text-muted-ink">此记录属于评测归档；工作区运行的反馈入口位于对应运行页。</p>
          {record.findings.map((finding) => <article aria-label={`${record.task_id}/${finding.id}`} key={finding.id} className="mt-3 min-w-0 space-y-2.5 border-t pt-3">
            <p className="whitespace-pre-wrap break-words text-[13.5px] font-medium text-ink">{finding.assertion}</p>
            <div className="grid min-w-0 gap-2.5 md:grid-cols-2">
              <div className="min-w-0 rounded-lg bg-subtle p-3 text-[13px]"><p className="font-medium text-ink">模型观察：{verdictNames[finding.verdict ?? ""] ?? "未评分"}</p><p className="mt-1 break-words text-ink-soft">{finding.reason || "无可用判断"}</p></div>
              <div className="min-w-0 rounded-lg bg-brand-tint p-3 text-[13px]"><p className="font-medium text-ink">监督审核：{verdictNames[finding.human_label ?? ""] ?? "未标注"}</p><p className="mt-1 break-words text-ink-soft">{finding.human_note || "等待人工判断"}</p></div>
            </div>
            {finding.human_label && finding.verdict !== finding.human_label && <p className="text-[13px] font-medium text-amber-900">存在分歧，请对照本条引用。</p>}
            {finding.citations.map((citation) => <blockquote key={citation.evidence_id} className="min-w-0 rounded-md border-l-2 border-brand/50 bg-subtle/70 px-3 py-2 text-[13px]"><p className="whitespace-pre-wrap break-words leading-6 text-ink-soft">{citation.quote}</p><p className="mt-1 break-words text-xs text-muted-ink">{citation.title} · 第 {citation.page_start}–{citation.page_end} 页 · 版本 {citation.source_version}</p></blockquote>)}
          </article>)}
        </Disclosure>)}
      </div>
    </>}
  </section>;
}
