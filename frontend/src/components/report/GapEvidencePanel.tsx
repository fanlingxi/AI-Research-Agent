import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Search, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, type AgentOutput, type GapEvidence, type ResearchCommandInput } from "../../lib/api";
import { invalidateKnowledgeReview } from "../../lib/knowledgeQueries";
import { ErrorBlock, Notice } from "../AsyncState";
import { StatusBadge } from "../StatusBadge";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { pageLabel } from "./SourceDrawer";

/** Only structured reports state per-question gaps; classic limitations are disclaimers. */
export function reportGaps(output?: AgentOutput): string[] {
  const draft = output?.structured.draft as { answer_status?: unknown; limitations?: unknown } | undefined;
  if (draft?.answer_status !== "partial" && draft?.answer_status !== "insufficient_evidence") return [];
  return Array.isArray(draft.limitations) ? draft.limitations.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
}

/** Links from a report to the reruns started from its gaps; the report itself never changes. */
export function GapRerunLinks({ runId }: { runId: string }) {
  const reruns = useQuery({ queryKey: ["gap-reruns", runId], queryFn: () => api.gapReruns(runId), refetchInterval: 5000 });
  if (!reruns.data?.length) return null;
  return (
    <Notice tone="info" title="补充证据后的重新研究">
      <ul className="space-y-1.5">
        {reruns.data.map((item) => (
          <li className="flex flex-wrap items-center gap-2" key={item.command_id}>
            <span>缺口 {item.gap_index + 1}：</span>
            {item.artifact ? <Link className="text-brand hover:underline" to={`/artifacts/${item.artifact.id}`}>阅读新报告</Link>
              : item.run ? <Link className="text-brand hover:underline" to={`/agent-runs/${item.run.id}`}>查看研究运行</Link>
                : <span className="text-muted-ink">正在准备…</span>}
            {item.run ? <StatusBadge status={item.run.status} /> : null}
          </li>
        ))}
      </ul>
    </Notice>
  );
}

export function GapEvidencePanel({ runId, projectId, taskId, output }: { runId: string; projectId: string; taskId: string; output: AgentOutput }) {
  const gaps = reportGaps(output);
  if (!gaps.length) return null;
  return (
    <Card aria-label="证据缺口">
      <CardHeader><CardTitle title="证据缺口" description="报告没能回答的部分。可以查找知识库里还没用上的证据、审核待审候选，再用最新知识重新研究；这份报告保持不变。" /></CardHeader>
      <CardContent className="space-y-4">
        {gaps.map((gap, index) => <GapItem gap={gap} index={index} key={index} output={output} projectId={projectId} runId={runId} taskId={taskId} />)}
      </CardContent>
    </Card>
  );
}

function GapItem({ runId, projectId, taskId, output, gap, index }: { runId: string; projectId: string; taskId: string; output: AgentOutput; gap: string; index: number }) {
  const find = useMutation({ mutationFn: () => api.gapEvidence(runId, output.output_sha256, index) });
  return (
    <div className="rounded-lg border p-3.5">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <p className="min-w-0 flex-1 break-words text-[13.5px] leading-6"><span className="mr-1.5 text-muted-ink">缺口 {index + 1}</span>{gap}</p>
        <Button size="sm" variant="outline" disabled={find.isPending} onClick={() => find.mutate()}>
          <Search size={14} />{find.isPending ? "正在查找…" : find.data ? "重新查找" : "查找补充证据"}
        </Button>
      </div>
      {find.error ? <div className="mt-3"><ErrorBlock error={find.error} title="没能查找补充证据" /></div> : null}
      {find.data ? <GapResults data={find.data} gapIndex={index} onChanged={() => find.mutate()} output={output} projectId={projectId} runId={runId} taskId={taskId} /> : null}
    </div>
  );
}

