import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, FileKey2, Play, Sparkles } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, api, type ContextSnapshotSummary, type WorkspaceTask } from "../lib/api";
import { formatDate, shortId } from "../lib/utils";
import { ErrorBlock, LoadingBlock } from "./AsyncState";
import { StatusBadge } from "./StatusBadge";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader } from "./ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";

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
      <Button className="mt-3 w-full" size="sm" variant="ghost" onClick={() => setOpen(true)}>Inspect snapshot items</Button>
      <DialogContent>
        <DialogTitle>ContextSnapshot contents</DialogTitle>
        <DialogDescription>这是 AgentRun 的不可变、按需读取输入索引；不会发送完整 ContextPackage 到普通 UI。</DialogDescription>
        <div className="mt-5 space-y-2">
          {items.isPending ? <LoadingBlock label="读取 Snapshot items…" /> : null}
          {items.error instanceof Error ? <ErrorBlock error={items.error} onRetry={() => items.refetch()} /> : null}
          {items.data?.items.map((item) => <div className="rounded-lg border bg-white p-3" key={`${item.section}:${item.item_type}:${item.item_id}`}><div className="flex items-center justify-between gap-2"><p className="font-medium text-sm">{item.item_type}</p><span className="text-xs text-muted-ink">{item.estimated_tokens} tokens</span></div><p className="mt-1 font-mono text-[10px] text-muted-ink">{item.item_id}</p><p className="mt-2 text-xs leading-5 text-slate-600">{item.selected_reason}</p></div>)}
          {items.data && !items.data.items.length ? <p className="text-sm text-muted-ink">此 Snapshot 没有可展示的 item。</p> : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SnapshotSummary({ snapshot, label }: { snapshot: ContextSnapshotSummary; label: string }) {
  return (
    <div className="rounded-lg border bg-white p-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-slate-700">{label}</span>
        <Badge tone={snapshot.persisted ? "success" : "warning"}>{snapshot.persisted ? "persisted" : "preview"}</Badge>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-muted-ink">
        <div><dt>Budget</dt><dd className="mt-0.5 font-medium text-ink">{snapshot.used_tokens} / {snapshot.token_budget}</dd></div>
        <div><dt>Claim bundles</dt><dd className="mt-0.5 font-medium text-ink">{snapshot.item_counts.knowledge_claim_bundles ?? 0}</dd></div>
      </dl>
      {snapshot.collection_scopes.length ? <p className="mt-3 truncate text-muted-ink">Scopes: {snapshot.collection_scopes.join(", ")}</p> : null}
      {snapshot.diagnostics.notices.length ? <p className="mt-2 text-amber-800">{snapshot.diagnostics.notices[0]}</p> : null}
      {snapshot.persisted && snapshot.id ? <p className="mt-2 font-mono text-[10px] text-slate-500">{shortId(snapshot.id)}</p> : null}
      {snapshot.persisted ? <SnapshotItemsDialog snapshot={snapshot} /> : null}
    </div>
  );
}

export function AgentPanel({ projectId, task }: { projectId: string; task?: WorkspaceTask }) {
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
    <aside className="border-l bg-panel p-4 lg:sticky lg:top-0 lg:h-screen lg:overflow-y-auto">
      <div className="mb-5 flex items-center gap-2">
        <span className="grid size-8 place-items-center rounded-lg bg-brand-soft text-brand"><Bot size={18} /></span>
        <div><h2 className="text-sm font-semibold">Research Agent</h2><p className="text-xs text-muted-ink">Project-scoped execution</p></div>
      </div>

      {!task ? (
        <Card><CardContent className="py-8 text-center text-sm text-muted-ink">选择一个 WorkspaceTask 后，才能预览 Context 或发起 AgentRun。</CardContent></Card>
      ) : (
        <div className="space-y-4">
          <Card>
            <CardHeader><div><p className="text-xs text-muted-ink">Current task</p><h3 className="mt-1 text-sm font-semibold">{task.title}</h3></div><StatusBadge status={task.status} /></CardHeader>
            <CardContent><p className="line-clamp-3 text-sm leading-6 text-slate-600">{task.goal || "未提供任务目标。"}</p><p className="mt-3 text-xs text-muted-ink">revision {task.revision} · {formatDate(task.updated_at)}</p></CardContent>
          </Card>

          <div className="space-y-2">
            <Button className="w-full" variant="secondary" disabled={previewMutation.isPending} onClick={() => previewMutation.mutate()}>
              <Sparkles size={16} /> {previewMutation.isPending ? "构建预览…" : "Preview context"}
            </Button>
            {preview ? <SnapshotSummary snapshot={preview} label="Preview" /> : null}
            <Button className="w-full" variant="outline" disabled={snapshotMutation.isPending} onClick={() => snapshotMutation.mutate()}>
              <FileKey2 size={16} /> {snapshotMutation.isPending ? "持久化中…" : "Create snapshot"}
            </Button>
            {snapshot ? <SnapshotSummary snapshot={snapshot} label="Immutable input" /> : null}
          </div>

          <label className="flex items-start gap-2 rounded-lg border bg-white p-3 text-xs text-slate-600">
            <input checked={createProposal} className="mt-0.5 accent-brand" type="checkbox" onChange={(event) => setCreateProposal(event.target.checked)} />
            <span><strong className="font-medium text-ink">Allow MemoryProposal</strong><br />Agent may create a proposed decision only. Human review and commit remain required.</span>
          </label>
          <Button className="w-full" disabled={!snapshot?.id || startMutation.isPending} onClick={() => startMutation.mutate()}>
            <Play size={16} /> {startMutation.isPending ? "正在排队…" : "Run research agent"}
          </Button>

          {error instanceof Error ? <ErrorBlock error={error as ApiError} /> : null}

          <section>
            <div className="mb-2 flex items-center justify-between"><h3 className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Recent runs</h3><span className="text-xs text-muted-ink">{runs.data?.items.length ?? 0}</span></div>
            {runs.isPending ? <LoadingBlock label="读取运行记录…" /> : null}
            <div className="space-y-2">
              {runs.data?.items.slice(0, 5).map((run) => (
                <button key={run.id} className="w-full rounded-lg border bg-white p-3 text-left hover:border-brand/40" onClick={() => navigate(`/agent-runs/${run.id}`)}>
                  <div className="flex items-center justify-between gap-2"><StatusBadge status={run.status} /><span className="font-mono text-[10px] text-muted-ink">{shortId(run.id)}</span></div>
                  <p className="mt-2 text-xs text-slate-600">{run.current_node ?? "queued"} · {formatDate(run.updated_at)}</p>
                </button>
              ))}
            </div>
          </section>
        </div>
      )}
    </aside>
  );
}
