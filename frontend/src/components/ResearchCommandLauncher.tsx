import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, FileText, LoaderCircle, Play, Search, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, type ResearchCommandInput } from "../lib/api";
import { ErrorBlock } from "./AsyncState";
import { StatusBadge } from "./StatusBadge";
import { Button } from "./ui/button";
import { Card, CardContent } from "./ui/card";

const PENDING_COMMAND_KEY = "research-workspace.pending-command";

export function ResearchCommandLauncher() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"quick_report" | "project_run">("quick_report");
  const [instruction, setInstruction] = useState("");
  const [collectionSearch, setCollectionSearch] = useState("");
  const [selectedCollections, setSelectedCollections] = useState<string[]>([]);
  const [reportDepth, setReportDepth] = useState<"brief" | "standard" | "deep">("standard");
  const [topK, setTopK] = useState(12);
  const [projectId, setProjectId] = useState("");
  const [taskMode, setTaskMode] = useState<"new" | "existing">("new");
  const [taskId, setTaskId] = useState("");
  const [taskTitle, setTaskTitle] = useState("");
  const [createProposal, setCreateProposal] = useState(false);
  const [tokenBudget, setTokenBudget] = useState(6000);
  const [activeCommandId, setActiveCommandId] = useState<string | null>(
    () => pendingCommandState()?.commandId ?? null,
  );
  const collections = useQuery({
    queryKey: ["knowledge-collections"],
    queryFn: api.collections,
    enabled: mode === "quick_report",
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: api.projects,
    enabled: mode === "project_run",
  });
  const tasks = useQuery({
    queryKey: ["project-tasks", projectId],
    queryFn: () => api.tasks(projectId),
    enabled: mode === "project_run" && Boolean(projectId),
  });
  const command = useQuery({
    queryKey: ["research-command", activeCommandId],
    queryFn: () => api.researchCommand(activeCommandId!),
    enabled: Boolean(activeCommandId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && ["completed", "failed"].includes(status) ? false : 1_000;
    },
  });
  const start = useMutation({
    mutationFn: ({ payload, key }: { payload: ResearchCommandInput; key: string }) =>
      api.submitResearchCommand(payload, key),
    onSuccess: (created) => {
      setActiveCommandId(created.id);
      rememberCommandId(created.id);
      void queryClient.invalidateQueries({ queryKey: ["runtime-work"] });
    },
  });
  const retry = useMutation({
    mutationFn: (commandId: string) => api.retryResearchCommand(commandId),
    onSuccess: (updated) => {
      queryClient.setQueryData(["research-command", updated.id], updated);
      void queryClient.invalidateQueries({ queryKey: ["runtime-work"] });
    },
  });

  useEffect(() => {
    const current = command.data ?? start.data;
    if (!current?.target_route) return;
    window.localStorage.removeItem(PENDING_COMMAND_KEY);
    navigate(current.target_route);
  }, [command.data, navigate, start.data]);

  const visibleCollections = useMemo(() => {
    const search = collectionSearch.trim().toLocaleLowerCase();
    return (collections.data ?? []).filter((collection) =>
      !collection.is_system
      && (!search || collection.name.toLocaleLowerCase().includes(search)
        || collection.slug.toLocaleLowerCase().includes(search)),
    );
  }, [collectionSearch, collections.data]);

  const buildPayload = (): ResearchCommandInput => {
    if (mode === "quick_report") {
      return {
        mode,
        instruction: instruction.trim(),
        collection_slugs: selectedCollections,
        report_depth: reportDepth,
        top_k: topK,
      };
    }
    return {
      mode,
      instruction: instruction.trim(),
      project_id: projectId,
      task: taskMode === "existing"
        ? { kind: "existing", task_id: taskId }
        : { kind: "new", title: taskTitle.trim() || undefined, priority: "normal" },
      create_memory_proposal: createProposal,
      max_steps: 10,
      max_tool_calls: 3,
      token_budget: tokenBudget,
    };
  };
  const canSubmit = instruction.trim().length >= 3 && (mode === "quick_report"
    ? !collections.isPending && !collections.isError
    : !projects.isPending && !projects.isError && Boolean(projectId)
      && (taskMode === "new" || (!tasks.isPending && !tasks.isError && Boolean(taskId))));
  const currentCommand = command.data ?? start.data;

  return (
    <Card className="mt-8 overflow-hidden border-brand/20 bg-gradient-to-br from-white to-brand-soft/40">
      <CardContent>
        <form
          className="space-y-5"
          onSubmit={(event) => {
            event.preventDefault();
            if (!canSubmit) return;
            const payload = buildPayload();
            start.mutate({ payload, key: idempotencyKeyFor(payload) });
          }}
        >
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-brand"><Sparkles size={18} /><p className="text-sm font-semibold">统一研究指令</p></div>
              <h2 className="mt-2 text-2xl font-semibold tracking-tight">你想研究什么？</h2>
              <p className="mt-2 text-sm leading-6 text-muted-ink">明确选择“快速报告”或“项目研究”，工作台会幂等创建并启动对应任务。</p>
            </div>
            <div className="flex rounded-lg border bg-white p-1" role="group" aria-label="研究模式">
              <Button aria-pressed={mode === "quick_report"} size="sm" variant={mode === "quick_report" ? "secondary" : "ghost"} onClick={() => setMode("quick_report")}><FileText size={14} />快速报告</Button>
              <Button aria-pressed={mode === "project_run"} size="sm" variant={mode === "project_run" ? "secondary" : "ghost"} onClick={() => setMode("project_run")}><Bot size={14} />项目研究</Button>
            </div>
          </div>

          <label className="block text-sm font-medium">研究指令
            <textarea aria-label="研究指令" className="mt-1.5 min-h-24 w-full resize-y rounded-lg border bg-white px-3 py-2 outline-none focus:border-brand" minLength={3} placeholder="例如：梳理 AI Agent 的发展路径、关键方法与当前局限" required value={instruction} onChange={(event) => setInstruction(event.target.value)} />
          </label>

          {mode === "quick_report" ? (
            <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_260px]">
              <div>
                <label className="relative block text-sm font-medium">搜索 Collection
                  <Search className="absolute bottom-2.5 left-3 text-slate-400" size={15} />
                  <input className="mt-1.5 min-h-10 w-full rounded-lg border bg-white py-2 pl-9 pr-3" placeholder="按名称或 slug 筛选" value={collectionSearch} onChange={(event) => setCollectionSearch(event.target.value)} />
                </label>
                <div className="mt-2 max-h-36 space-y-1 overflow-y-auto rounded-lg border bg-white p-2">
                  {visibleCollections.map((collection) => (
                    <label className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-slate-50" key={collection.slug}>
                      <input checked={selectedCollections.includes(collection.slug)} type="checkbox" onChange={(event) => setSelectedCollections((current) => event.target.checked ? [...current, collection.slug] : current.filter((slug) => slug !== collection.slug))} />
                      <span>{collection.name}</span><span className="ml-auto text-xs text-muted-ink">{collection.ingestion_count} 次入库</span>
                    </label>
                  ))}
                  {!visibleCollections.length ? <p className="px-2 py-3 text-sm text-muted-ink">没有匹配的正式 Collection。</p> : null}
                </div>
                <p className="mt-2 text-xs text-muted-ink">未勾选时使用全部已发布知识。产物是一份带证据包的独立报告。</p>
              </div>
              <details className="rounded-lg border bg-white p-3"><summary className="cursor-pointer text-sm font-medium">报告高级设置</summary><div className="mt-3 space-y-3"><label className="block text-xs font-medium">深度<select className="mt-1 w-full rounded border px-2 py-2 text-sm" value={reportDepth} onChange={(event) => setReportDepth(event.target.value as typeof reportDepth)}><option value="brief">简报</option><option value="standard">标准</option><option value="deep">深入</option></select></label><label className="block text-xs font-medium">最多引用证据片段<input className="mt-1 w-full rounded border px-2 py-2 text-sm" max={30} min={1} type="number" value={topK} onChange={(event) => setTopK(Number(event.target.value))} /></label></div></details>
            </div>
          ) : (
            <div className="grid gap-5 lg:grid-cols-2">
              <label className="text-sm font-medium">Project
                <select className="mt-1.5 min-h-11 w-full rounded-lg border bg-white px-3 py-2" value={projectId} onChange={(event) => { setProjectId(event.target.value); setTaskId(""); }}><option value="">请显式选择 Project</option>{projects.data?.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>
                <span className="mt-2 block text-xs font-normal text-muted-ink">项目研究会创建不可变 ContextSnapshot、AgentRun 和可审计产物，不会静默新建 Project。</span>
              </label>
              <div>
                <div className="flex gap-2"><Button size="sm" variant={taskMode === "new" ? "secondary" : "outline"} onClick={() => setTaskMode("new")}>创建新 Task</Button><Button size="sm" variant={taskMode === "existing" ? "secondary" : "outline"} onClick={() => setTaskMode("existing")}>使用已有 Task</Button></div>
                {taskMode === "new" ? <label className="mt-3 block text-sm font-medium">新 Task 标题<input className="mt-1.5 min-h-10 w-full rounded-lg border bg-white px-3 py-2" placeholder="留空则取指令前 60 字" value={taskTitle} onChange={(event) => setTaskTitle(event.target.value)} /></label> : <label className="mt-3 block text-sm font-medium">已有 Task<select className="mt-1.5 min-h-10 w-full rounded-lg border bg-white px-3 py-2" disabled={!projectId || tasks.isPending} value={taskId} onChange={(event) => setTaskId(event.target.value)}><option value="">请选择 Task</option>{tasks.data?.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}</select></label>}
              </div>
              <details className="rounded-lg border bg-white p-3 lg:col-span-2"><summary className="cursor-pointer text-sm font-medium">Agent 高级设置</summary><div className="mt-3 grid gap-3 sm:grid-cols-2"><label className="text-sm"><input checked={createProposal} className="mr-2" type="checkbox" onChange={(event) => setCreateProposal(event.target.checked)} />允许生成 MemoryProposal</label><label className="text-xs font-medium">Context token 预算<input className="mt-1 w-full rounded border px-2 py-2 text-sm" max={16000} min={256} type="number" value={tokenBudget} onChange={(event) => setTokenBudget(Number(event.target.value))} /></label></div></details>
            </div>
          )}

          {(collections.error ?? projects.error ?? tasks.error) instanceof Error ? <ErrorBlock error={(collections.error ?? projects.error ?? tasks.error) as Error} /> : null}
          {start.error instanceof Error ? <ErrorBlock error={start.error} /> : null}
          {retry.error instanceof Error ? <ErrorBlock error={retry.error} /> : null}
          {currentCommand && !currentCommand.target_route ? (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-white p-3 text-sm">
              <div><div className="flex items-center gap-2"><StatusBadge status={currentCommand.status} /><span>{currentCommand.orchestration_stage}</span></div><p className="mt-1 text-xs text-muted-ink">指令已持久化；刷新页面或网络重试不会创建重复资源。</p>{currentCommand.error ? <p className="mt-2 text-xs text-red-800">{currentCommand.error}</p> : null}</div>
              <div className="flex gap-2">{currentCommand.status === "failed" ? <Button size="sm" variant="outline" onClick={() => retry.mutate(currentCommand.id)}>从失败阶段恢复</Button> : null}<Link className="text-sm font-medium text-brand hover:underline" to="/runtime">查看运行台</Link></div>
            </div>
          ) : null}
          <div className="flex justify-end">
            <Button disabled={!canSubmit || start.isPending} size="lg" type="submit">{start.isPending ? <LoaderCircle className="animate-spin" size={17} /> : <Play size={17} />}{start.isPending ? "正在接受指令…" : "提交并开始研究"}</Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function idempotencyKeyFor(payload: ResearchCommandInput) {
  const fingerprint = JSON.stringify(payload);
  const stored = pendingCommandState();
  if (stored?.fingerprint === fingerprint && stored.key) return stored.key;
  const key = globalThis.crypto?.randomUUID?.()
    ?? `research-command-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  window.localStorage.setItem(PENDING_COMMAND_KEY, JSON.stringify({ fingerprint, key }));
  return key;
}

function rememberCommandId(commandId: string) {
  const stored = pendingCommandState();
  if (!stored) return;
  window.localStorage.setItem(PENDING_COMMAND_KEY, JSON.stringify({ ...stored, commandId }));
}

function pendingCommandState() {
  try {
    return JSON.parse(window.localStorage.getItem(PENDING_COMMAND_KEY) ?? "null") as {
      fingerprint?: string;
      key?: string;
      commandId?: string;
    } | null;
  } catch {
    window.localStorage.removeItem(PENDING_COMMAND_KEY);
    return null;
  }
}
