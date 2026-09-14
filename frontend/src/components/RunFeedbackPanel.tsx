import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type AgentRun, type AgentOutput, type FeedbackInput, type RunFeedback, type RunRecheck } from "../lib/api";
import { shortId } from "../lib/utils";
import { CitationDrawer } from "./CitationDrawer";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./AsyncState";
import { Button } from "./ui/button";
import { StatusBadge } from "./StatusBadge";

const TERMINAL = new Set(["completed", "failed", "needs_review", "stale_context", "cancelled"]);
const field = "mt-1 w-full min-w-0 rounded-lg border bg-white px-3 py-2 text-sm";
const categories = { citation: "引用错误", unsupported: "证据不支持", incomplete: "回答不完整", scope: "资料范围", source_version: "来源版本", execution: "执行失败", other: "其他" };
const states = { pending: "待复核", accepted: "已受理", rejected: "不受理", resolved: "复检已解决" };

function saved<T>(key: string): T | null {
  try { return JSON.parse(sessionStorage.getItem(key) ?? "null") as T | null; } catch { return null; }
}
function persist(key: string, value: unknown) {
  if (value === null) sessionStorage.removeItem(key);
  else sessionStorage.setItem(key, JSON.stringify(value));
}

// Freeze a submitted request until acknowledged. Retrying after a lost response or
// page reload must carry the same key, revision and body to the idempotent API.
function ActionForm<T>({ storageKey, build, send, onDone, label, children }: {
  storageKey: string; build: (form: FormData) => T; send: (body: T) => Promise<unknown>;
  onDone: () => void; label: string; children: ReactNode;
}) {
  const key = `run-review:${storageKey}`;
  const [body, setBody] = useState<T | null>(() => saved<T>(key));
  const mutation = useMutation({ mutationFn: send, retry: false, onSuccess: () => {
    persist(key, null); setBody(null); onDone();
  } });
  const [localError, setLocalError] = useState<Error | null>(null);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const next = body ?? build(new FormData(event.currentTarget));
      persist(key, next); setBody(next); setLocalError(null); mutation.mutate(next);
    } catch (error) { setLocalError(error as Error); }
  }
  const rejected = mutation.error instanceof ApiError && mutation.error.status >= 400 && mutation.error.status < 500;
  return <form onSubmit={submit} className="space-y-3">
    <fieldset disabled={body !== null || mutation.isPending} className="min-w-0 space-y-3">{children}</fieldset>
    {body !== null && <p className="text-sm text-muted-ink">已保留本次提交内容。重试会确认同一操作的结果。</p>}
    {(mutation.error || localError) && <ErrorBlock error={(mutation.error ?? localError)!} />}
    {rejected ? <Button type="button" variant="outline" onClick={() => { persist(key, null); setBody(null); mutation.reset(); onDone(); }}>刷新状态后重新填写</Button> :
      <Button type="submit" disabled={mutation.isPending}>{mutation.isPending ? "正在提交…" : body !== null ? "重试同一提交" : label}</Button>}
  </form>;
}

function PersonNote({ personLabel = "复核人", noteLabel = "复核说明" }: { personLabel?: string; noteLabel?: string }) {
  return <><label className="block text-sm">{personLabel}<input className={field} name="person" required maxLength={200} /></label><label className="block text-sm">{noteLabel}<textarea className={field} name="note" rows={3} required maxLength={3000} /></label></>;
}
const value = (form: FormData, key: string) => String(form.get(key) ?? "").trim();

