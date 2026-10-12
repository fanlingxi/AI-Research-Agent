import { useQueries, useQuery } from "@tanstack/react-query";
import { ArrowLeft, FileText, Gavel, History } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { AgentPanel } from "../components/AgentPanel";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { TaskGoalEditor } from "../components/TaskGoalEditor";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { api } from "../lib/api";
import { nodeLabel, PRIORITY_LABELS } from "../lib/labels";
import { formatDate } from "../lib/utils";

export function TaskDetailPage() {
  const { projectId = "", taskId = "" } = useParams();
  const task = useQuery({ queryKey: ["task", taskId], queryFn: () => api.task(taskId) });
  const [artifacts, decisions, runs, project] = useQueries({ queries: [
    { queryKey: ["artifacts", projectId], queryFn: () => api.artifacts(projectId) },
    { queryKey: ["decisions", projectId], queryFn: () => api.decisions(projectId) },
    { queryKey: ["task-runs", projectId, taskId], queryFn: () => api.taskRuns(projectId, taskId) },
    { queryKey: ["project", projectId], queryFn: () => api.project(projectId) },
  ] });
  if (task.isPending) return <div className="p-6"><LoadingBlock label="读取任务…" /></div>;
  if (task.error instanceof Error) return <div className="p-6"><ErrorBlock error={task.error} onRetry={() => task.refetch()} /></div>;
  if (!task.data) return <div className="p-6"><LoadingBlock label="读取任务…" /></div>;
  const relatedArtifacts = (artifacts.data?.filter((item) => item.task_id === taskId) ?? []).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const relatedDecisions = decisions.data?.filter((item) => item.status === "accepted" && (!item.task_id || item.task_id === taskId)) ?? [];
  return (
    <div className="grid min-h-screen min-w-0 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px]">
      <div className="min-w-0 px-4 pb-16 pt-6 sm:px-6 lg:px-10 lg:pt-9">
        <Link className="inline-flex items-center gap-1.5 text-[13px] text-muted-ink hover:text-ink" to={`/projects/${projectId}/tasks`}><ArrowLeft size={15} />{project.data?.name ?? "项目"} · 任务</Link>
        <header className="mt-4 border-b pb-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <h1 className="min-w-0 max-w-3xl break-words text-[22px] font-semibold leading-8 tracking-tight">{task.data.title}</h1>
            <StatusBadge status={task.data.status} />
          </div>
          <p className="mt-1.5 text-xs text-muted-ink">{PRIORITY_LABELS[task.data.priority] ?? task.data.priority}优先级 · 更新于 {formatDate(task.data.updated_at)}</p>
          <p className="mt-4 max-w-3xl whitespace-pre-wrap break-words text-[14.5px] leading-7 text-ink-soft">{task.data.goal || "未填写任务目标。"}</p>
          <TaskGoalEditor key={`${task.data.id}:${task.data.revision}`} task={task.data} />
        </header>
        <div className="mt-6 grid min-w-0 gap-5 xl:grid-cols-2">
          <Card>
            <CardHeader><CardTitle icon={<FileText size={16} />} title="报告" /></CardHeader>
            <CardContent className="space-y-1 pt-3">
              {relatedArtifacts.map((artifact, index) => (
                <Link className="flex items-center justify-between gap-3 rounded-lg px-2 py-2 hover:bg-subtle" key={artifact.id} to={`/artifacts/${artifact.id}`}>
                  <span className="min-w-0"><span className="block text-[13.5px] font-medium">第 {relatedArtifacts.length - index} 版报告</span><span className="text-xs text-muted-ink">{formatDate(artifact.created_at)}</span></span>
                  {artifact.status !== "ready" ? <StatusBadge status={artifact.status} /> : null}
                </Link>
              ))}
              {!relatedArtifacts.length ? <p className="text-[13px] text-muted-ink">研究运行完成后，报告会出现在这里。</p> : null}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle icon={<Gavel size={16} />} title="相关项目决策" /></CardHeader>
            <CardContent className="space-y-2.5 pt-3">
              {relatedDecisions.map((decision) => <div key={decision.id} className="rounded-lg border p-3"><p className="font-medium">{decision.summary}</p><p className="mt-1 text-[13px] text-muted-ink">{decision.impact || decision.rationale}</p></div>)}
              {!relatedDecisions.length ? <p className="text-[13px] text-muted-ink">没有影响此任务的项目决策。</p> : null}
            </CardContent>
          </Card>
          <Card className="xl:col-span-2">
            <CardHeader><CardTitle icon={<History size={16} />} title="研究运行" description="每次运行使用一个冻结的上下文快照，旧运行与报告不会被修改。" /></CardHeader>
            <CardContent className="pt-3">
              <div className="divide-y">
                {runs.data?.items.map((run) => (
                  <Link className="flex items-center justify-between gap-3 px-1 py-3 first:pt-1 last:pb-1 hover:text-brand" key={run.id} to={`/agent-runs/${run.id}`}>
                    <span className="min-w-0"><span className="block font-medium">{formatDate(run.created_at)}</span><span className="mt-0.5 block text-xs text-muted-ink">{nodeLabel(run.current_node)} · {run.model_name}</span></span>
                    <StatusBadge status={run.status} />
                  </Link>
                ))}
              </div>
              {!runs.data?.items.length ? <EmptyBlock className="py-7" title="还没有研究运行">在右侧冻结上下文快照后开始研究。</EmptyBlock> : null}
            </CardContent>
          </Card>
        </div>
      </div>
      <AgentPanel key={`${task.data.id}:${task.data.revision}`} projectId={projectId} task={task.data} />
    </div>
  );
}
