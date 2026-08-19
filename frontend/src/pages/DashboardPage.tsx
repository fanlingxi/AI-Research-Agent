import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Bot,
  FileText,
  FolderKanban,
  ListTodo,
  LoaderCircle,
  Play,
  Plus,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../lib/api";
import { formatDate } from "../lib/utils";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../components/ui/dialog";

const METRICS = [
  { key: "active_projects", label: "Active projects", icon: FolderKanban },
  { key: "recent_workspace_tasks", label: "Recent tasks", icon: ListTodo },
  { key: "recent_agent_runs", label: "Agent runs", icon: Bot },
  { key: "recent_artifacts", label: "Artifacts", icon: FileText },
] as const;

function CreateProjectDialog() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [domain, setDomain] = useState("");
  const create = useMutation({
    mutationFn: () => api.createProject({ name, goal, domain }),
    onSuccess: (project) => {
      void client.invalidateQueries({ queryKey: ["projects"] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
      setParams({});
      navigate(`/projects/${project.id}/overview`);
    },
  });
  const open = params.get("createProject") === "1";
  return (
    <Dialog open={open} onOpenChange={(next) => !next && setParams({})}>
      <DialogContent>
        <DialogTitle>创建 Project</DialogTitle>
        <DialogDescription>Project 定义范围；知识访问仍需要显式绑定 Collection。</DialogDescription>
        <form className="mt-5 space-y-4" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
          <label className="block text-sm font-medium">名称<input autoFocus className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2 outline-none focus:border-brand" minLength={2} required value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label className="block text-sm font-medium">目标<textarea className="mt-1.5 min-h-24 w-full rounded-lg border bg-white px-3 py-2 outline-none focus:border-brand" value={goal} onChange={(event) => setGoal(event.target.value)} /></label>
          <label className="block text-sm font-medium">领域<input className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2 outline-none focus:border-brand" value={domain} onChange={(event) => setDomain(event.target.value)} /></label>
          {create.error instanceof Error ? <ErrorBlock error={create.error} /> : null}
          <div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setParams({})}>取消</Button><Button disabled={create.isPending} type="submit">{create.isPending ? "创建中…" : "创建 Project"}</Button></div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function QuickResearchLauncher() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [instruction, setInstruction] = useState("");
  const [collectionSlug, setCollectionSlug] = useState("");
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections });
  const start = useMutation({
    mutationFn: () => api.submitAndExecuteReport({
      query: instruction.trim(),
      collection_slugs: collectionSlug ? [collectionSlug] : [],
      top_k: 12,
      report_depth: "standard",
    }),
    onSuccess: (report) => {
      void client.invalidateQueries({ queryKey: ["reports"] });
      navigate(`/reports?report=${encodeURIComponent(report.id)}`);
    },
  });
  return (
    <Card className="mt-8 overflow-hidden border-brand/20 bg-gradient-to-br from-white to-brand-soft/40">
      <CardContent>
        <form
          className="grid min-w-0 gap-5 lg:grid-cols-[minmax(0,1fr)_240px] lg:items-end"
          onSubmit={(event) => {
            event.preventDefault();
            if (
              instruction.trim().length >= 3
              && !collections.isPending
              && !collections.isError
            ) start.mutate();
          }}
        >
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-brand">
              <Sparkles size={18} />
              <p className="text-sm font-semibold">快速开始研究</p>
            </div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight">直接告诉工作台你想研究什么</h2>
            <p className="mt-2 text-sm leading-6 text-muted-ink">
              一次提交会创建并立即执行证据报告；范围、进度、失败重试和结果都留在主工作台。
            </p>
            <input
              aria-label="研究指令"
              className="mt-4 min-h-11 w-full rounded-lg border bg-white px-3 py-2 outline-none focus:border-brand"
              minLength={3}
              placeholder="例如：梳理 AI Agent 的发展路径、关键方法与当前局限"
              required
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
            />
          </div>
          <div>
            <label className="text-sm font-medium">
              证据范围
              <select
                className="mt-1.5 min-h-11 w-full rounded-lg border bg-white px-3 py-2"
                disabled={collections.isPending || collections.isError}
                value={collectionSlug}
                onChange={(event) => setCollectionSlug(event.target.value)}
              >
                <option value="">
                  {collections.isPending ? "正在读取集合…" : "全部已发布知识"}
                </option>
                {collections.data?.filter((item) => !item.is_system).map((collection) => (
                  <option key={collection.slug} value={collection.slug}>{collection.name}</option>
                ))}
              </select>
              <span className="mt-2 block text-xs font-normal leading-5 text-muted-ink">
                默认生成标准报告，最多引用 12 条证据；可在报告页调整高级参数。
              </span>
            </label>
            <Button
              className="mt-3 w-full"
              disabled={start.isPending || collections.isPending || collections.isError}
              size="lg"
              type="submit"
            >
              {start.isPending ? <LoaderCircle className="animate-spin" size={17} /> : <Play size={17} />}
              {start.isPending ? "正在启动…" : "开始研究"}
            </Button>
          </div>
          {collections.error instanceof Error ? (
            <div className="lg:col-span-2">
              <ErrorBlock error={collections.error} onRetry={() => collections.refetch()} />
            </div>
          ) : null}
          {start.error instanceof Error ? (
            <div className="lg:col-span-2"><ErrorBlock error={start.error} /></div>
          ) : null}
        </form>
      </CardContent>
    </Card>
  );
}

