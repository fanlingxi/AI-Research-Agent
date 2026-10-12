import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, ArrowRight, FileText, Library, ListTodo, Plus } from "lucide-react";
import { useState } from "react";
import { Link, NavLink, useParams } from "react-router-dom";

import { api, type Project, type ProjectKnowledgeScope, type WorkspaceTask } from "../lib/api";
import { nodeLabel, PRIORITY_LABELS } from "../lib/labels";
import { cn, formatDate } from "../lib/utils";
import { AgentPanel } from "../components/AgentPanel";
import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "../components/AsyncState";
import { KnowledgeExplorer } from "../components/KnowledgeExplorer";
import { StatusBadge } from "../components/StatusBadge";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../components/ui/dialog";
import { Field, Input, Select, Textarea } from "../components/ui/field";

const SECTIONS = [
  ["overview", "概览"],
  ["tasks", "任务"],
  ["artifacts", "报告"],
  ["knowledge", "知识范围"],
  ["memory", "项目记忆"],
  ["agent", "运行记录"],
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
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}><Plus size={14} />新建任务</Button>
      <DialogContent>
        <DialogTitle>新建任务</DialogTitle>
        <DialogDescription>任务是一次研究的具体问题。每次研究运行都针对一个任务，并冻结当时的上下文。</DialogDescription>
        <form className="mt-5 space-y-4" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
          <Field label="任务标题"><Input autoFocus minLength={2} required value={title} onChange={(event) => setTitle(event.target.value)} /></Field>
          <Field label="任务目标" hint="写清楚要回答的问题、期望的输出形式和限制条件。"><Textarea className="min-h-28" value={goal} onChange={(event) => setGoal(event.target.value)} /></Field>
          <Field label="优先级"><Select value={priority} onChange={(event) => setPriority(event.target.value)}>{["low", "normal", "high", "urgent"].map((value) => <option key={value} value={value}>{PRIORITY_LABELS[value]}</option>)}</Select></Field>
          {create.error instanceof Error ? <ErrorBlock error={create.error} /> : null}
          <div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setOpen(false)}>取消</Button><Button disabled={create.isPending} type="submit">创建任务</Button></div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ScopeManager({ project, scopes }: { project: Project; scopes: ProjectKnowledgeScope[] }) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const client = useQueryClient();
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections, enabled: open });
  const availableCollections = collections.data?.filter((collection) => !collection.is_system) ?? [];
  const replace = useMutation({
    mutationFn: () => api.replaceScopes(project.id, project.revision, selected.filter((slug) => availableCollections.some((collection) => collection.slug === slug))),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["project", project.id] });
      void client.invalidateQueries({ queryKey: ["scopes", project.id] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
      setOpen(false);
    },
  });
  const openDialog = () => { setSelected(scopes.map((scope) => scope.collection_slug)); setOpen(true); };
  const toggle = (slug: string) => setSelected((current) => current.includes(slug) ? current.filter((value) => value !== slug) : [...current, slug]);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button size="sm" variant="outline" onClick={openDialog}>管理知识范围</Button>
      <DialogContent>
        <DialogTitle>选择项目可用的知识集合</DialogTitle>
        <DialogDescription>项目研究只会使用这里勾选的集合中已审核发布的知识。收件箱中的资料不能用于研究。</DialogDescription>
        <form className="mt-5 space-y-3" onSubmit={(event) => { event.preventDefault(); replace.mutate(); }}>
          {collections.isPending ? <LoadingBlock label="读取知识集合…" /> : null}
          {collections.error instanceof Error ? <ErrorBlock error={collections.error} onRetry={() => collections.refetch()} /> : null}
          {availableCollections.length ? (
            <div className="max-h-80 space-y-1 overflow-y-auto rounded-lg border p-1.5">
              {availableCollections.map((collection) => (
                <label className="flex cursor-pointer items-start gap-3 rounded-md p-2 hover:bg-subtle" key={collection.slug}>
                  <input checked={selected.includes(collection.slug)} className="mt-1 accent-[var(--color-brand)]" type="checkbox" onChange={() => toggle(collection.slug)} />
                  <span className="min-w-0"><span className="block text-sm font-medium">{collection.name}</span><span className="mt-0.5 block text-xs text-muted-ink">{collection.ingestion_count} 批导入</span></span>
                </label>
              ))}
            </div>
          ) : null}
          {collections.data && !availableCollections.length ? <EmptyBlock title="没有可选的知识集合">先在知识库导入资料并创建集合。</EmptyBlock> : null}
          {replace.error instanceof Error ? <ErrorBlock error={replace.error} /> : null}
          <div className="flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => setOpen(false)}>取消</Button><Button disabled={replace.isPending || collections.isPending} type="submit">{replace.isPending ? "保存中…" : "保存范围"}</Button></div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function TaskRows({ projectId, tasks }: { projectId: string; tasks: WorkspaceTask[] }) {
  if (!tasks.length) return <EmptyBlock className="py-8" icon={<ListTodo size={18} />} title="还没有任务">新建一个任务，写下要研究的问题，然后在右侧发起研究。</EmptyBlock>;
  return (
    <div className="divide-y">
      {tasks.map((task) => (
        <Link className="flex min-w-0 items-center justify-between gap-4 px-1 py-3 first:pt-1 last:pb-1 hover:text-brand" key={task.id} to={`/projects/${projectId}/tasks/${task.id}`}>
          <span className="min-w-0"><span className="block truncate font-medium">{task.title}</span><span className="mt-0.5 line-clamp-1 text-xs text-muted-ink">{task.goal || "未填写目标"}</span></span>
          <span className="flex shrink-0 items-center gap-2">{task.priority !== "normal" ? <span className="hidden text-xs text-muted-ink sm:inline">{PRIORITY_LABELS[task.priority] ?? task.priority}优先级</span> : null}<StatusBadge status={task.status} /></span>
        </Link>
      ))}
    </div>
  );
}

