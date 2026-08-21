import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Database,
  FileClock,
  RefreshCw,
  ServerCog,
  ShieldAlert,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { api, type RuntimeWorkItem } from "../lib/api";
import { appConfig } from "../lib/config";
import { formatDate } from "../lib/utils";

const SERVICE_LABELS: Record<string, string> = {
  api: "API 编排服务",
  worker: "统一任务执行器",
  llm: "语言模型",
  neo4j: "图关系数据库",
  qdrant: "向量检索服务",
  vault: "Vault 知识投影",
};

const KIND_LABELS: Record<string, string> = {
  ingestion: "PDF 入库",
  report: "研究报告",
  agent_run: "项目 AgentRun",
  research_command: "研究指令",
  collection_sync: "Collection 同步",
  projection: "外部投影",
};

const STAGE_LABELS: Record<string, string> = {
  queued: "等待执行",
  queued_for_dispatch: "已进入执行队列",
  retrieval: "检索证据",
  drafting: "生成初稿",
  evaluation: "引用与质量校验",
  revision: "受控修订",
  collection_sync: "同步 Collection 投影",
  entity_projection: "投影知识实体",
  relation_projection: "投影图关系",
};

function queueTotal(workCounts: Record<string, Record<string, number>>) {
  return Object.values(workCounts).reduce(
    (total, counts) => total + (counts.queued ?? 0),
    0,
  );
}

