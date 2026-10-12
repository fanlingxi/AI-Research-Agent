import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, FolderKanban, LoaderCircle, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, type ResearchCommandInput } from "../lib/api";
import { COMMAND_STAGE_LABELS } from "../lib/labels";
import { cn } from "../lib/utils";
import { ErrorBlock, Notice } from "./AsyncState";
import { Button } from "./ui/button";
import { Field, Input, Select, Textarea } from "./ui/field";
import { Disclosure } from "./ui/page";

const PENDING_COMMAND_KEY = "research-workspace.pending-command";

const MODES = {
  quick_report: {
    label: "快速报告",
    icon: Zap,
    description: "直接基于已发布的知识检索证据并生成一份带引用的报告，适合快速了解一个问题。",
  },
  project_run: {
    label: "项目研究",
    icon: FolderKanban,
    description: "在项目中建立任务、冻结上下文快照后运行研究智能体。结果可追溯，可登记反馈并生成修订版。",
  },
} as const;

export function ResearchCommandLauncher({ className }: { className?: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"quick_report" | "project_run">("quick_report");
  const [instruction, setInstruction] = useState("");
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
    void queryClient.invalidateQueries({ queryKey: ["reports"] });
    navigate(current.target_route);
  }, [command.data, navigate, queryClient, start.data]);

  const visibleCollections = useMemo(
    () => (collections.data ?? []).filter((collection) => !collection.is_system),
    [collections.data],
  );

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
  const loadError = (collections.error ?? projects.error ?? tasks.error) as Error | null;

  return (
    <section className={cn("rounded-2xl border bg-panel shadow-[var(--shadow-card)]", className)} aria-label="提问研究">
      <form
        className="p-4 sm:p-6"
        onSubmit={(event) => {
          event.preventDefault();
          if (!canSubmit) return;
          const payload = buildPayload();
          start.mutate({ payload, key: idempotencyKeyFor(payload) });
        }}
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-[17px] font-semibold text-ink">你想研究什么？</h2>
          <div className="inline-flex rounded-lg bg-subtle p-0.5 ring-1 ring-inset ring-line" role="group" aria-label="研究方式">
            {(Object.keys(MODES) as Array<keyof typeof MODES>).map((key) => {
              const Icon = MODES[key].icon;
              return (
                <button
                  aria-pressed={mode === key}
                  className={cn(
                    "inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-[13px] font-medium text-muted-ink transition-colors hover:text-ink",
                    mode === key && "bg-panel text-ink shadow-[var(--shadow-card)]",
                  )}
                  key={key}
                  onClick={() => setMode(key)}
                  type="button"
                >
                  <Icon size={14} />{MODES[key].label}
                </button>
              );
            })}
          </div>
        </div>

        <Textarea
          aria-label="研究指令"
          className="mt-4 min-h-28 resize-y border-line-strong px-4 py-3 text-[15px] leading-7"
          minLength={3}
          placeholder="例如：比较查询改写和知识图谱查询扩展两条路线，各自解决什么问题、改变了检索流程的哪个环节？"
          required
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) event.currentTarget.form?.requestSubmit();
          }}
        />

        <div className="mt-4">
          {mode === "quick_report" ? (
            <fieldset className="min-w-0">
              <legend className="text-[13px] font-medium text-ink-soft">资料范围</legend>
              <div className="mt-2 flex flex-wrap gap-2">
                {visibleCollections.map((collection) => {
                  const checked = selectedCollections.includes(collection.slug);
                  return (
                    <label
                      className={cn(
                        "inline-flex h-8 max-w-full cursor-pointer items-center gap-1.5 rounded-full border px-3 text-[13px] transition-colors focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-brand",
                        checked ? "border-brand bg-brand-soft text-brand-strong" : "border-line-strong bg-panel text-ink-soft hover:border-brand/40",
                      )}
                      key={collection.slug}
                    >
                      <input
                        checked={checked}
                        className="sr-only"
                        type="checkbox"
                        onChange={(event) => setSelectedCollections((current) => event.target.checked ? [...current, collection.slug] : current.filter((slug) => slug !== collection.slug))}
                      />
                      {checked ? <Check size={13} /> : null}
                      <span className="truncate">{collection.name}</span>
                    </label>
                  );
                })}
                {collections.isPending ? <span className="text-[13px] text-muted-ink">读取知识集合…</span> : null}
                {collections.data && !visibleCollections.length ? <span className="text-[13px] text-muted-ink">还没有知识集合，<Link className="text-brand hover:underline" to="/knowledge">先导入资料</Link>。</span> : null}
              </div>
              <p className="mt-2 text-xs text-muted-ink">{selectedCollections.length ? `只使用选中的 ${selectedCollections.length} 个集合中已审核发布的知识。` : "未选择时使用全部已审核发布的知识。"}</p>
              <Disclosure className="mt-3" summary="报告设置">
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="报告深度"><Select value={reportDepth} onChange={(event) => setReportDepth(event.target.value as typeof reportDepth)}><option value="brief">简报</option><option value="standard">标准</option><option value="deep">深度综述</option></Select></Field>
                  <Field label="最多引用证据片段" hint="限制报告引用的片段数，不限制检索的文献数。"><Input max={30} min={1} type="number" value={topK} onChange={(event) => setTopK(Number(event.target.value))} /></Field>
                </div>
              </Disclosure>
            </fieldset>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="项目" hint={projects.data && !projects.data.length ? <>还没有项目，<Link className="text-brand hover:underline" to="?createProject=1">新建一个</Link>。</> : "研究会在该项目的知识范围内进行。"}>
                <Select aria-label="项目" value={projectId} onChange={(event) => { setProjectId(event.target.value); setTaskId(""); }}>
                  <option value="">选择项目</option>
                  {projects.data?.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
                </Select>
              </Field>
              <div className="min-w-0">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[13px] font-medium text-ink-soft">任务</span>
                  <div className="inline-flex rounded-md bg-subtle p-0.5 text-xs" role="group" aria-label="任务来源">
                    <button aria-pressed={taskMode === "new"} className={cn("rounded px-2 py-0.5", taskMode === "new" ? "bg-panel font-medium text-ink shadow-[var(--shadow-card)]" : "text-muted-ink")} onClick={() => setTaskMode("new")} type="button">新建任务</button>
                    <button aria-pressed={taskMode === "existing"} className={cn("rounded px-2 py-0.5", taskMode === "existing" ? "bg-panel font-medium text-ink shadow-[var(--shadow-card)]" : "text-muted-ink")} onClick={() => setTaskMode("existing")} type="button">已有任务</button>
                  </div>
                </div>
                <div className="mt-1.5">
                  {taskMode === "new"
                    ? <Input aria-label="新任务标题" placeholder="任务标题（留空则取问题前 60 字）" value={taskTitle} onChange={(event) => setTaskTitle(event.target.value)} />
                    : <Select aria-label="已有任务" disabled={!projectId || tasks.isPending} value={taskId} onChange={(event) => setTaskId(event.target.value)}><option value="">{projectId ? "选择任务" : "请先选择项目"}</option>{tasks.data?.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}</Select>}
                </div>
              </div>
              <Disclosure className="sm:col-span-2" summary="运行设置">
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="flex items-start gap-2 text-[13px] text-ink-soft"><input checked={createProposal} className="mt-1 accent-brand" type="checkbox" onChange={(event) => setCreateProposal(event.target.checked)} /><span>允许智能体提出项目记忆<span className="block text-xs text-muted-ink">只会生成待审核的提案，需要你批准后才会写入。</span></span></label>
                  <Field label="上下文 Token 预算"><Input max={16000} min={256} type="number" value={tokenBudget} onChange={(event) => setTokenBudget(Number(event.target.value))} /></Field>
                </div>
              </Disclosure>
            </div>
          )}
        </div>

        {loadError ? <div className="mt-4"><ErrorBlock error={loadError} /></div> : null}
        {start.error instanceof Error ? <div className="mt-4"><ErrorBlock error={start.error} /></div> : null}
        {retry.error instanceof Error ? <div className="mt-4"><ErrorBlock error={retry.error} /></div> : null}
        {currentCommand && !currentCommand.target_route ? (
          <Notice
            className="mt-4"
            tone={currentCommand.status === "failed" ? "danger" : "info"}
            icon={currentCommand.status === "failed" ? undefined : <LoaderCircle className="animate-spin" size={15} />}
            title={currentCommand.status === "failed" ? "研究未能启动" : COMMAND_STAGE_LABELS[currentCommand.orchestration_stage] ?? "正在准备研究…"}
            action={currentCommand.status === "failed" ? (
              <div className="flex gap-2">
                <Button size="sm" variant="ghost" onClick={() => { window.localStorage.removeItem(PENDING_COMMAND_KEY); setActiveCommandId(null); start.reset(); }}>忽略</Button>
                <Button size="sm" variant="outline" onClick={() => retry.mutate(currentCommand.id)}>从失败阶段恢复</Button>
              </div>
            ) :<Link className="text-[13px] font-medium text-brand hover:underline" to="/runtime">查看运行记录</Link>}
          >
            {currentCommand.error ?? "指令已保存；刷新页面或重复提交不会创建重复的研究。"}
          </Notice>
        ) : null}

        <div className="mt-5 flex flex-col-reverse gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[13px] leading-5 text-muted-ink">{MODES[mode].description}</p>
          <Button className="w-full sm:w-auto" disabled={!canSubmit || start.isPending} size="lg" type="submit">
            {start.isPending ? <LoaderCircle className="animate-spin" size={17} /> : <ArrowRight size={17} />}
            {start.isPending ? "正在提交…" : "提交并开始研究"}
          </Button>
        </div>
      </form>
    </section>
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
