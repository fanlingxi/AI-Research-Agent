import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, Clock3, FileText, RotateCcw, Wrench, XCircle } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, type AgentRunEvent } from "../lib/api";
import { formatDate, shortId } from "../lib/utils";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";

const ACTIVE = new Set(["queued", "preparing", "running", "validating"]);

function safeRunError(errorCode: string | null) {
  if (errorCode === "citation_validation_failed") {
    return "引用验证未通过；请查看结构化验证结果后再处理。";
  }
  if (errorCode === "stale_context") {
    return "此运行使用的 ContextSnapshot 已失效，无法继续执行。";
  }
  return "运行未能完成。请确认受支持的模型配置后重试，或查看结构化 Trace。";
}

function EventCard({ event }: { event: AgentRunEvent }) {
  const planSteps = event.output_summary.plan_steps;
  return <li className="relative pl-7"><span className="absolute left-0 top-1.5 grid size-4 place-items-center rounded-full border-4 border-panel bg-brand" /><div className="rounded-lg border bg-white p-4"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2"><p className="font-medium">{event.node_name ?? event.event_type}</p><Badge tone="neutral">#{event.sequence}</Badge></div><StatusBadge status={event.status} /></div><p className="mt-1 text-xs text-muted-ink">{event.event_type} · {formatDate(event.created_at)}{event.latency_ms ? ` · ${Math.round(event.latency_ms)} ms` : ""}</p>{Array.isArray(planSteps) ? <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm leading-6 text-slate-700">{planSteps.map((step, index) => <li key={index}>{typeof step === "object" && step ? String((step as Record<string, unknown>).description ?? "") : String(step)}</li>)}</ol> : null}{Object.keys(event.output_summary).length && !Array.isArray(planSteps) ? <pre className="mt-3 overflow-x-auto rounded bg-slate-50 p-3 text-xs leading-5 text-slate-600">{JSON.stringify(event.output_summary, null, 2)}</pre> : null}{Object.keys(event.error).length ? <p className="mt-3 rounded bg-danger-soft p-2 text-xs text-red-900">此节点未完成；请查看运行状态并在需要时重试。</p> : null}</div></li>;
}