function CandidateExport({ feedback }: { feedback: RunFeedback }) {
  const [version, setVersion] = useState("");
  const result = useMutation({ mutationFn: () => api.exportFeedback(feedback.run_id, feedback.id, version.trim()) });
  return <details className="rounded-lg border p-3"><summary className="cursor-pointer text-sm text-brand">导出开发集候选</summary>
    <form className="mt-3 space-y-3" onSubmit={(e) => { e.preventDefault(); result.mutate(); }}>
      <p className="text-xs text-muted-ink">仅生成候选文件；仍需数据集审核，不自动导入，也不附加正确答案标签。</p>
      <label className="block text-sm">目标开发集版本<input className={field} required maxLength={128} value={version} onChange={(e) => { setVersion(e.target.value); result.reset(); }} /></label>
      <Button type="submit" variant="outline" disabled={!version.trim() || result.isPending}>生成候选文件</Button>
      {result.error && <ErrorBlock error={result.error} />}
      {result.data && <a className="block text-sm font-medium text-brand underline" download={`${feedback.id}-dev-candidate.json`} href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(result.data, null, 2))}`}>下载候选 JSON</a>}
    </form>
  </details>;
}

function RecheckCard({ link, refresh }: { link: RunRecheck; refresh: () => void }) {
  return <article aria-label="修订运行" className="min-w-0 space-y-3 rounded-xl border bg-white p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium">新旧运行关联</h3><StatusBadge status={link.child_status} /></div>
    <div className="grid min-w-0 gap-3 sm:grid-cols-2">
      <Link className="min-w-0 break-all text-sm text-brand underline" to={`/agent-runs/${link.parent_run_id}#feedback`}>原运行 · {shortId(link.parent_run_id)}</Link>
      <Link className="min-w-0 break-all text-sm text-brand underline" to={`/agent-runs/${link.child_run_id}#feedback`}>修订运行 · {shortId(link.child_run_id)}</Link>
    </div>
    <p className="whitespace-pre-wrap break-words text-sm">修订说明：{link.request.resolution_note}</p>
    {link.request.token_budget != null && <p className="text-xs text-muted-ink">本次生成用量上限：{link.request.token_budget.toLocaleString()} Token</p>}
    {link.request.context_max_tokens != null && <p className="text-xs text-muted-ink">本次快照容量上限：{link.request.context_max_tokens.toLocaleString()} Token</p>}
    <p className="break-all text-xs text-muted-ink">新快照：{link.child_snapshot_id}</p>
    {link.child_artifact_id && <Link className="block text-sm text-brand underline" to={`/artifacts/${link.child_artifact_id}`}>查看修订报告</Link>}
    {link.recheck ? <div className="space-y-2 rounded-lg bg-brand-soft/40 p-3 text-sm"><p className="font-medium">{link.recheck.decision === "resolved" ? "复检：已解决" : "复检：仍未解决"}</p><p>{link.recheck.reviewer} · {link.recheck.note}</p><p className="text-xs text-muted-ink">复检时运行状态：{link.recheck_anchor?.status ?? "未知"}；后续运行变化不会覆盖这次记录。</p></div> : TERMINAL.has(link.child_status) ?
      <ActionForm storageKey={`recheck:${link.id}`} label="提交复检" onDone={refresh}
        build={(form) => ({ decision: value(form, "decision") as "resolved" | "unresolved", reviewer: value(form, "person"), note: value(form, "note") })}
        send={(body) => api.recheckFeedback(link.parent_run_id, link.id, body)}>
        <p className="text-sm text-muted-ink">请对照原问题和修订报告，再记录人工复检结论。</p>
        <label className="block text-sm">复检结论<select className={field} name="decision" defaultValue="unresolved"><option value="unresolved">仍未解决</option><option value="resolved" disabled={link.child_status !== "completed"}>已解决（需报告完成）</option></select></label>
        <PersonNote personLabel="复检人" noteLabel="复检说明" />
      </ActionForm> : <p role="status" className="text-sm text-muted-ink">修订运行尚未结束，等待完成后复检。</p>}
  </article>;
}

