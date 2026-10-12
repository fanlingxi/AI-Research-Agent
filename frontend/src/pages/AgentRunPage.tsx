import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ArrowLeft, ArrowRight, Check, Circle, FileText, LoaderCircle, RotateCcw, XCircle } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { JsonDetails } from "../components/JsonDetails";
import { RunFeedbackPanel } from "../components/RunFeedbackPanel";
import { api, type AgentRunEvent } from "../lib/api";
import { EVENT_LABELS, nodeLabel, TOOL_LABELS } from "../lib/labels";
import { cn, formatDate, shortId } from "../lib/utils";
import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Button, buttonVariants } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Disclosure, Page } from "../components/ui/page";

const ACTIVE = new Set(["queued", "preparing", "running", "validating"]);
const PIPELINE = ["load_context", "task_analysis", "constrained_plan", "snapshot_tools", "research_draft", "citation_validation"];

function safeRunError(errorCode: string | null, errorMessage: string | null) {
  if (errorMessage === "AgentRun has exhausted its token budget.") {
    return "本次运行的总 Token 上限已耗尽。可登记反馈，在修订运行时提高生成用量上限。";
  }
  if (errorCode === "citation_validation_failed") {
    return "引用验证未通过；请查看验证结果后再处理。";
  }
  if (errorCode === "stale_context") {
    return "此运行使用的上下文快照已失效，无法继续执行。";
  }
  return "运行未能完成。请确认模型配置后重试，或查看完整执行记录。";
}