export function AgentRunPage() {
  const { runId = "" } = useParams();
  const client = useQueryClient();
  const [afterSequence, setAfterSequence] = useState(0);
  const run = useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId), refetchInterval: (query) => ACTIVE.has(query.state.data?.status ?? "") ? 2_500 : false });
  const trace = useQuery({ queryKey: ["trace", runId, afterSequence], queryFn: () => api.trace(runId, afterSequence), refetchInterval: () => ACTIVE.has(run.data?.status ?? "") ? 2_500 : false });
  const output = useQuery({ queryKey: ["output", runId], queryFn: () => api.output(runId), enabled: run.data?.status === "completed" });
  const cancel = useMutation({ mutationFn: () => api.cancelRun(runId), onSuccess: () => void client.invalidateQueries({ queryKey: ["run", runId] }) });
  const resume = useMutation({ mutationFn: () => api.resumeRun(runId), onSuccess: () => void client.invalidateQueries({ queryKey: ["run", runId] }) });
  if (run.isPending || trace.isPending) return <div className="mx-auto max-w-6xl p-6 lg:p-8"><LoadingBlock label="读取 AgentRun 审计记录…" /></div>;
  if (run.error instanceof Error || trace.error instanceof Error) return <div className="mx-auto max-w-6xl p-6 lg:p-8"><ErrorBlock error={(run.error ?? trace.error) as Error} /></div>;
  const current = run.data!;
  const currentTrace = trace.data!;
  const artifactId = typeof output.data?.structured.artifact_id === "string" ? output.data.structured.artifact_id : undefined;
  const proposalId = typeof output.data?.structured.memory_proposal_id === "string" ? output.data.structured.memory_proposal_id : undefined;
  return <div className="mx-auto max-w-6xl p-6 lg:p-8"><header className="flex flex-wrap items-start justify-between gap-5 border-b pb-6"><div><p className="text-sm font-medium text-brand">AgentRun trace</p><h1 className="mt-1 text-2xl font-semibold tracking-tight">{current.workflow_name}</h1><p className="mt-2 font-mono text-xs text-muted-ink">{current.id}</p></div><div className="flex items-center gap-2"><StatusBadge status={current.status} />{ACTIVE.has(current.status) ? <Button variant="outline" size="sm" disabled={cancel.isPending} onClick={() => cancel.mutate()}><XCircle size={15} /> Cancel</Button> : null}{current.status === "failed" ? <Button size="sm" disabled={resume.isPending} onClick={() => resume.mutate()}><RotateCcw size={15} /> Resume</Button> : null}</div></header>{cancel.error instanceof Error || resume.error instanceof Error ? <div className="mt-5"><ErrorBlock error={(cancel.error ?? resume.error) as Error} /></div> : null}<section className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4"><Card><CardContent><p className="text-xs text-muted-ink">Current node</p><p className="mt-1 font-medium">{current.current_node ?? "queued"}</p></CardContent></Card><Card><CardContent><p className="text-xs text-muted-ink">ContextSnapshot</p><p className="mt-1 font-mono text-sm">{shortId(current.context_snapshot_id)}</p></CardContent></Card><Card><CardContent><p className="text-xs text-muted-ink">Token usage</p><p className="mt-1 font-medium">{currentTrace.token_usage.total_tokens ?? 0} / {current.token_budget}</p></CardContent></Card><Card><CardContent><p className="text-xs text-muted-ink">Runtime telemetry</p><p className="mt-1 font-medium">{Math.round(currentTrace.total_latency_ms)} ms · repair {current.repair_count}</p></CardContent></Card></section>{current.error_message ? <div className="mt-6 rounded-xl border border-red-200 bg-danger-soft p-4 text-sm text-red-950"><div className="flex gap-2"><AlertCircle size={18} /><div><p className="font-medium">运行未完成</p><p className="mt-1">{safeRunError(current.error_code)}</p></div></div></div> : null}<section className="mt-6 grid gap-6 xl:grid-cols-[1.15fr_0.85fr]"><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Safe structured trace</p><h2 className="mt-1 text-lg font-semibold">Timeline</h2></div><Clock3 className="text-brand" size={20} /></CardHeader><CardContent>{currentTrace.events.length ? <ol className="space-y-4 border-l border-slate-200 pl-4">{currentTrace.events.map((event) => <EventCard key={event.id} event={event} />)}</ol> : <EmptyBlock title="尚无事件">Run 排队后将写入追加式事件；不展示 chain-of-thought 或原始 Prompt。</EmptyBlock>}{currentTrace.next_event_sequence ? <Button className="mt-5" variant="outline" onClick={() => setAfterSequence(currentTrace.next_event_sequence!)}>Load next events</Button> : null}</CardContent></Card><div className="space-y-6"><Card><CardHeader><div className="flex items-center gap-2"><Wrench size={17} className="text-brand" /><h2 className="font-semibold">Tool calls</h2></div></CardHeader><CardContent className="space-y-3">{currentTrace.tool_calls.map((call) => <div key={call.id} className="rounded-lg border bg-white p-3"><div className="flex justify-between gap-2"><p className="font-medium">{call.tool_name}</p><StatusBadge status={call.status} /></div><p className="mt-1 text-xs text-muted-ink">{call.permission} · {shortId(call.result_hash)}</p><pre className="mt-2 overflow-x-auto rounded bg-slate-50 p-2 text-[11px] text-slate-600">{JSON.stringify(call.result_summary, null, 2)}</pre></div>)}{!currentTrace.tool_calls.length ? <p className="text-sm text-muted-ink">此 Trace 页无 Tool Call。</p> : null}</CardContent></Card><Card><CardHeader><div className="flex items-center gap-2"><CheckCircle2 size={17} className="text-brand" /><h2 className="font-semibold">Validation & output</h2></div></CardHeader><CardContent>{current.status === "completed" && output.isPending ? <LoadingBlock label="读取输出摘要…" /> : null}{output.error instanceof Error ? <ErrorBlock error={output.error} /> : null}{output.data ? <div className="space-y-3 text-sm"><pre className="max-h-40 overflow-auto rounded bg-slate-50 p-3 text-xs text-slate-600">{JSON.stringify(output.data.validation, null, 2)}</pre>{artifactId ? <Link className="inline-flex items-center gap-2 font-medium text-brand hover:underline" to={`/artifacts/${artifactId}`}><FileText size={16} /> Open Artifact</Link> : null}{proposalId ? <Link className="block font-medium text-brand hover:underline" to={`/review?proposal=${proposalId}`}>Open MemoryProposal review</Link> : null}</div> : current.status === "completed" ? <p className="text-sm text-muted-ink">完成后将展示结构化 validation 与 Artifact provenance。</p> : <p className="text-sm text-muted-ink">此 Run 未完成，因此没有可展示的输出。</p>}</CardContent></Card></div></section></div>;
}
