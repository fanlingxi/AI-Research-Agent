import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpenCheck, Bot, FileText, FolderKanban, ListTodo, Plus, Scale } from "lucide-react";
import { useState } from "react";
import { Link, NavLink, useParams } from "react-router-dom";

import { api, type Project, type ProjectKnowledgeScope, type WorkspaceTask } from "../lib/api";
import { formatDate } from "../lib/utils";
import { AgentPanel } from "../components/AgentPanel";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { KnowledgeExplorer } from "../components/KnowledgeExplorer";
import { StatusBadge } from "../components/StatusBadge";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../components/ui/dialog";

const SECTIONS = [
  ["overview", "Overview", FolderKanban],
  ["tasks", "Tasks", ListTodo],
  ["memory", "Memory", Scale],
  ["knowledge", "Knowledge", BookOpenCheck],
  ["artifacts", "Artifacts", FileText],
  ["agent", "Agent", Bot],
] as const;

function CreateTaskDialog({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [priority, setPriority] = useState("normal");
  const client = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createTask(projectId, { title, goal, priority }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["tasks", projectId] }); setOpen(false); setTitle(""); setGoal(""); },
  });
  return <Dialog open={open} onOpenChange={setOpen}><Button size="sm" onClick={() => setOpen(true)}><Plus size={15} /> New task</Button><DialogContent><DialogTitle>创建 WorkspaceTask</DialogTitle><DialogDescription>Task 是 AgentRun 的唯一工作边界。</DialogDescription><form className="mt-5 space-y-4" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}><label className="block text-sm font-medium">标题<input autoFocus className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2" minLength={2} required value={title} onChange={(event) => setTitle(event.target.value)} /></label><label className="block text-sm font-medium">目标<textarea className="mt-1.5 min-h-24 w-full rounded-lg border bg-white px-3 py-2" value={goal} onChange={(event) => setGoal(event.target.value)} /></label><label className="block text-sm font-medium">优先级<select className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2" value={priority} onChange={(event) => setPriority(event.target.value)}>{["low", "normal", "high", "urgent"].map((value) => <option key={value}>{value}</option>)}</select></label>{create.error instanceof Error ? <ErrorBlock error={create.error} /> : null}<div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setOpen(false)}>取消</Button><Button disabled={create.isPending} type="submit">创建</Button></div></form></DialogContent></Dialog>;
}

function ScopeManager({ project, scopes }: { project: Project; scopes: ProjectKnowledgeScope[] }) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const client = useQueryClient();
  const collections = useQuery({
    queryKey: ["knowledge-collections"],
    queryFn: api.collections,
    enabled: open,
  });
  const replace = useMutation({
    mutationFn: () => api.replaceScopes(project.id, project.revision, selected),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["project", project.id] });
      void client.invalidateQueries({ queryKey: ["scopes", project.id] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
      setOpen(false);
    },
  });
  const openDialog = () => {
    setSelected(scopes.map((scope) => scope.collection_slug));
    setOpen(true);
  };
  const toggle = (slug: string) => {
    setSelected((current) => current.includes(slug)
      ? current.filter((value) => value !== slug)
      : [...current, slug]);
  };
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button size="sm" variant="outline" onClick={openDialog}>Manage scopes</Button>
      <DialogContent>
        <DialogTitle>绑定 Knowledge Collections</DialogTitle>
        <DialogDescription>
          访问范围只由显式 Collection scope 决定。保存时使用 Project revision，冲突不会静默覆盖他人的变更。
        </DialogDescription>
        <form className="mt-5 space-y-3" onSubmit={(event) => { event.preventDefault(); replace.mutate(); }}>
          {collections.isPending ? <LoadingBlock label="读取 Collections…" /> : null}
          {collections.error instanceof Error ? <ErrorBlock error={collections.error} onRetry={() => collections.refetch()} /> : null}
          {collections.data?.length ? (
            <div className="max-h-80 space-y-2 overflow-y-auto rounded-lg border p-2">
              {collections.data.map((collection) => (
                <label className="flex cursor-pointer items-start gap-3 rounded-md p-2 hover:bg-slate-50" key={collection.slug}>
                  <input
                    checked={selected.includes(collection.slug)}
                    className="mt-1 accent-brand"
                    type="checkbox"
                    onChange={() => toggle(collection.slug)}
                  />
                  <span className="min-w-0"><span className="block text-sm font-medium">{collection.name}</span><span className="mt-0.5 block text-xs text-muted-ink">{collection.slug} · {collection.ingestion_count} ingestions</span></span>
                </label>
              ))}
            </div>
          ) : null}
          {collections.data && !collections.data.length ? <EmptyBlock title="没有可绑定的 Collection">先在 Knowledge Core 创建或导入一个 Collection。</EmptyBlock> : null}
          {replace.error instanceof Error ? <ErrorBlock error={replace.error} /> : null}
          <div className="flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => setOpen(false)}>取消</Button><Button disabled={replace.isPending || collections.isPending} type="submit">{replace.isPending ? "保存中…" : "保存 scopes"}</Button></div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function TaskRows({ projectId, tasks }: { projectId: string; tasks: WorkspaceTask[] }) {
  return tasks.length ? <div className="divide-y">{tasks.map((task) => <Link className="flex items-center justify-between gap-4 py-3 first:pt-0 last:pb-0 hover:text-brand" key={task.id} to={`/projects/${projectId}/tasks/${task.id}`}><div><p className="font-medium">{task.title}</p><p className="mt-1 line-clamp-1 text-xs text-muted-ink">{task.goal || "未设置目标"}</p></div><div className="flex items-center gap-3"><span className="hidden text-xs text-muted-ink sm:block">{task.priority}</span><StatusBadge status={task.status} /></div></Link>)}</div> : <EmptyBlock title="还没有 WorkspaceTask">创建任务后，才能生成 ContextSnapshot 并启动 Agent。</EmptyBlock>;
}