function GapResults({ data, runId, projectId, taskId, output, gapIndex, onChanged }: {
  data: GapEvidence; runId: string; projectId: string; taskId: string; output: AgentOutput; gapIndex: number; onChanged: () => void;
}) {
  const empty = !data.reviewed.length && !data.pending.length;
  // Nothing is preselected: lexical matches can be weak, and pins take snapshot budget.
  const [chosen, setChosen] = useState<string[]>([]);
  const claimIds = data.reviewed.map((item) => item.claim_id).filter((id) => chosen.includes(id));
  return (
    <div className="mt-3 space-y-4 border-t pt-3 text-[13px]">
      {data.reviewed.length ? (
        <section className="space-y-2">
          <p className="font-medium">已审核、但没有用在这份报告里的知识（{data.reviewed.length}）</p>
          <p className="text-xs text-muted-ink">勾选确实能补上这个缺口的条目，重新研究时会优先放入新快照。</p>
          {data.reviewed.map((item) => {
            const evidence = item.evidence[0];
            return (
              <label className="flex cursor-pointer gap-2.5 rounded-md bg-subtle/70 p-3" key={item.claim_id}>
                <input
                  type="checkbox" className="mt-1.5 size-3.5 shrink-0 accent-brand" checked={chosen.includes(item.claim_id)}
                  onChange={(event) => setChosen((current) => event.target.checked ? [...current, item.claim_id] : current.filter((id) => id !== item.claim_id))}
                />
                <span className="min-w-0 flex-1">
                <span className="line-clamp-4 break-words leading-6">{item.statement}</span>
                {evidence ? <>
                  {evidence.quote.trim() === item.statement.trim() ? null : <span className="mt-1.5 line-clamp-3 break-words border-l-2 border-line-strong pl-2.5 text-xs leading-5 text-ink-soft">{evidence.quote}</span>}
                  <span className="mt-1 block text-xs text-muted-ink">{evidence.document_title} · {pageLabel(evidence.page_start, evidence.page_end)}</span>
                </> : null}
                </span>
              </label>
            );
          })}
        </section>
      ) : null}
      {data.reviewed_in_snapshot_count ? (
        <p className="leading-6 text-muted-ink">{data.reviewed.length ? "另有" : "相关的已审核知识中有"} {data.reviewed_in_snapshot_count} 条已经在这份报告的快照里；如果它们已能回答这个缺口，问题可能出在回答环节，可以在下方登记反馈。</p>
      ) : null}
      {data.pending.length ? (
        <section className="space-y-2">
          <p className="font-medium">待审核候选（{data.pending.length}，审核通过后才会用于研究）</p>
          {data.pending.map((item) => <PendingCandidate item={item} key={item.candidate_id} onDecided={onChanged} />)}
        </section>
      ) : null}
      {empty && !data.reviewed_in_snapshot_count ? (
        <p className="leading-6 text-muted-ink">
          知识库和待审候选里都没有找到相关内容，可能需要导入新的资料。
          <Link className="ml-1 text-brand hover:underline" to="/knowledge">去导入资料</Link>
        </p>
      ) : null}
      {data.search.method === "lexical" && data.search.notices.length ? <p className="text-xs text-muted-ink">本次只用关键词检索，可能漏掉用其他语言或说法表述的内容。</p> : null}
      {data.can_rerun ? <RerunButton claimIds={claimIds} gapIndex={gapIndex} output={output} projectId={projectId} runId={runId} taskId={taskId} /> : null}
    </div>
  );
}

function PendingCandidate({ item, onDecided }: { item: GapEvidence["pending"][number]; onDecided: () => void }) {
  const client = useQueryClient();
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") => api.decideCandidate(item.candidate_id, decision),
    onSuccess: () => {
      invalidateKnowledgeReview(client, item.ingestion_id);
      onDecided();
    },
  });
  const reviewLink = `/review?collection=${encodeURIComponent(item.collection_slug)}&ingestion=${encodeURIComponent(item.ingestion_id)}`;
  return (
    <div className="rounded-md border p-3">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Badge tone="neutral">{item.kind === "entity" ? "实体" : "关系"}</Badge>
        <p className="min-w-0 break-words font-medium">{item.title}</p>
      </div>
      {item.summary ? <p className="mt-1 break-words leading-6 text-ink-soft">{item.summary}</p> : null}
      {item.quote ? <blockquote className="mt-1.5 line-clamp-3 break-words border-l-2 border-line-strong pl-2.5 text-xs leading-5 text-ink-soft">{item.quote}</blockquote> : null}
      <p className="mt-1 text-xs text-muted-ink">{item.paper_title} · {pageLabel(item.page_start, item.page_end)}</p>
      {item.prereview ? <p className="mt-1 text-xs text-muted-ink">AI 预审：{item.prereview.reason}</p> : null}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {decide.isSuccess ? <span className="text-xs text-emerald-800">{decide.variables === "approve" ? "已批准" : "已驳回"}</span> : <>
          <Button size="sm" disabled={decide.isPending} onClick={() => decide.mutate("approve")}><Check size={14} />批准</Button>
          <Button size="sm" variant="outline" disabled={decide.isPending} onClick={() => decide.mutate("reject")}><X size={14} />驳回</Button>
        </>}
        <Link className="text-xs text-brand hover:underline" to={reviewLink}>在审核页查看</Link>
      </div>
      {decide.error ? <div className="mt-2"><ErrorBlock error={decide.error} title="审核没有完成" /></div> : null}
    </div>
  );
}