export function DashboardPage() {
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: api.dashboard });
  if (dashboard.isPending) return <div className="p-6 lg:p-8"><LoadingBlock /></div>;
  if (dashboard.error instanceof Error) return <div className="p-6 lg:p-8"><ErrorBlock error={dashboard.error} onRetry={() => dashboard.refetch()} /></div>;
  const data = dashboard.data!;
  return (
    <div className="mx-auto max-w-7xl p-6 lg:p-8">
      <CreateProjectDialog />
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div><p className="text-sm font-medium text-brand">Personal Knowledge Agent Platform</p><h1 className="mt-1 text-3xl font-semibold tracking-tight">Project workspace</h1><p className="mt-2 text-sm text-muted-ink">把研究从任务、可验证上下文到产物和人工审核连接起来。</p></div>
        <Link to="/?createProject=1"><Button><Plus size={16} /> New project</Button></Link>
      </header>

      <QuickResearchLauncher />

      <section className="mt-8 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {METRICS.map(({ key, label, icon: Icon }) => <Card key={key}><CardContent className="flex items-center gap-4"><span className="grid size-10 place-items-center rounded-lg bg-brand-soft text-brand"><Icon size={20} /></span><div><p className="text-2xl font-semibold">{data[key].length}</p><p className="text-sm text-muted-ink">{label}</p></div></CardContent></Card>)}
      </section>

      <section className="mt-8 grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <Card>
          <CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Projects</p><h2 className="mt-1 text-lg font-semibold">Active work</h2></div><ShieldCheck className="text-brand" size={20} /></CardHeader>
          <CardContent className="space-y-3">
            {data.active_projects.length ? data.active_projects.map((project) => <Link key={project.id} className="group block rounded-lg border bg-white p-4 hover:border-brand/40" to={`/projects/${project.id}/overview`}><div className="flex items-start justify-between gap-3"><div><h3 className="font-medium group-hover:text-brand">{project.name}</h3><p className="mt-1 line-clamp-2 text-sm leading-6 text-muted-ink">{project.goal || "尚未设置 Project 目标。"}</p></div><StatusBadge status={project.status} /></div><p className="mt-3 text-xs text-muted-ink">{project.domain || "general"} · updated {formatDate(project.updated_at)}</p></Link>) : <EmptyBlock title="还没有活动项目">创建一个 Project，然后显式绑定 Knowledge Collection。</EmptyBlock>}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Review</p><h2 className="mt-1 text-lg font-semibold">Pending MemoryProposals</h2></div><Link className="text-sm font-medium text-brand hover:underline" to="/review">Open queue</Link></CardHeader>
          <CardContent className="space-y-3">
            {data.pending_memory_proposals.length ? data.pending_memory_proposals.map((proposal) => <Link className="block rounded-lg border bg-white p-3 hover:border-brand/40" key={proposal.id} to={`/review?proposal=${proposal.id}`}><div className="flex justify-between gap-3"><p className="font-medium">{proposal.proposal_type}</p><StatusBadge status={proposal.status} /></div><p className="mt-1 line-clamp-2 text-sm text-muted-ink">{proposal.rationale || "No rationale provided."}</p></Link>) : <EmptyBlock title="没有待审 Proposal">Agent 只能创建 proposed 记录；人工审核后才能 commit。</EmptyBlock>}
          </CardContent>
        </Card>
      </section>

      <section className="mt-8 grid gap-6 xl:grid-cols-2">
        <Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Tasks</p><h2 className="mt-1 text-lg font-semibold">Recent WorkspaceTasks</h2></div></CardHeader><CardContent className="divide-y">{data.recent_workspace_tasks.map((task) => <Link className="flex items-center justify-between gap-4 py-3 first:pt-0 last:pb-0 hover:text-brand" key={task.id} to={`/projects/${task.project_id}/tasks/${task.id}`}><div><p className="font-medium">{task.title}</p><p className="mt-1 text-xs text-muted-ink">{task.priority} · revision {task.revision}</p></div><StatusBadge status={task.status} /></Link>)}</CardContent></Card>
        <Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Runtime</p><h2 className="mt-1 text-lg font-semibold">Recent AgentRuns</h2></div></CardHeader><CardContent className="divide-y">{data.recent_agent_runs.map((run) => <Link className="flex items-center justify-between gap-4 py-3 first:pt-0 last:pb-0 hover:text-brand" key={run.id} to={`/agent-runs/${run.id}`}><div><p className="font-medium">{run.workflow_name}</p><p className="mt-1 text-xs text-muted-ink">{run.current_node ?? "queued"} · {formatDate(run.updated_at)}</p></div><StatusBadge status={run.status} /></Link>)}{!data.recent_agent_runs.length ? <EmptyBlock title="尚未运行 Agent">从 Task Detail 创建 ContextSnapshot 后启动研究运行。</EmptyBlock> : null}</CardContent></Card>
      </section>
      <Link className="mt-8 inline-flex items-center gap-2 text-sm font-medium text-brand hover:underline" to="/review">Open review workflow <ArrowRight size={16} /></Link>
    </div>
  );
}
