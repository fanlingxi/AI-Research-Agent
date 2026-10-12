import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Lock, Play } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, api, type ContextSnapshotSummary, type WorkspaceTask } from "../lib/api";
import { nodeLabel } from "../lib/labels";
import { cn, formatDate } from "../lib/utils";
import { ErrorBlock, LoadingBlock } from "./AsyncState";
import { StatusBadge } from "./StatusBadge";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";
import { Select } from "./ui/field";

const SECTION_LABELS: Record<string, string> = {
  knowledge_claim_bundles: "知识证据包",
  decisions: "项目决策",
  tasks: "任务信息",
};

function SnapshotItemsDialog({ snapshot }: { snapshot: ContextSnapshotSummary }) {
  const [open, setOpen] = useState(false);
  const items = useQuery({
    queryKey: ["snapshot-items", snapshot.id],
    queryFn: () => api.snapshotItems(snapshot.id!),
    enabled: open && Boolean(snapshot.id),
  });
  if (!snapshot.id) return null;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button className="mt-2 h-auto px-0 text-xs" variant="link" onClick={() => setOpen(true)}>查看快照包含的内容</Button>
      <DialogContent>
        <DialogTitle>快照内容</DialogTitle>
        <DialogDescription>研究运行只能读取这些内容。快照创建后不会再改变。</DialogDescription>
        <div className="mt-5 space-y-2">
          {items.isPending ? <LoadingBlock label="读取快照内容…" /> : null}
          {items.error instanceof Error ? <ErrorBlock error={items.error} onRetry={() => items.refetch()} /> : null}
          {items.data?.items.map((item) => (
            <div className="rounded-lg border p-3" key={`${item.section}:${item.item_type}:${item.item_id}`}>
              <div className="flex items-center justify-between gap-2"><p className="text-sm font-medium">{SECTION_LABELS[item.section] ?? item.item_type}</p><span className="text-xs tabular-nums text-muted-ink">约 {item.estimated_tokens} Token</span></div>
              <p className="mt-1 text-xs leading-5 text-ink-soft">{item.selected_reason}</p>
              <p className="mt-1 break-all font-mono text-[10px] text-muted-ink">{item.item_id}</p>
            </div>
          ))}
          {items.data && !items.data.items.length ? <p className="text-sm text-muted-ink">此快照没有可展示的内容。</p> : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SnapshotSummary({ snapshot }: { snapshot: ContextSnapshotSummary }) {
  const bundles = snapshot.item_counts.knowledge_claim_bundles ?? 0;
  return (
    <div className={cn("rounded-lg border p-3 text-xs", snapshot.persisted ? "border-brand/30 bg-brand-tint" : "bg-subtle/60")}>
      <p className="font-medium text-ink">{snapshot.persisted ? "已冻结" : "预览"}：{bundles} 个知识证据包</p>
      <p className="mt-1 text-muted-ink">约 {snapshot.used_tokens.toLocaleString()} / {snapshot.token_budget.toLocaleString()} Token{snapshot.collection_scopes.length ? ` · 范围：${snapshot.collection_scopes.join("、")}` : ""}</p>
      {snapshot.diagnostics.notices.length ? <p className="mt-1.5 text-amber-800">{snapshot.diagnostics.notices[0]}</p> : null}
      {!bundles ? <p className="mt-1.5 text-amber-800">没有找到相关知识。请检查项目的知识范围，或先审核更多候选知识。</p> : null}
      {snapshot.persisted ? <SnapshotItemsDialog snapshot={snapshot} /> : null}
    </div>
  );
}

function Step({ number, title, done, children }: { number: number; title: string; done?: boolean; children: ReactNode }) {
  return (
    <li className="relative pl-8">
      <span className={cn("absolute left-0 top-0 grid size-5 place-items-center rounded-full text-[11px] font-semibold", done ? "bg-brand text-white" : "bg-subtle text-muted-ink ring-1 ring-line-strong")}>{number}</span>
      <p className="text-[13px] font-medium leading-5 text-ink">{title}</p>
      <div className="mt-2">{children}</div>
    </li>
  );
}

export function AgentPanel({
  projectId,
  task,
  availableTasks,
  onTaskChange,
}: {
  projectId: string;
  task?: WorkspaceTask;
  availableTasks?: WorkspaceTask[];
  onTaskChange?: (taskId: string) => void;
}) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [preview, setPreview] = useState<ContextSnapshotSummary>();
  const [snapshot, setSnapshot] = useState<ContextSnapshotSummary>();
  const [createProposal, setCreateProposal] = useState(true);
  const runs = useQuery({
    queryKey: ["task-runs", projectId, task?.id],
    queryFn: () => api.taskRuns(projectId, task!.id),
    enabled: Boolean(task),
    refetchInterval: (query) =>
      query.state.data?.items.some((run) => ["queued", "preparing", "running", "validating"].includes(run.status))
        ? 2_500
        : false,
  });
  const previewMutation = useMutation({
    mutationFn: () => api.previewContext(projectId, task!.id),
    onSuccess: ({ snapshot: next }) => setPreview(next),
  });
  const snapshotMutation = useMutation({
    mutationFn: () => api.createSnapshot(projectId, task!.id),
    onSuccess: (next) => setSnapshot(next),
  });
  const startMutation = useMutation({
    mutationFn: () => api.startRun(projectId, task!.id, snapshot!.id!, createProposal),
    onSuccess: (run) => {
      void client.invalidateQueries({ queryKey: ["task-runs", projectId, task?.id] });
      navigate(`/agent-runs/${run.id}`);
    },
  });
  const error = previewMutation.error ?? snapshotMutation.error ?? startMutation.error;

  return (
    <aside className="min-w-0 border-t bg-panel px-4 py-6 sm:px-6 lg:sticky lg:top-0 lg:h-screen lg:overflow-y-auto lg:border-l lg:border-t-0 lg:px-5 scroll-thin" aria-label="发起项目研究">
      <h2 className="text-[15px] font-semibold">发起项目研究</h2>
      <p className="mt-1 text-[13px] leading-5 text-muted-ink">先冻结上下文快照，再运行研究智能体。运行只使用冻结时的知识，结果可完整追溯。</p>

      <ol className="mt-5 space-y-5">
        <Step number={1} title="选择任务" done={Boolean(task)}>
          {availableTasks ? (
            <Select aria-label="选择任务" value={task?.id ?? ""} onChange={(event) => { setPreview(undefined); setSnapshot(undefined); onTaskChange?.(event.target.value); }}>
              <option value="">请选择任务</option>
              {availableTasks.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
            </Select>
          ) : null}
          {task ? (
            <div className="mt-2 rounded-lg border p-3">
              <p className="text-xs text-muted-ink">当前任务</p>
              <p className="mt-0.5 text-[13.5px] font-medium leading-5">{task.title}</p>
              <p className="mt-1 line-clamp-3 text-xs leading-5 text-ink-soft">{task.goal || "未填写任务目标。"}</p>
            </div>
          ) : (
            <p className="mt-2 text-xs leading-5 text-muted-ink">选择一个任务后，才能准备上下文并开始研究。{availableTasks && !availableTasks.length ? "当前没有进行中的任务，请先新建任务。" : ""}</p>
          )}
        </Step>
        <Step number={2} title="冻结上下文快照" done={Boolean(snapshot?.id)}>
          <div className="flex flex-wrap gap-2">
            <Button disabled={!task || previewMutation.isPending} size="sm" variant="outline" onClick={() => previewMutation.mutate()}><Eye size={14} />{previewMutation.isPending ? "预览中…" : "预览"}</Button>
            <Button disabled={!task || snapshotMutation.isPending} size="sm" variant={snapshot ? "outline" : "secondary"} onClick={() => snapshotMutation.mutate()}><Lock size={14} />{snapshotMutation.isPending ? "冻结中…" : snapshot ? "重新冻结" : "冻结快照"}</Button>
          </div>
          {preview && !snapshot ? <div className="mt-2"><SnapshotSummary snapshot={preview} /></div> : null}
          {snapshot ? <div className="mt-2"><SnapshotSummary snapshot={snapshot} /></div> : null}
        </Step>
        <Step number={3} title="开始研究">
          <label className="flex items-start gap-2 text-xs leading-5 text-ink-soft">
            <input checked={createProposal} className="mt-0.5 accent-[var(--color-brand)]" type="checkbox" onChange={(event) => setCreateProposal(event.target.checked)} />
            <span>允许智能体提出项目记忆<span className="block text-muted-ink">只会生成待审核的提案，需要你批准。</span></span>
          </label>
          <Button className="mt-3 w-full" disabled={!snapshot?.id || startMutation.isPending} onClick={() => startMutation.mutate()}>
            <Play size={15} />{startMutation.isPending ? "正在排队…" : "开始研究"}
          </Button>
        </Step>
      </ol>

      {error instanceof Error ? <div className="mt-4"><ErrorBlock error={error as ApiError} /></div> : null}

      {task ? (
        <section className="mt-7 border-t pt-5">
          <h3 className="text-[13px] font-semibold">此任务的研究运行</h3>
          {runs.isPending ? <p className="mt-2 text-xs text-muted-ink">读取中…</p> : null}
          <div className="mt-2 space-y-1">
            {runs.data?.items.slice(0, 5).map((run) => (
              <Link className="flex items-center justify-between gap-2 rounded-lg px-2 py-2 hover:bg-subtle" key={run.id} to={`/agent-runs/${run.id}`}>
                <span className="min-w-0"><span className="block truncate text-xs text-ink">{nodeLabel(run.current_node)}</span><span className="text-[11px] text-muted-ink">{formatDate(run.updated_at)}</span></span>
                <StatusBadge status={run.status} />
              </Link>
            ))}
            {runs.data && !runs.data.items.length ? <p className="text-xs text-muted-ink">还没有运行。</p> : null}
          </div>
        </section>
      ) : null}
    </aside>
  );
}