export function RuntimePage() {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [cursor, setCursor] = useState<string | undefined>();
  const [cursorHistory, setCursorHistory] = useState<Array<string | undefined>>([]);
  const overview = useQuery({
    queryKey: ["runtime-overview"],
    queryFn: api.runtimeOverview,
    refetchInterval: 5_000,
  });
  const work = useQuery({
    queryKey: ["runtime-work", kind, status, cursor],
    queryFn: () => api.runtimeWork({
      kind: kind || undefined,
      status: status || undefined,
      cursor,
      limit: 30,
    }),
    refetchInterval: (query) => query.state.data?.items.some((item) =>
      item.job_status === "queued" || item.job_status === "running") ? 2_500 : false,
  });
  const refreshWork = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["runtime-overview"] }),
      queryClient.invalidateQueries({ queryKey: ["runtime-work"] }),
      queryClient.invalidateQueries({ queryKey: ["knowledge-ingestions"] }),
      queryClient.invalidateQueries({ queryKey: ["reports"] }),
    ]);
  };
  const retry = useMutation({
    mutationFn: async (item: RuntimeWorkItem) => {
      if (item.kind === "projection") return api.retryProjection(item.id);
      if (item.kind === "ingestion") return api.retryIngestion(item.resource_id);
      if (item.kind === "report") return api.retryReport(item.resource_id);
      if (item.kind === "agent_run") return api.resumeRun(item.resource_id);
      if (item.kind === "research_command") return api.retryResearchCommand(item.resource_id);
      throw new Error("该任务必须通过资源详情页恢复。");
    },
    onSuccess: refreshWork,
  });
  const cancel = useMutation({
    mutationFn: (item: RuntimeWorkItem) => api.cancelRun(item.resource_id),
    onSuccess: refreshWork,
  });

  if (overview.isPending || work.isPending) {
    return <div className="p-6 lg:p-8"><LoadingBlock label="读取运行记录…" /></div>;
  }
  if (overview.error instanceof Error || work.error instanceof Error) {
    return (
      <div className="p-6 lg:p-8">
        <ErrorBlock
          error={(overview.error ?? work.error) as Error}
          onRetry={() => { void overview.refetch(); void work.refetch(); }}
        />
      </div>
    );
  }
  if (!overview.data || !work.data) {
    return <div className="p-6 lg:p-8"><LoadingBlock label="读取运行记录…" /></div>;
  }
  const overviewData = overview.data;
  const workData = work.data;
  const services = Object.entries(overviewData.services);
  const queued = queueTotal(overviewData.work_counts);
  const workerOnline = overviewData.services.worker?.available === true;
  const llmReady = overviewData.services.llm?.available === true;
  const resetPage = () => {
    setCursor(undefined);
    setCursorHistory([]);
  };
  const goNext = () => {
    if (!workData.next_cursor) return;
    setCursorHistory((current) => [...current, cursor]);
    setCursor(workData.next_cursor);
  };
  const goBack = () => {
    const previous = cursorHistory.at(-1);
    setCursorHistory((current) => current.slice(0, -1));
    setCursor(previous);
  };

  return (
    <div className="mx-auto max-w-7xl p-6 lg:p-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b pb-6">
        <div>
          <p className="text-sm font-medium text-brand">运行可观测性</p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight">运行与恢复</h1>
          <p className="mt-2 text-sm text-muted-ink">查看任务为何排队、当前阶段、执行器所有权与投影失败。</p>
        </div>
        <Button variant="outline" onClick={() => { void overview.refetch(); void work.refetch(); }}>
          <RefreshCw size={15} />刷新
        </Button>
      </header>

      {!workerOnline && queued > 0 ? (
        <div className="mt-5 flex gap-3 rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950" role="alert">
          <AlertTriangle className="shrink-0" size={19} />
          <div>
            <p className="font-medium">{queued} 个任务在排队，但 Worker 离线</p>
            <p className="mt-1">请运行 <code className="rounded bg-white px-1.5 py-0.5">python -m app.worker</code>，或使用 <code className="rounded bg-white px-1.5 py-0.5">make up</code> 启动完整栈。</p>
          </div>
        </div>
      ) : null}
      {!llmReady ? (
        <div className="mt-3 rounded-xl border border-slate-300 bg-white px-4 py-3 text-sm text-slate-700">真实 LLM 未配置：PDF 抽取和研究报告会在产生数据副作用前失败；其他确定性功能仍可使用。</div>
      ) : null}

      <section className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {services.map(([name, service]) => (
          <Card key={name}>
            <CardContent>
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-medium">{SERVICE_LABELS[name] ?? name}</p>
                <span className={`size-2 rounded-full ${service.available ? "bg-emerald-500" : "bg-red-500"}`} />
              </div>
              <p className="mt-2 text-xs leading-5 text-muted-ink">{service.detail}</p>
            </CardContent>
          </Card>
        ))}
      </section>

      <section className="mt-6 grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card className="min-w-0">
          <CardHeader>
            <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">统一任务流</p><h2 className="mt-1 text-lg font-semibold">任务时间线</h2></div>
            <FileClock className="text-brand" size={20} />
          </CardHeader>
          <CardContent>
            <div className="mb-4 flex flex-wrap gap-3">
              <label className="text-xs font-medium text-muted-ink">类型
                <select className="mt-1 block min-w-40 rounded-lg border bg-white px-3 py-2 text-sm text-ink" value={kind} onChange={(event) => { setKind(event.target.value); resetPage(); }}>
                  <option value="">全部类型</option>
                  {Object.entries(KIND_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
              <label className="text-xs font-medium text-muted-ink">状态
                <select className="mt-1 block min-w-36 rounded-lg border bg-white px-3 py-2 text-sm text-ink" value={status} onChange={(event) => { setStatus(event.target.value); resetPage(); }}>
                  <option value="">全部状态</option>
                  {["queued", "running", "failed", "completed", "needs_review", "stale_context"].map((value) => <option key={value} value={value}>{value}</option>)}
                </select>
              </label>
            </div>
            <div className="max-h-[42rem] min-w-0 space-y-3 overflow-y-auto overscroll-contain pr-2" data-testid="runtime-work-list">
              {workData.items.map((item) => (
                <article className="min-w-0 rounded-lg border bg-white p-4" key={item.id}>
                  <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <Link className="break-words font-medium hover:text-brand hover:underline" to={item.detail_route}>{item.title}</Link>
                      <p className="mt-1 text-xs text-muted-ink">{KIND_LABELS[item.kind] ?? item.kind} · {item.current_stage ? (STAGE_LABELS[item.current_stage] ?? item.current_stage) : "未记录阶段"}</p>
                    </div>
                    <div className="flex gap-2"><StatusBadge status={item.business_status} /><StatusBadge status={item.job_status} /></div>
                  </div>
                  <div className="mt-3 grid gap-2 text-xs text-muted-ink sm:grid-cols-2 xl:grid-cols-4">
                    <span>尝试 {item.attempt}</span>
                    <span>{item.queue_position ? `队列第 ${item.queue_position} 位` : "不在等待队列"}</span>
                    <span className="truncate" title={item.executor ?? ""}>执行器 {item.executor ?? "未领取"}</span>
                    <span>更新 {formatDate(item.updated_at)}</span>
                  </div>
                  {item.lease_until ? <p className="mt-2 text-xs text-muted-ink">租约到期 {formatDate(item.lease_until)}</p> : null}
                  {item.last_error ? <p className="mt-3 max-h-28 overflow-auto break-words rounded bg-danger-soft p-2 text-xs leading-5 text-red-900">{item.last_error}</p> : null}
                  {(item.can_retry || item.can_cancel) ? (
                    <div className="mt-3 flex gap-2">
                      {item.can_retry ? <Button disabled={retry.isPending} size="sm" variant="outline" onClick={() => retry.mutate(item)}><RefreshCw size={13} />安全重试</Button> : null}
                      {item.can_cancel ? <Button disabled={cancel.isPending} size="sm" variant="danger" onClick={() => cancel.mutate(item)}>取消任务</Button> : null}
                    </div>
                  ) : null}
                </article>
              ))}
              {!workData.items.length ? <EmptyBlock title="没有匹配的任务">调整类型或状态筛选。</EmptyBlock> : null}
            </div>
            {(cursorHistory.length > 0 || workData.next_cursor) ? (
              <div className="mt-4 flex items-center justify-between border-t pt-4">
                <Button disabled={!cursorHistory.length} size="sm" variant="outline" onClick={goBack}><ChevronLeft size={14} />上一页</Button>
                <span className="text-xs text-muted-ink">第 {cursorHistory.length + 1} 页</span>
                <Button disabled={!workData.next_cursor} size="sm" variant="outline" onClick={goNext}>下一页<ChevronRight size={14} /></Button>
              </div>
            ) : null}
            {retry.error instanceof Error ? <div className="mt-4"><ErrorBlock error={retry.error} /></div> : null}
            {cancel.error instanceof Error ? <div className="mt-4"><ErrorBlock error={cancel.error} /></div> : null}
          </CardContent>
        </Card>

        <aside className="min-w-0 space-y-5">
          <Card>
            <CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">执行器</p><h2 className="mt-1 font-semibold">Worker 心跳</h2></div><ServerCog className="text-brand" size={20} /></CardHeader>
            <CardContent className="space-y-3">
              {overviewData.executors.map((executor) => (
                <div className="rounded-lg border bg-white p-3" key={executor.id}>
                  <div className="flex justify-between gap-2"><p className="truncate text-sm font-medium" title={executor.id}>{executor.id}</p><StatusBadge status={executor.online ? "active" : "failed"} /></div>
                  <p className="mt-2 text-xs text-muted-ink">{executor.version} · 最后心跳 {formatDate(executor.last_heartbeat_at)}</p>
                  <p className="mt-1 truncate text-xs text-muted-ink">当前任务：{executor.current_job_id ?? "空闲"}</p>
                </div>
              ))}
              {!overviewData.executors.length ? <p className="text-sm text-muted-ink">尚未收到 Worker 心跳。</p> : null}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">队列</p><h2 className="mt-1 font-semibold">各类工作</h2></div><Database className="text-brand" size={20} /></CardHeader>
            <CardContent className="space-y-3">
              {Object.entries(overviewData.work_counts).flatMap(([workKind, counts]) =>
                Object.entries(counts).map(([workStatus, count]) => (
                  <div className="flex items-center justify-between rounded-lg border bg-white px-3 py-2" key={`${workKind}-${workStatus}`}>
                    <div><p className="text-xs font-medium">{KIND_LABELS[workKind] ?? workKind}</p><StatusBadge status={workStatus} /></div><span className="font-semibold">{count}</span>
                  </div>
                )),
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">投影 backlog</p><h2 className="mt-1 font-semibold">图关系与 Vault</h2></div><Activity className="text-brand" size={20} /></CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-2 text-center text-sm">
                {(["queued", "running", "failed", "completed"] as const).map((value) => <div className="rounded-lg border bg-white p-2" key={value}><p className="text-lg font-semibold">{overviewData.projection_backlog[value]}</p><p className="text-xs text-muted-ink">{value}</p></div>)}
              </div>
              {overviewData.projection_backlog.failed > 0 ? <p className="mt-3 text-xs leading-5 text-red-800">失败投影会导致 SQLite 有正式事实，但 Neo4j/Vault 暂未同步。在左侧筛选“外部投影 + failed”可定点重试。</p> : null}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">高级诊断</p><h2 className="mt-1 font-semibold">需要底层检查？</h2></div><ShieldAlert className="text-brand" size={20} /></CardHeader>
            <CardContent><p className="text-sm leading-6 text-muted-ink">日常状态、恢复和投影重试已在主工作台中完成；Streamlit 仅保留原始状态和重建诊断。</p><a className="mt-4 inline-flex text-sm font-medium text-brand hover:underline" href={appConfig.operationsUrl} rel="noreferrer" target="_blank">打开运维控制台</a></CardContent>
          </Card>
        </aside>
      </section>
    </div>
  );
}