export function ProjectWorkspacePage() {
  const { projectId = "", section = "overview" } = useParams();
  const [selectedTaskId, setSelectedTaskId] = useState("");
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
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections, enabled: section === "knowledge" || section === "overview" });
  if (project.isPending) return <div className="p-6"><LoadingBlock label="读取项目…" /></div>;
  if (project.error instanceof Error) return <div className="p-6"><ErrorBlock error={project.error} onRetry={() => project.refetch()} /></div>;
  if (!project.data) return <div className="p-6"><LoadingBlock label="读取项目…" /></div>;
  const openTasks = (tasks.data ?? []).filter((task) => !["completed", "cancelled"].includes(task.status));
  const currentTask = openTasks.find((task) => task.id === selectedTaskId);
  const sectionError = [tasks, decisions, artifacts, scopes, runs, proposals].find((query) => query.error instanceof Error)?.error;
  const taskTitles = new Map((tasks.data ?? []).map((task) => [task.id, task.title]));
  const reports = (artifacts.data ?? []).filter((item) => item.type === "research_report").sort((a, b) => b.created_at.localeCompare(a.created_at));
  const scopeNames = (scopes.data ?? []).map((scope) => ({ slug: scope.collection_slug, name: collections.data?.find((item) => item.slug === scope.collection_slug)?.name ?? scope.collection_slug }));
  const pendingProposals = (proposals.data ?? []).filter((item) => ["proposed", "approved"].includes(item.status));

  const reportList = (limit?: number) => reports.length ? (
    <div className="divide-y">
      {reports.slice(0, limit).map((item) => (
        <Link className="flex min-w-0 items-center gap-3 px-1 py-3 first:pt-1 last:pb-1 hover:text-brand" key={item.id} to={`/artifacts/${item.id}`}>
          <FileText className="shrink-0 text-muted-ink" size={16} />
          <span className="min-w-0 flex-1"><span className="block truncate font-medium">{(item.task_id && taskTitles.get(item.task_id)) || "研究报告"}</span><span className="text-xs text-muted-ink">{formatDate(item.created_at)}</span></span>
          {item.status !== "ready" ? <StatusBadge status={item.status} /> : null}
        </Link>
      ))}
    </div>
  ) : <EmptyBlock className="py-8" icon={<FileText size={18} />} title="还没有报告">研究运行完成并通过引用校验后，报告会出现在这里。</EmptyBlock>;

  const scopeCard = (
    <Card>
      <CardHeader><CardTitle icon={<Library size={16} />} title="知识范围" description="研究只会使用这些集合中已审核的知识。" /><ScopeManager project={project.data} scopes={scopes.data ?? []} /></CardHeader>
      <CardContent>
        {scopes.data?.length ? <div className="flex flex-wrap gap-2">{scopeNames.map((scope) => <Badge key={scope.slug} tone="brand">{scope.name}</Badge>)}</div> : (
          <Notice tone="warning" icon={<AlertTriangle size={15} />} title="还没有选择知识集合">没有知识范围时，研究运行拿不到任何证据。点击「管理知识范围」选择集合。</Notice>
        )}
      </CardContent>
    </Card>
  );

  return (
    <div className="grid min-h-screen min-w-0 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px]">
      <div className="min-w-0 px-4 pb-16 pt-6 sm:px-6 lg:px-10 lg:pt-9">
        <Link className="inline-flex items-center gap-1.5 text-[13px] text-muted-ink hover:text-ink" to="/projects"><ArrowLeft size={15} />全部项目</Link>
        <header className="mt-4">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 max-w-3xl">
              <h1 className="break-words text-[22px] font-semibold leading-8 tracking-tight sm:text-2xl">{project.data.name}</h1>
              <p className="mt-1.5 text-sm leading-6 text-muted-ink">{project.data.goal || "尚未填写研究目标。"}</p>
            </div>
            <StatusBadge status={project.data.status} />
          </div>
          <nav aria-label="项目栏目" className="-mx-1 mt-6 flex gap-1 overflow-x-auto border-b px-1">
            {SECTIONS.map(([value, label]) => (
              <NavLink
                className={({ isActive }) => cn("-mb-px shrink-0 border-b-2 px-3 pb-2.5 pt-1 text-sm font-medium", isActive ? "border-brand text-brand-strong" : "border-transparent text-muted-ink hover:text-ink")}
                key={value}
                to={`/projects/${projectId}/${value}`}
              >{label}</NavLink>
            ))}
          </nav>
        </header>
        {sectionError instanceof Error ? <div className="mt-6"><ErrorBlock error={sectionError} /></div> : null}
        <section className="mt-6 min-w-0">
          {section === "overview" ? (
            <div className="grid min-w-0 gap-5 xl:grid-cols-2">
              <div className="xl:col-span-2">{scopeCard}</div>
              <Card>
                <CardHeader><CardTitle icon={<ListTodo size={16} />} title="进行中的任务" /><CreateTaskDialog projectId={projectId} /></CardHeader>
                <CardContent className="pt-3"><TaskRows projectId={projectId} tasks={openTasks} /></CardContent>
              </Card>
              <Card>
                <CardHeader><CardTitle icon={<FileText size={16} />} title="最近的报告" />{reports.length > 3 ? <Link className="text-[13px] font-medium text-brand hover:underline" to={`/projects/${projectId}/artifacts`}>全部</Link> : null}</CardHeader>
                <CardContent className="pt-3">{reportList(3)}</CardContent>
              </Card>
              {pendingProposals.length ? (
                <div className="xl:col-span-2">
                  <Notice tone="warning" title={`${pendingProposals.length} 条项目记忆提案等待审核`} action={<Link className="inline-flex items-center gap-1 text-[13px] font-medium underline" to="/review?tab=memory">去审核<ArrowRight size={13} /></Link>}>批准并写入后，才会作为后续研究的项目记忆。</Notice>
                </div>
              ) : null}
            </div>
          ) : null}
          {section === "tasks" ? (
            <Card>
              <CardHeader><CardTitle title="全部任务" description="包括已完成和已取消的任务。" /><CreateTaskDialog projectId={projectId} /></CardHeader>
              <CardContent className="pt-3"><TaskRows projectId={projectId} tasks={tasks.data ?? []} /></CardContent>
            </Card>
          ) : null}
          {section === "artifacts" ? (
            <Card>
              <CardHeader><CardTitle title="项目报告" description="每份报告来自一次研究运行，内容不可修改；修订会生成新的报告。" /></CardHeader>
              <CardContent className="pt-3">{reportList()}</CardContent>
            </Card>
          ) : null}
          {section === "knowledge" ? (
            <div className="space-y-5">
              {scopeCard}
              <KnowledgeExplorer collections={scopeNames} description="只在本项目的知识范围内检索，结果与项目研究使用的资料一致。" />
            </div>
          ) : null}
          {section === "memory" ? (
            <div className="grid gap-5 xl:grid-cols-2">
              <Card>
                <CardHeader><CardTitle title="项目决策" description="已确认的决策会作为后续研究的背景。" /></CardHeader>
                <CardContent className="space-y-3">
                  {decisions.data?.map((item) => (
                    <div key={item.id} className="rounded-lg border p-3.5">
                      <div className="flex justify-between gap-3"><p className="font-medium">{item.summary}</p><StatusBadge status={item.status} /></div>
                      <p className="mt-1.5 text-[13px] leading-6 text-muted-ink">{item.rationale || item.impact || "未记录理由。"}</p>
                    </div>
                  ))}
                  {!decisions.data?.length ? <EmptyBlock className="py-7" title="还没有项目决策">批准并写入的记忆提案会出现在这里。</EmptyBlock> : null}
                </CardContent>
              </Card>
              <Card>
                <CardHeader><CardTitle title="记忆提案" description="研究智能体只能提出建议，需要你在审核页确认。" /><Link className="text-[13px] font-medium text-brand hover:underline" to="/review?tab=memory">去审核</Link></CardHeader>
                <CardContent className="space-y-3">
                  {proposals.data?.map((item) => (
                    <Link key={item.id} className="block rounded-lg border p-3.5 hover:border-brand/40" to={`/review?tab=memory&proposal=${item.id}`}>
                      <div className="flex justify-between gap-3"><p className="font-medium">{typeof item.payload.summary === "string" ? item.payload.summary : "项目决策提案"}</p><StatusBadge status={item.status} /></div>
                      <p className="mt-1.5 line-clamp-2 text-[13px] text-muted-ink">{item.rationale || "未提供理由。"}</p>
                    </Link>
                  ))}
                  {!proposals.data?.length ? <EmptyBlock className="py-7" title="没有记忆提案" /> : null}
                </CardContent>
              </Card>
            </div>
          ) : null}
          {section === "agent" ? (
            <Card>
              <CardHeader><CardTitle title="研究运行记录" description="每次运行都基于一个冻结的上下文快照，可查看完整过程。" /></CardHeader>
              <CardContent className="pt-3">
                <div className="divide-y">
                  {runs.data?.items.map((run) => (
                    <Link key={run.id} className="flex min-w-0 items-center justify-between gap-4 px-1 py-3 first:pt-1 last:pb-1 hover:text-brand" to={`/agent-runs/${run.id}`}>
                      <span className="min-w-0"><span className="block truncate font-medium">{taskTitles.get(run.task_id) ?? "研究运行"}</span><span className="mt-0.5 block text-xs text-muted-ink">{nodeLabel(run.current_node)} · {formatDate(run.updated_at)}</span></span>
                      <StatusBadge status={run.status} />
                    </Link>
                  ))}
                </div>
                {!runs.data?.items.length ? <EmptyBlock className="py-8" title="还没有研究运行">选择一个任务，在右侧冻结快照后开始研究。</EmptyBlock> : null}
              </CardContent>
            </Card>
          ) : null}
        </section>
      </div>
      <AgentPanel availableTasks={openTasks} projectId={projectId} task={currentTask} onTaskChange={setSelectedTaskId} />
    </div>
  );
}