export function ProjectWorkspacePage() {
  const { projectId = "", section = "overview" } = useParams();
  const project = useQuery({ queryKey: ["project", projectId], queryFn: () => api.project(projectId) });
  const [tasks, decisions, artifacts, scopes, runs, proposals] = useQueries({
    queries: [
      { queryKey: ["tasks", projectId], queryFn: () => api.tasks(projectId, true) },
      { queryKey: ["decisions", projectId], queryFn: () => api.decisions(projectId) },
      { queryKey: ["artifacts", projectId], queryFn: () => api.artifacts(projectId) },
      { queryKey: ["scopes", projectId], queryFn: () => api.scopes(projectId) },
      { queryKey: ["project-runs", projectId], queryFn: () => api.projectRuns(projectId) },
      { queryKey: ["proposals", projectId], queryFn: () => api.proposals(projectId) },
    ],
  });
  if (project.isPending) return <div className="p-6"><LoadingBlock /></div>;
  if (project.error instanceof Error) return <div className="p-6"><ErrorBlock error={project.error} onRetry={() => project.refetch()} /></div>;
  if (!project.data) return <div className="p-6"><LoadingBlock label="等待 Project 数据…" /></div>;
  const currentTask = tasks.data?.find((task) => !["completed", "cancelled"].includes(task.status));
  const sectionError = [tasks, decisions, artifacts, scopes, runs, proposals].find((query) => query.error instanceof Error)?.error;
  return (
    <div className="grid min-h-screen grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]">
      <div className="min-w-0 p-6 lg:p-8">
        <header className="border-b pb-5"><div className="flex flex-wrap items-start justify-between gap-4"><div><p className="text-sm font-medium text-brand">{project.data.domain || "Project"}</p><h1 className="mt-1 text-2xl font-semibold tracking-tight">{project.data.name}</h1><p className="mt-2 max-w-3xl text-sm leading-6 text-muted-ink">{project.data.goal || "尚未设置 Project 目标。"}</p></div><div className="text-right"><StatusBadge status={project.data.status} /><p className="mt-2 text-xs text-muted-ink">revision {project.data.revision}</p></div></div><nav className="mt-6 flex gap-1 overflow-x-auto">{SECTIONS.map(([value, label, Icon]) => <NavLink key={value} className={({ isActive }) => `inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium ${isActive ? "bg-brand-soft text-brand" : "text-slate-600 hover:bg-slate-100"}`} to={`/projects/${projectId}/${value}`}><Icon size={16} />{label}</NavLink>)}</nav></header>
        {sectionError instanceof Error ? <div className="mt-6"><ErrorBlock error={sectionError} /></div> : null}
        <section className="mt-6">
          {section === "overview" ? <div className="grid gap-5 xl:grid-cols-2"><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Knowledge authority</p><h2 className="mt-1 text-lg font-semibold">Explicit Collection scopes</h2></div><ScopeManager project={project.data} scopes={scopes.data ?? []} /></CardHeader><CardContent>{scopes.data?.length ? <div className="flex flex-wrap gap-2">{scopes.data.map((scope) => <Badge key={scope.collection_slug} tone="brand">{scope.collection_slug}</Badge>)}</div> : <EmptyBlock title="未绑定 Collection">在此绑定 Collection；Context Builder 在没有 scope 时将安全地返回 no_scope。</EmptyBlock>}</CardContent></Card><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Memory</p><h2 className="mt-1 text-lg font-semibold">Accepted decisions</h2></div></CardHeader><CardContent className="space-y-3">{decisions.data?.filter((item) => item.status === "accepted").slice(0, 4).map((item) => <div key={item.id} className="rounded-lg border bg-white p-3"><p className="font-medium">{item.summary}</p><p className="mt-1 line-clamp-2 text-sm text-muted-ink">{item.impact || item.rationale}</p></div>)}{!decisions.data?.some((item) => item.status === "accepted") ? <EmptyBlock title="没有 accepted Decision">人工 Decision 将作为后续 Task 的项目记忆。</EmptyBlock> : null}</CardContent></Card><Card className="xl:col-span-2"><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Workspace</p><h2 className="mt-1 text-lg font-semibold">Open tasks</h2></div><CreateTaskDialog projectId={projectId} /></CardHeader><CardContent><TaskRows projectId={projectId} tasks={(tasks.data ?? []).filter((task) => !["completed", "cancelled"].includes(task.status))} /></CardContent></Card></div> : null}
          {section === "tasks" ? <Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">WorkspaceTask</p><h2 className="mt-1 text-lg font-semibold">All tasks</h2></div><CreateTaskDialog projectId={projectId} /></CardHeader><CardContent><TaskRows projectId={projectId} tasks={tasks.data ?? []} /></CardContent></Card> : null}
          {section === "memory" ? <div className="grid gap-5 xl:grid-cols-2"><Card><CardHeader><h2 className="text-lg font-semibold">Decisions</h2></CardHeader><CardContent className="space-y-3">{decisions.data?.map((item) => <div key={item.id} className="rounded-lg border bg-white p-4"><div className="flex justify-between gap-3"><p className="font-medium">{item.summary}</p><StatusBadge status={item.status} /></div><p className="mt-2 text-sm leading-6 text-muted-ink">{item.rationale || item.impact || "No rationale recorded."}</p></div>)}</CardContent></Card><Card><CardHeader><div><h2 className="text-lg font-semibold">MemoryProposals</h2><p className="mt-1 text-sm text-muted-ink">Agent 只能提出；人工审核与 commit 在 Review queue 完成。</p></div><Link className="text-sm font-medium text-brand hover:underline" to="/review">Open review</Link></CardHeader><CardContent className="space-y-3">{proposals.data?.map((item) => <Link key={item.id} className="block rounded-lg border bg-white p-4 hover:border-brand/40" to={`/review?proposal=${item.id}`}><div className="flex justify-between gap-3"><p className="font-medium">{item.proposal_type}</p><StatusBadge status={item.status} /></div><p className="mt-2 line-clamp-2 text-sm text-muted-ink">{item.rationale || "No rationale provided."}</p></Link>)}</CardContent></Card></div> : null}
          {section === "knowledge" ? <KnowledgeExplorer collectionSlugs={(scopes.data ?? []).map((scope) => scope.collection_slug)} /> : null}
          {section === "artifacts" ? <Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Artifacts</p><h2 className="mt-1 text-lg font-semibold">Versioned references</h2></div></CardHeader><CardContent className="space-y-3">{artifacts.data?.map((item) => <Link key={item.id} className="block rounded-lg border bg-white p-4 hover:border-brand/40" to={`/artifacts/${item.id}`}><div className="flex justify-between gap-3"><div><p className="font-medium">{item.type} · v{item.version}</p><p className="mt-1 truncate text-xs text-muted-ink">{item.reference}</p></div><StatusBadge status={item.status} /></div></Link>)}{!artifacts.data?.length ? <EmptyBlock title="尚无 Artifact">完成可验证的 Research AgentRun 后，系统将创建 ready Artifact reference。</EmptyBlock> : null}</CardContent></Card> : null}
          {section === "agent" ? <Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">AgentRun</p><h2 className="mt-1 text-lg font-semibold">Project audit history</h2></div></CardHeader><CardContent className="space-y-3">{runs.data?.items.map((run) => <Link key={run.id} className="flex items-center justify-between gap-4 rounded-lg border bg-white p-4 hover:border-brand/40" to={`/agent-runs/${run.id}`}><div><p className="font-medium">{run.workflow_name}</p><p className="mt-1 text-xs text-muted-ink">{run.current_node ?? "queued"} · {formatDate(run.updated_at)} · repair {run.repair_count}</p></div><StatusBadge status={run.status} /></Link>)}{!runs.data?.items.length ? <EmptyBlock title="没有 AgentRun">选择一个任务，并在右侧 Agent Panel 创建 Snapshot 后运行。</EmptyBlock> : null}</CardContent></Card> : null}
        </section>
      </div>
      <AgentPanel projectId={projectId} task={currentTask} />
    </div>
  );
}