function FeedbackCard({ feedback, links, run, refresh }: { feedback: RunFeedback; links: RunRecheck[]; run: AgentRun; refresh: () => void }) {
  return <article aria-label={`反馈：${feedback.request.note}`} className="min-w-0 space-y-4 rounded-xl border bg-panel p-4">
    <div className="flex flex-wrap justify-between gap-2"><h3 className="font-semibold">{categories[feedback.request.category]}</h3><span className="text-sm text-brand">{states[feedback.status]}</span></div>
    <p className="whitespace-pre-wrap break-words text-sm">{feedback.request.note}</p>
    <p className="text-xs text-muted-ink">登记人：{feedback.request.reporter} · {feedback.request.finding_index ? `结论 ${feedback.request.finding_index}` : "整个运行"}</p>
    {feedback.anchor.assertion && <blockquote className="rounded-lg border-l-4 border-brand bg-brand-soft/30 p-3 text-sm">{feedback.anchor.assertion}</blockquote>}
    <details><summary className="cursor-pointer text-sm text-brand">查看登记时的原文与版本</summary><div className="mt-3 space-y-3 text-sm">
      <p className="break-all text-xs text-muted-ink">原快照：{feedback.anchor.snapshot_id} · 报告指纹：{feedback.anchor.output_sha256 ?? "无报告"}</p>
      {feedback.anchor.evidence.map((e) => <blockquote key={e.evidence_id} className="rounded border p-3"><p className="whitespace-pre-wrap break-words">{e.quote}</p><p className="mt-2 break-all text-xs text-muted-ink">来源版本：{e.source_version} · {JSON.stringify(e.location)}</p></blockquote>)}
      {!feedback.anchor.evidence.length && <p>此次反馈未指定引用片段。</p>}
    </div></details>
    {feedback.anchor.artifact_id && <Link className="block text-sm text-brand underline" to={`/artifacts/${feedback.anchor.artifact_id}`}>查看原报告</Link>}
    {feedback.decision && <p className="rounded-lg bg-slate-50 p-3 text-sm">复核：{feedback.decision.reviewer} · {feedback.decision.note}</p>}
    {feedback.status === "pending" && <ActionForm storageKey={`review:${feedback.id}`} label="提交复核" onDone={refresh}
      build={(form) => ({ expected_revision: feedback.revision, decision: value(form, "decision") as "accepted" | "rejected", reviewer: value(form, "person"), note: value(form, "note") })}
      send={(body) => api.reviewFeedback(run.id, feedback.id, body)}>
      <label className="block text-sm">处理决定<select className={field} name="decision"><option value="accepted">受理此问题</option><option value="rejected">不受理（请说明原因）</option></select></label><PersonNote />
    </ActionForm>}
    {links.map((link) => <RecheckCard key={link.id} link={link} refresh={refresh} />)}
    {feedback.status === "accepted" && !links.some((link) => !link.recheck) && (run.revision === feedback.anchor.run_revision && TERMINAL.has(run.status) ?
      <ActionForm storageKey={`rerun:${feedback.id}:${feedback.revision}`} label="创建修订运行" onDone={refresh}
        build={(form) => ({ idempotency_key: crypto.randomUUID(), expected_revision: feedback.revision, resolution_note: value(form, "note"), ...(value(form, "token_budget") ? { token_budget: Number(value(form, "token_budget")) } : {}), ...(value(form, "context_max_tokens") ? { context_max_tokens: Number(value(form, "context_max_tokens")) } : {}) })}
        send={(body) => api.rerunFeedback(run.id, feedback.id, body)}>
        <p className="text-sm text-muted-ink">先在<Link className="text-brand underline" to={`/projects/${run.project_id}/tasks/${run.task_id}`}>任务页</Link>修订目标，或完成<Link className="text-brand underline" to="/review">资料审核</Link>。新运行会使用最新资料并可能调用模型；修订说明仅用于留档。</p>
        <label className="block text-sm">已完成的修订<textarea className={field} name="note" required maxLength={3000} rows={3} /></label>
        <details><summary className="cursor-pointer text-sm text-brand">调整生成用量上限</summary><p className="mt-2 text-xs text-muted-ink">默认沿用原运行的 {run.token_budget.toLocaleString()} Token，支持提高至 1,048,576。快照默认最多 16,000 Token；需要纳入更多已有证据时可单独扩容，最高 262,144，且不能超过新运行总上限。旧快照保持不变。</p><label className="mt-3 block text-sm">新运行 Token 上限（可选）<input className={field} name="token_budget" type="number" min={256} max={1048576} step={1} placeholder={String(run.token_budget)} /></label><label className="mt-3 block text-sm">新快照 Token 上限（可选）<input className={field} name="context_max_tokens" type="number" min={256} max={262144} step={1} placeholder="16000" /></label></details>
      </ActionForm> : <p className="text-sm text-amber-900">原运行已变化，请针对当前结果重新登记反馈。</p>)}
    {(feedback.status === "accepted" || feedback.status === "resolved") && <CandidateExport feedback={feedback} />}
  </article>;
}

