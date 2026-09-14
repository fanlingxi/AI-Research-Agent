import { TaskGoalEditor } from "../components/TaskGoalEditor";
import { useQueries, useQuery } from "@tanstack/react-query";
import { ArrowLeft, FileKey2, FileText, Gavel, History } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { api } from "../lib/api";
import { formatDate, shortId } from "../lib/utils";
import { AgentPanel } from "../components/AgentPanel";
import { ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Card, CardContent, CardHeader } from "../components/ui/card";

export function TaskDetailPage() {
  const { projectId = "", taskId = "" } = useParams();
  const task = useQuery({ queryKey: ["task", taskId], queryFn: () => api.task(taskId) });
  const [artifacts, decisions, runs] = useQueries({ queries: [
    { queryKey: ["artifacts", projectId], queryFn: () => api.artifacts(projectId) },
    { queryKey: ["decisions", projectId], queryFn: () => api.decisions(projectId) },
    { queryKey: ["task-runs", projectId, taskId], queryFn: () => api.taskRuns(projectId, taskId) },
  ] });
  if (task.isPending) return <div className="p-6"><LoadingBlock /></div>;
  if (task.error instanceof Error) return <div className="p-6"><ErrorBlock error={task.error} onRetry={() => task.refetch()} /></div>;
  if (!task.data) return <div className="p-6"><LoadingBlock label="等待 WorkspaceTask 数据…" /></div>;
  const relatedArtifacts = artifacts.data?.filter((item) => item.task_id === taskId) ?? [];
  const relatedDecisions = decisions.data?.filter((item) => item.status === "accepted" && (!item.task_id || item.task_id === taskId)) ?? [];
  return <div className="grid min-h-screen grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]"><div className="min-w-0 p-6 lg:p-8"><Link className="inline-flex items-center gap-2 text-sm font-medium text-muted-ink hover:text-brand" to={`/projects/${projectId}/tasks`}><ArrowLeft size={16} /> Back to tasks</Link><header className="mt-6 border-b pb-6"><div className="flex flex-wrap items-start justify-between gap-4"><div><p className="text-sm font-medium text-brand">WorkspaceTask</p><h1 className="mt-1 text-2xl font-semibold tracking-tight">{task.data.title}</h1></div><StatusBadge status={task.data.status} /></div><p className="mt-4 max-w-3xl whitespace-pre-wrap text-[15px] leading-7 text-slate-700">{task.data.goal || "未设置任务目标。"}</p><div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted-ink"><span>priority {task.data.priority}</span><span>revision {task.data.revision}</span><span>updated {formatDate(task.data.updated_at)}</span></div><TaskGoalEditor key={`${task.data.id}:${task.data.revision}`} task={task.data} /></header><div className="mt-6 grid gap-5 xl:grid-cols-2"><Card><CardHeader><div className="flex items-center gap-2"><Gavel size={17} className="text-brand" /><h2 className="font-semibold">Accepted decisions</h2></div></CardHeader><CardContent className="space-y-3">{relatedDecisions.map((decision) => <div key={decision.id} className="rounded-lg border bg-white p-3"><p className="font-medium">{decision.summary}</p><p className="mt-1 text-sm text-muted-ink">{decision.impact || decision.rationale}</p></div>)}{!relatedDecisions.length ? <p className="text-sm text-muted-ink">没有影响此任务的 accepted Decision。</p> : null}</CardContent></Card><Card><CardHeader><div className="flex items-center gap-2"><FileText size={17} className="text-brand" /><h2 className="font-semibold">Related artifacts</h2></div></CardHeader><CardContent className="space-y-3">{relatedArtifacts.map((artifact) => <Link className="block rounded-lg border bg-white p-3 hover:border-brand/40" key={artifact.id} to={`/artifacts/${artifact.id}`}><div className="flex justify-between gap-3"><p className="font-medium">{artifact.type} · v{artifact.version}</p><StatusBadge status={artifact.status} /></div></Link>)}{!relatedArtifacts.length ? <p className="text-sm text-muted-ink">运行完成后会显示与此任务关联的 Artifact。</p> : null}</CardContent></Card><Card className="xl:col-span-2"><CardHeader><div className="flex items-center gap-2"><History size={17} className="text-brand" /><h2 className="font-semibold">Context Snapshot & AgentRun history</h2></div></CardHeader><CardContent className="space-y-3">{runs.data?.items.map((run) => <Link className="flex items-center justify-between gap-3 rounded-lg border bg-white p-4 hover:border-brand/40" key={run.id} to={`/agent-runs/${run.id}`}><div><div className="flex items-center gap-2"><FileKey2 size={16} className="text-brand" /><p className="font-medium">{shortId(run.context_snapshot_id)}</p></div><p className="mt-1 text-xs text-muted-ink">{run.workflow_name} · {run.current_node ?? "queued"} · {formatDate(run.updated_at)}</p></div><StatusBadge status={run.status} /></Link>)}{!runs.data?.items.length ? <p className="text-sm text-muted-ink">右侧创建的 immutable Snapshot 将成为 AgentRun 的唯一正式输入。</p> : null}</CardContent></Card></div></div><AgentPanel key={`${task.data.id}:${task.data.revision}`} projectId={projectId} task={task.data} /></div>;
}