function Pipeline({ events, status, currentNode }: { events: AgentRunEvent[]; status: string; currentNode: string | null }) {
  const completed = new Set(events.filter((event) => event.event_type === "node_completed").map((event) => event.node_name));
  const started = new Set(events.filter((event) => event.event_type === "node_started").map((event) => event.node_name));
  const latency = new Map(events.filter((event) => event.event_type === "node_completed" && event.latency_ms).map((event) => [event.node_name, event.latency_ms!]));
  const failed = !ACTIVE.has(status) && status !== "completed";
  return (
    <ol className="space-y-0">
      {PIPELINE.map((node, index) => {
        const done = completed.has(node) || status === "completed";
        const running = !done && (started.has(node) || currentNode === node) && ACTIVE.has(status);
        const broken = !done && failed && (currentNode === node || started.has(node));
        return (
          <li className="relative flex gap-3 pb-4 last:pb-0" key={node}>
            {index < PIPELINE.length - 1 ? <span className={cn("absolute left-[9px] top-5 h-[calc(100%-12px)] w-px", done ? "bg-brand/40" : "bg-line")} /> : null}
            <span className={cn(
              "relative z-10 grid size-[19px] shrink-0 place-items-center rounded-full",
              done ? "bg-brand text-on-brand" : running ? "bg-info-soft text-sky-700" : broken ? "bg-danger-soft text-red-700" : "bg-panel text-line-strong ring-1 ring-line-strong",
            )}>
              {done ? <Check size={12} /> : running ? <LoaderCircle className="animate-spin" size={12} /> : broken ? <XCircle size={12} /> : <Circle size={6} fill="currentColor" />}
            </span>
            <span className="flex min-w-0 flex-1 items-baseline justify-between gap-2">
              <span className={cn("text-[13.5px]", done || running ? "text-ink" : "text-muted-ink", broken && "text-red-800")}>{nodeLabel(node)}</span>
              {latency.get(node) ? <span className="shrink-0 text-xs tabular-nums text-muted-ink">{(latency.get(node)! / 1000).toFixed(1)} 秒</span> : null}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function EventRow({ event }: { event: AgentRunEvent }) {
  const planSteps = event.output_summary.plan_steps;
  return (
    <li className="min-w-0 py-3 first:pt-0 last:pb-0">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <p className="min-w-0 text-[13.5px]"><span className="mr-2 text-xs tabular-nums text-muted-ink">#{event.sequence}</span>{EVENT_LABELS[event.event_type] ?? event.event_type}{event.node_name ? ` · ${nodeLabel(event.node_name)}` : ""}</p>
        <span className="text-xs text-muted-ink">{formatDate(event.created_at)}{event.latency_ms ? ` · ${Math.round(event.latency_ms)} ms` : ""}</span>
      </div>
      {Array.isArray(planSteps) ? <ol className="mt-2 list-decimal space-y-1 pl-5 text-[13px] leading-6 text-ink-soft">{planSteps.map((step, index) => <li key={index}>{typeof step === "object" && step ? String((step as Record<string, unknown>).description ?? "") : String(step)}</li>)}</ol> : null}
      {Object.keys(event.output_summary).length && !Array.isArray(planSteps) ? <JsonDetails value={event.output_summary} /> : null}
      {Object.keys(event.error).length ? <p className="mt-2 rounded bg-danger-soft p-2 text-xs text-red-900">此步骤未完成；请查看运行状态并在需要时重试。</p> : null}
    </li>
  );
}

export function AgentRunPage() {
  const { runId = "" } = useParams();
  return <RunDetail key={runId} runId={runId} />;
}

function RunDetail({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [afterSequence, setAfterSequence] = useState(0);
  const run = useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId), refetchInterval: (query) => ACTIVE.has(query.state.data?.status ?? "") ? 2_500 : false });
  const trace = useQuery({ queryKey: ["trace", runId, afterSequence], queryFn: () => api.trace(runId, afterSequence), refetchInterval: () => ACTIVE.has(run.data?.status ?? "") ? 2_500 : false });
  const output = useQuery({ queryKey: ["output", runId], queryFn: () => api.output(runId), enabled: run.data?.status === "completed" });
  const task = useQuery({ queryKey: ["task", run.data?.task_id], queryFn: () => api.task(run.data!.task_id), enabled: Boolean(run.data?.task_id) });
  const cancel = useMutation({ mutationFn: () => api.cancelRun(runId), onSuccess: () => void client.invalidateQueries({ queryKey: ["run", runId] }) });
  const resume = useMutation({ mutationFn: () => api.resumeRun(runId), onSuccess: () => void client.invalidateQueries({ queryKey: ["run", runId] }) });
  const review = useMutation({
    mutationFn: (action: "rerun" | "close") => api.reviewRun(runId, action),
    onSuccess: (next, action) => {
      if (action === "rerun") navigate(`/agent-runs/${next.id}`);
      else void client.invalidateQueries({ queryKey: ["run", runId] });
    },
  });
  if (run.isPending || trace.isPending) return <Page><LoadingBlock label="读取研究运行…" /></Page>;
  if (run.error instanceof Error || trace.error instanceof Error) return <Page><ErrorBlock error={(run.error ?? trace.error) as Error} /></Page>;
  const current = run.data!;
  const currentTrace = trace.data!;
  const artifactId = typeof output.data?.structured.artifact_id === "string" ? output.data.structured.artifact_id : undefined;
  const proposalId = typeof output.data?.structured.memory_proposal_id === "string" ? output.data.structured.memory_proposal_id : undefined;
  const seconds = current.started_at && current.completed_at ? (new Date(current.completed_at).getTime() - new Date(current.started_at).getTime()) / 1000 : null;
  const mutationError = (cancel.error ?? resume.error ?? review.error) as Error | null;
  return (
    <Page width="wide">
      <Link className="inline-flex items-center gap-1.5 text-[13px] text-muted-ink hover:text-ink" to={`/projects/${current.project_id}/tasks/${current.task_id}`}><ArrowLeft size={15} />返回任务</Link>
      <header className="mt-4 flex min-w-0 flex-wrap items-start justify-between gap-5 border-b pb-6">
        <div className="min-w-0 max-w-3xl">
          <p className="text-[13px] text-muted-ink">研究运行</p>
          <h1 className="mt-1 break-words text-[22px] font-semibold leading-8 tracking-tight">{task.data?.title ?? "研究运行"}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-muted-ink">
            <StatusBadge status={current.status} />
            <span>{formatDate(current.created_at)}</span>
            <span>模型 {current.model_name}</span>
            {seconds != null ? <span>用时 {seconds.toFixed(1)} 秒</span> : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {artifactId ? <Link className={buttonVariants()} to={`/artifacts/${artifactId}`}><FileText size={15} />阅读报告</Link> : null}
          {ACTIVE.has(current.status) ? <Button variant="outline" disabled={cancel.isPending} onClick={() => cancel.mutate()}><XCircle size={15} />取消运行</Button> : null}
          {current.status === "failed" ? <Button disabled={resume.isPending} onClick={() => resume.mutate()}><RotateCcw size={15} />继续运行</Button> : null}
          {current.status === "needs_review" ? <><Button disabled={review.isPending} onClick={() => review.mutate("rerun")}><RotateCcw size={15} />用新快照重新运行</Button><Button variant="outline" disabled={review.isPending} onClick={() => review.mutate("close")}><XCircle size={15} />关闭此运行</Button></> : null}
          {current.status === "stale_context" ? <Link className={buttonVariants()} to={`/projects/${current.project_id}/tasks/${current.task_id}`}><RotateCcw size={15} />前往任务重新冻结快照</Link> : null}
        </div>
      </header>
      {mutationError ? <div className="mt-5"><ErrorBlock error={mutationError} /></div> : null}
      {current.error_message ? (
        <Notice className="mt-5" tone="danger" icon={<AlertCircle size={15} />} title="运行未完成">
          <p>{safeRunError(current.error_code, current.error_message)}</p>
          {current.status === "needs_review" ? <pre className="mt-2 max-h-40 max-w-full overflow-auto whitespace-pre-wrap rounded bg-panel/70 p-3 text-xs">{current.error_message}</pre> : null}
        </Notice>
      ) : null}
      {proposalId ? <Notice className="mt-5" tone="warning" title="智能体提出了一条项目记忆" action={<Link className="text-[13px] font-medium underline" to={`/review?tab=memory&proposal=${proposalId}`}>去审核</Link>}>需要你批准后才会写入项目记忆。</Notice> : null}

      <section className="mt-6 grid min-w-0 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_340px]" data-testid="agent-trace-grid">
        <div className="min-w-0 space-y-6">
          <Card>
            <CardHeader><CardTitle title="运行结果" /></CardHeader>
            <CardContent>
              {current.status === "completed" && output.isPending ? <LoadingBlock label="读取输出摘要…" /> : null}
              {output.error instanceof Error ? <ErrorBlock error={output.error} /> : null}
              {output.data ? (
                <div className="space-y-3 text-[13.5px]">
                  <p className="flex items-center gap-1.5 text-emerald-800"><Check size={15} />{output.data.validation.passed === false ? "引用校验未通过" : "报告已生成，引用校验通过"}</p>
                  {artifactId ? <Link className="inline-flex items-center gap-1 font-medium text-brand hover:underline" to={`/artifacts/${artifactId}`}>阅读报告并逐条核对引用<ArrowRight size={14} /></Link> : null}
                  <Disclosure summary="校验详情"><pre className="max-h-48 max-w-full overflow-auto text-xs leading-5 text-ink-soft">{JSON.stringify(output.data.validation, null, 2)}</pre></Disclosure>
                </div>
              ) : current.status === "completed" ? <p className="text-[13px] text-muted-ink">完成后将显示校验结果与报告链接。</p> : <p className="text-[13px] text-muted-ink">{ACTIVE.has(current.status) ? "运行进行中，完成后会在这里显示报告。" : "此运行未完成，因此没有可展示的输出。"}</p>}
            </CardContent>
          </Card>
          <Disclosure summary={`完整执行记录 · ${currentTrace.events.length} 个事件 · ${currentTrace.tool_calls.length} 次证据读取`}>
            {currentTrace.events.length ? <ol className="min-w-0 divide-y">{currentTrace.events.map((event) => <EventRow key={event.id} event={event} />)}</ol> : <EmptyBlock className="py-6" title="尚无事件">运行开始后会逐步记录每个步骤；不会展示模型的思维过程或原始提示词。</EmptyBlock>}
            {currentTrace.next_event_sequence ? <Button className="mt-4" size="sm" variant="outline" onClick={() => setAfterSequence(currentTrace.next_event_sequence!)}>加载后续事件</Button> : null}
            {currentTrace.tool_calls.length ? (
              <div className="mt-5 border-t pt-4">
                <p className="mb-2 text-[13px] font-medium">证据读取</p>
                <div className="space-y-2">
                  {currentTrace.tool_calls.map((call) => (
                    <div key={call.id} className="min-w-0 rounded-lg border p-3">
                      <div className="flex min-w-0 justify-between gap-2"><p className="min-w-0 break-words text-[13px] font-medium">{TOOL_LABELS[call.tool_name] ?? call.tool_name}</p><StatusBadge status={call.status} /></div>
                      <p className="mt-0.5 break-all text-xs text-muted-ink">{call.tool_name} · 只读 · {shortId(call.result_hash)}</p>
                      <JsonDetails value={call.result_summary} />
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </Disclosure>
        </div>
        <aside className="min-w-0 space-y-5">
          <Card>
            <CardHeader><CardTitle title="执行步骤" /></CardHeader>
            <CardContent><Pipeline currentNode={current.current_node} events={currentTrace.events} status={current.status} /></CardContent>
          </Card>
          <Card>
            <CardContent className="grid grid-cols-2 gap-4 text-[13px]">
              <div><p className="text-muted-ink">Token 用量</p><p className="mt-0.5 font-semibold tabular-nums">{(currentTrace.token_usage.total_tokens ?? 0).toLocaleString()}<span className="font-normal text-muted-ink"> / {current.token_budget.toLocaleString()}</span></p></div>
              <div><p className="text-muted-ink">证据读取</p><p className="mt-0.5 font-semibold tabular-nums">{current.tool_call_count} 次</p></div>
              <div><p className="text-muted-ink">自动修复</p><p className="mt-0.5 font-semibold tabular-nums">{current.repair_count} 次</p></div>
              <div><p className="text-muted-ink">上下文快照</p><p className="mt-0.5 truncate font-mono text-xs" title={current.context_snapshot_id}>{shortId(current.context_snapshot_id)}</p></div>
            </CardContent>
          </Card>
        </aside>
      </section>
      <div className="mt-10"><RunFeedbackPanel key={current.id} run={current} output={output.data} /></div>
    </Page>
  );
}