export function RunFeedbackPanel({ run, output }: { run: AgentRun; output?: AgentOutput }) {
  const client = useQueryClient();
  const history = useQuery({ queryKey: ["feedback", run.id], queryFn: () => api.feedback(run.id), refetchInterval: 5000 });
  const [finding, setFinding] = useState(0);
  const [evidence, setEvidence] = useState<string[]>([]);
  const [openEvidence, setOpenEvidence] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const draft = output?.structured.draft as { findings?: { assertion: string; evidence_ids: string[] }[] } | undefined;
  const findings = draft?.findings ?? [];
  function refresh() {
    for (const key of ["feedback", "run", "trace"]) void client.invalidateQueries({ queryKey: [key] });
  }
  return <section id="feedback" className="mt-6 min-w-0 space-y-5" aria-label="反馈与复检">
    <header><h2 className="text-xl font-semibold">反馈与复检</h2><p className="mt-2 text-sm text-muted-ink">检查结论及其引用，登记问题并追踪修订。反馈和复检结论由操作人填写，保留为审核记录。</p></header>
    <div className="rounded-xl border bg-panel p-4">
      <h3 className="font-semibold">结论与引用检查</h3><p className="mt-2 text-sm text-muted-ink">本运行未执行在线语义裁判；引用格式校验通过不代表原文支持结论。历史语义观察见<Link to="/experiments#semantic-review" className="text-brand underline">实验对比</Link>。</p>
      {findings.map((item, index) => <div key={index} className="mt-4 space-y-2 border-t pt-3"><p className="whitespace-pre-wrap break-words text-sm">{index + 1}. {item.assertion}</p><div className="flex flex-wrap gap-2">{item.evidence_ids.map((id) => <Button key={id} size="sm" variant="outline" onClick={() => setOpenEvidence(id)}>原文 {shortId(id)}</Button>)}</div></div>)}
      {!findings.length && <p className="mt-3 text-sm text-muted-ink">无可检查的结构化结论，可对整个运行登记问题。</p>}
    </div>
    {history.isPending && <LoadingBlock label="正在读取反馈记录…" />}
    {history.error && <ErrorBlock error={history.error} onRetry={() => void history.refetch()} />}
    {history.data && <>
      {history.data.links.filter((link) => link.child_run_id === run.id).map((link) => <RecheckCard key={link.id} link={link} refresh={refresh} />)}
      {!history.data.feedback.length && <EmptyBlock title="尚无反馈">可为具体结论或整个运行登记问题。</EmptyBlock>}
      {history.data.feedback.map((item) => <FeedbackCard key={item.id} feedback={item} run={run} links={history.data.links.filter((link) => link.feedback_id === item.id)} refresh={refresh} />)}
    </>}
    {TERMINAL.has(run.status) ? <details className="rounded-xl border bg-panel p-4"><summary className="cursor-pointer font-semibold">登记反馈</summary><div className="mt-4">
      <ActionForm key={generation} storageKey={`create:${run.id}`} label="提交反馈" onDone={() => { refresh(); setGeneration((n) => n + 1); setFinding(0); setEvidence([]); }}
        build={(form): FeedbackInput => ({ idempotency_key: crypto.randomUUID(), category: value(form, "category") as FeedbackInput["category"], reporter: value(form, "person"), note: value(form, "note"), finding_index: finding || null, evidence_ids: evidence })}
        send={(body) => api.createFeedback(run.id, body)}>
        <label className="block text-sm">问题类型<select className={field} name="category" defaultValue="incomplete">{Object.entries(categories).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
        <label className="block text-sm">反馈对象<select className={field} value={finding} onChange={(e) => { setFinding(Number(e.target.value)); setEvidence([]); }}><option value={0}>整个运行</option>{findings.map((_, index) => <option key={index} value={index + 1}>结论 {index + 1}</option>)}</select></label>
        {finding > 0 && <div className="space-y-2"><p className="text-sm">关联引用（可选，最多30条）</p>{findings[finding - 1]?.evidence_ids.map((id) => <label className="flex items-start gap-2 break-all text-xs" key={id}><input type="checkbox" checked={evidence.includes(id)} disabled={evidence.length >= 30 && !evidence.includes(id)} onChange={(e) => setEvidence((old) => e.target.checked ? [...old, id] : old.filter((item) => item !== id))} />{id}</label>)}</div>}
        <PersonNote personLabel="登记人" noteLabel="问题说明" />
      </ActionForm>
    </div></details> : <p className="text-sm text-muted-ink">运行结束后可登记反馈。</p>}
    <CitationDrawer snapshotId={run.context_snapshot_id} evidenceId={openEvidence} onOpenChange={(open) => !open && setOpenEvidence(null)} />
  </section>;
}