type Pending = { key: string; payload: ResearchCommandInput; commandId?: string };

function readPending(storageKey: string): Pending | null {
  try { return JSON.parse(localStorage.getItem(storageKey) ?? "null") as Pending | null; }
  catch { return null; }
}

function rememberPending(storageKey: string, value: Pending | null) {
  try {
    if (value) localStorage.setItem(storageKey, JSON.stringify(value));
    else localStorage.removeItem(storageKey);
  } catch { /* The idempotency key only protects against duplicate clicks in this tab then. */ }
}

function RerunButton({ runId, projectId, taskId, output, gapIndex, claimIds }: { runId: string; projectId: string; taskId: string; output: AgentOutput; gapIndex: number; claimIds: string[] }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const storageKey = `research-gap-rerun:${runId}:${gapIndex}`;
  const [pending, setPending] = useState(() => readPending(storageKey));
  const submitLock = useRef(false);
  const start = useMutation({
    mutationFn: (value: Pending) => api.submitResearchCommand(value.payload, value.key),
    onSuccess: (command, value) => {
      const next = { ...value, commandId: command.id };
      rememberPending(storageKey, next);
      setPending(next);
      client.setQueryData(["research-command", command.id], command);
      void client.invalidateQueries({ queryKey: ["gap-reruns", runId] });
    },
    onSettled: () => { submitLock.current = false; },
  });
  const command = useQuery({
    queryKey: ["research-command", pending?.commandId],
    queryFn: () => api.researchCommand(pending!.commandId!),
    enabled: Boolean(pending?.commandId),
    refetchInterval: (query) => ["failed", "completed"].includes(query.state.data?.status ?? "") || query.state.data?.target_route ? false : 1000,
  });
  const current = command.data ?? start.data;
  useEffect(() => {
    if (!current?.target_route) return;
    rememberPending(storageKey, null);
    navigate(current.target_route);
  }, [current?.target_route, navigate, storageKey]);
  const locked = Boolean(pending?.commandId) || start.isPending;
  return (
    <div className="space-y-2">
      <Button disabled={locked || !claimIds.length} onClick={() => {
        if (submitLock.current) return;
        submitLock.current = true;
        const payload: ResearchCommandInput = {
          mode: "project_run", instruction: "补充证据后重新研究", project_id: projectId,
          task: { kind: "existing", task_id: taskId },
          gap_evidence_of: { run_id: runId, output_sha256: output.output_sha256, gap_index: gapIndex, claim_ids: claimIds },
          create_memory_proposal: false, max_steps: 10, max_tool_calls: 3,
        };
        const value = pending && JSON.stringify(pending.payload) === JSON.stringify(payload) ? pending : { key: crypto.randomUUID(), payload };
        rememberPending(storageKey, value);
        setPending(value);
        start.mutate(value);
      }}>{start.isPending ? "正在提交…" : claimIds.length ? `用选中的 ${claimIds.length} 条知识重新研究` : "用最新知识重新研究"}</Button>
      <p className="text-xs text-muted-ink">{claimIds.length ? "会为同一任务冻结新的快照，优先放入选中的知识（最多占快照预算的一半），再开始一次新运行；" : "先勾选要带入的知识。"}这份报告和它的快照保持不变。</p>
      {current && !current.target_route ? (
        <Notice tone={current.status === "failed" ? "danger" : "info"} title={current.status === "failed" ? "重新研究没能启动" : "正在准备新的研究…"}>
          {current.error ?? "指令已保存，准备完成后会自动打开新运行。"}
          {current.status === "failed" ? <Button className="mt-2" size="sm" variant="outline" onClick={() => { rememberPending(storageKey, null); setPending(null); start.reset(); }}>重新提交</Button> : null}
        </Notice>
      ) : null}
      {start.error ?? command.error ? <ErrorBlock error={(start.error ?? command.error) as Error} title="重新研究没能启动" /> : null}
    </div>
  );
}
