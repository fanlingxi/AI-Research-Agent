import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./AsyncState";

export const verdictNames: Record<string, string> = { supported: "支持", contradicted: "矛盾", insufficient_evidence: "证据不足", cannot_determine: "无法判断" };

export function SemanticObservation() {
  const result = useQuery({ queryKey: ["semantic-observation"], queryFn: api.semanticObservation });
  const data = result.data;
  return <section id="semantic-review" className="min-w-0 space-y-4 rounded-xl border bg-panel p-4 md:p-6" aria-label="语义观察与监督审核">
    <header><h2 className="text-lg font-semibold">语义观察与监督审核</h2><p className="mt-2 text-sm text-muted-ink">A05历史归档，仅检查各结论自带的引用。生产流程保持观察式，不阻断报告；不代表完整报告质量，也不会启动模型调用。</p></header>
    {result.isPending && <LoadingBlock label="正在读取语义观察…" />}
    {result.error && <ErrorBlock error={result.error} onRetry={() => void result.refetch()} />}
    {data && !data.available && <EmptyBlock title="尚无语义观察归档">本机未保存A05结果，当前状态为未评分。</EmptyBlock>}
    {data?.summary && <>
      <p className="rounded-lg bg-brand-soft/40 p-3 text-sm">{data.summary.planned_runs} 次观察 · {data.summary.total_findings} 条结论 · 已标注 {data.summary.human_labeled} · 未标注 {data.summary.unlabeled} · 裁判不可用 {data.summary.judge_unavailable_on_labeled}。已标注一致率：{data.summary.agreement_on_labeled == null ? "未测量" : `${(data.summary.agreement_on_labeled * 100).toFixed(1)}%`}。</p>
      <p className="text-sm text-amber-900">标注方式：{data.annotation_mode === "user_supervised_ai_assisted" ? "用户监督下的AI辅助审核，非独立盲标" : "见归档记录，独立性未验证"}。未覆盖类别：{data.summary.uncovered_categories.map((v) => verdictNames[v] ?? v).join("、") || "无"}。失败与未评分均保留。</p>
      {data.records.map((record) => <details key={record.task_id} className="min-w-0 rounded-lg border p-4">
        <summary className="cursor-pointer text-sm font-medium">{record.task_id} · {record.status} · {record.findings.length} 条结论</summary>
        <p className="mt-3 break-all text-xs text-muted-ink">隔离评测原运行：{record.run_id || "无"} · 输出指纹：{record.output_sha256 || "无"}</p>
        <p className="mt-1 text-xs text-muted-ink">此记录属于评测归档；工作区运行的反馈入口位于对应运行页。</p>
        {record.findings.map((finding) => <article aria-label={`${record.task_id}/${finding.id}`} key={finding.id} className="mt-4 min-w-0 space-y-3 border-t pt-4">
          <p className="whitespace-pre-wrap break-words text-sm font-medium">{finding.assertion}</p>
          <div className="grid min-w-0 gap-3 md:grid-cols-2"><div className="rounded-lg bg-slate-50 p-3 text-sm"><p className="font-medium">模型观察：{verdictNames[finding.verdict ?? ""] ?? "未评分"}</p><p className="mt-2 break-words">{finding.reason || "无可用判断"}</p></div><div className="rounded-lg bg-brand-soft/40 p-3 text-sm"><p className="font-medium">监督审核：{verdictNames[finding.human_label ?? ""] ?? "未标注"}</p><p className="mt-2 break-words">{finding.human_note || "等待人工判断"}</p></div></div>
          {finding.human_label && finding.verdict !== finding.human_label && <p className="text-sm font-semibold text-amber-900">存在分歧，请对照本条引用。</p>}
          {finding.citations.map((citation) => <blockquote key={citation.evidence_id} className="min-w-0 rounded-lg border-l-4 border-brand bg-white p-3 text-sm"><p className="whitespace-pre-wrap break-words leading-6">{citation.quote}</p><p className="mt-2 break-words text-xs text-muted-ink">{citation.title} · 第 {citation.page_start}–{citation.page_end} 页 · 版本 {citation.source_version}</p></blockquote>)}
        </article>)}
      </details>)}
    </>}
  </section>;
}
