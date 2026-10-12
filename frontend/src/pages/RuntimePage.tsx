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
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "../components/AsyncState";
import { ProjectionMaintenance } from "../components/ProjectionMaintenance";
import { StatusBadge } from "../components/StatusBadge";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Select } from "../components/ui/field";
import { Page, PageHeader, Stat } from "../components/ui/page";
import { api, type RuntimeWorkItem } from "../lib/api";
import { statusLabel } from "../lib/labels";
import { cn, formatDate } from "../lib/utils";

const SERVICE_LABELS: Record<string, string> = {
  api: "API 编排服务",
  worker: "统一任务执行器",
  llm: "语言模型",
  neo4j: "图关系数据库",
  qdrant: "向量检索服务",
  vault: "知识文档投影（Vault）",
};

const KIND_LABELS: Record<string, string> = {
  ingestion: "PDF 入库",
  report: "研究报告",
  agent_run: "项目研究运行",
  research_command: "研究指令",
  collection_sync: "资料集合同步",
  projection: "外部投影",
  projection_rebuild: "投影重建",
};

const STAGE_LABELS: Record<string, string> = {
  queued: "等待执行",
  queued_for_dispatch: "已进入执行队列",
  retrieval: "检索证据",
  drafting: "生成初稿",
  evaluation: "引用与质量校验",
  revision: "受控修订",
  collection_sync: "同步资料集合投影",
  projection_rebuild: "重建派生投影",
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
      if (item.kind === "projection_rebuild") return api.retryRebuild(item.id);
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
    return <Page width="wide"><LoadingBlock label="读取运行记录…" /></Page>;
  }
  if (overview.error instanceof Error || work.error instanceof Error) {
    return (
      <Page width="wide">
        <ErrorBlock
          error={(overview.error ?? work.error) as Error}
          onRetry={() => { void overview.refetch(); void work.refetch(); }}
          title="无法读取运行记录"
        />
      </Page>
    );
  }
  if (!overview.data || !work.data) {
    return <Page width="wide"><LoadingBlock label="读取运行记录…" /></Page>;
  }
  const overviewData = overview.data;
  const workData = work.data;
  const services = Object.entries(overviewData.services);
  const workKinds = Object.entries(overviewData.work_counts).filter(([, counts]) => Object.keys(counts).length > 0);
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
    <Page width="wide">
      <PageHeader
        title="运行记录"
        description="后台任务的排队、执行与失败情况，以及各项服务的状态。日常使用通常不需要来这里；任务卡住或失败时可在此排查和重试。"
        actions={<Button variant="outline" onClick={() => { void overview.refetch(); void work.refetch(); }}><RefreshCw size={15} />刷新</Button>}
      />

      {(!workerOnline && queued > 0) || !llmReady ? (
        <div className="mb-5 space-y-2">
          {!workerOnline && queued > 0 ? (
            <Notice tone="warning" icon={<AlertTriangle size={15} />} title={`${queued} 个任务在排队，但 Worker 离线`}>
              请运行 <code className="rounded bg-panel px-1.5 py-0.5 font-mono text-xs">python -m app.worker</code>，或使用 <code className="rounded bg-panel px-1.5 py-0.5 font-mono text-xs">make up</code> 启动完整栈。
            </Notice>
          ) : null}
          {!llmReady ? <Notice>未配置真实语言模型：PDF 抽取和研究报告会在产生数据副作用前失败；其他确定性功能仍可使用。</Notice> : null}
        </div>
      ) : null}

      <Card>
        <CardHeader><CardTitle icon={<ServerCog size={16} />} title="服务状态" description={`更新于 ${formatDate(overviewData.generated_at)}`} /></CardHeader>
        <CardContent className="grid gap-x-6 gap-y-3 pt-3 sm:grid-cols-2 xl:grid-cols-3">
          {services.map(([name, service]) => (
            <div className="flex min-w-0 items-start gap-2.5" key={name}>
              <span aria-hidden="true" className={cn("mt-1.5 size-2 shrink-0 rounded-full", service.available ? "bg-emerald-500" : "bg-red-500")} />
              <div className="min-w-0">
                <p className="text-[13.5px] font-medium text-ink">{SERVICE_LABELS[name] ?? name}<span className="sr-only">：{service.available ? "可用" : "不可用"}</span></p>
                <p className="break-words text-xs leading-5 text-muted-ink">{service.detail}</p>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <section className="mt-5 grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card className="flex min-w-0 flex-col">
          <CardHeader><CardTitle icon={<FileClock size={16} />} title="任务时间线" description="入库、报告、研究运行与投影任务，按最近更新排列。" /></CardHeader>
          <CardContent className="flex min-h-0 flex-1 flex-col pt-3 xl:basis-0">
            <div className="mb-3 flex shrink-0 flex-wrap gap-2">
              <Select aria-label="任务类型" className="w-auto min-w-36" value={kind} onChange={(event) => { setKind(event.target.value); resetPage(); }}>
                <option value="">全部类型</option>
                {Object.entries(KIND_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </Select>
              <Select aria-label="任务状态" className="w-auto min-w-32" value={status} onChange={(event) => { setStatus(event.target.value); resetPage(); }}>
                <option value="">全部状态</option>
                {["queued", "running", "failed", "completed", "needs_review", "stale_context"].map((value) => <option key={value} value={value}>{statusLabel(value)}</option>)}
              </Select>
            </div>
            <div className="max-h-[42rem] min-w-0 divide-y divide-line overflow-y-auto overscroll-contain rounded-lg border xl:h-0 xl:min-h-64 xl:max-h-none xl:flex-1 xl:basis-0" data-testid="runtime-work-list">
              {workData.items.map((item) => (
                <article className="min-w-0 px-3.5 py-3" key={item.id}>
                  <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-3 gap-y-1.5">
                    <div className="min-w-0 flex-1">
                      <Link className="break-words text-[13.5px] font-medium text-ink hover:text-brand hover:underline" to={item.detail_route}>{item.title}</Link>
                      <p className="mt-0.5 text-xs text-muted-ink">{KIND_LABELS[item.kind] ?? item.kind} · {item.current_stage ? (STAGE_LABELS[item.current_stage] ?? item.current_stage) : "未记录阶段"}</p>
                    </div>
                    <div className="flex shrink-0 flex-wrap gap-1.5"><StatusBadge status={item.business_status} />{item.job_status !== item.business_status ? <StatusBadge status={item.job_status} /> : null}</div>
                  </div>
                  <p className="mt-1.5 flex min-w-0 flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-ink">
                    <span>尝试 {item.attempt}</span>
                    {item.queue_position ? <span>队列第 {item.queue_position} 位</span> : null}
                    <span className="min-w-0 max-w-full truncate" title={item.executor ?? ""}>执行器 {item.executor ?? "未领取"}</span>
                    <span>更新 {formatDate(item.updated_at)}</span>
                    {item.lease_until ? <span>租约到期 {formatDate(item.lease_until)}</span> : null}
                  </p>
                  {item.last_error ? <p className="mt-2 max-h-28 overflow-auto whitespace-pre-wrap break-words rounded-md bg-danger-soft px-2.5 py-2 text-xs leading-5 text-red-900">{item.last_error}</p> : null}
                  {(item.can_retry || item.can_cancel) ? (
                    <div className="mt-2 flex gap-2">
                      {item.can_retry ? <Button disabled={retry.isPending} size="sm" variant="outline" onClick={() => retry.mutate(item)}><RefreshCw size={13} />安全重试</Button> : null}
                      {item.can_cancel ? <Button disabled={cancel.isPending} size="sm" variant="danger-outline" onClick={() => cancel.mutate(item)}>取消任务</Button> : null}
                    </div>
                  ) : null}
                </article>
              ))}
              {!workData.items.length ? <EmptyBlock className="m-3 border-0 py-8" title="没有匹配的任务">{kind || status ? "调整类型或状态筛选。" : "后台任务出现后会显示在这里。"}</EmptyBlock> : null}
            </div>
            {(cursorHistory.length > 0 || workData.next_cursor) ? (
              <div className="mt-3 flex shrink-0 items-center justify-between">
                <Button disabled={!cursorHistory.length} size="sm" variant="outline" onClick={goBack}><ChevronLeft size={14} />上一页</Button>
                <span className="text-xs tabular-nums text-muted-ink">第 {cursorHistory.length + 1} 页</span>
                <Button disabled={!workData.next_cursor} size="sm" variant="outline" onClick={goNext}>下一页<ChevronRight size={14} /></Button>
              </div>
            ) : null}
            {retry.error instanceof Error ? <div className="mt-3"><ErrorBlock error={retry.error} /></div> : null}
            {cancel.error instanceof Error ? <div className="mt-3"><ErrorBlock error={cancel.error} /></div> : null}
          </CardContent>
        </Card>

        <aside className="min-w-0 space-y-5">
          <Card>
            <CardHeader><CardTitle icon={<ServerCog size={16} />} title="执行器心跳" /></CardHeader>
            <CardContent className="space-y-2.5 pt-3">
              {overviewData.executors.map((executor) => (
                <div className="min-w-0 rounded-lg border px-3 py-2.5" key={executor.id}>
                  <div className="flex min-w-0 items-center justify-between gap-2"><p className="truncate font-mono text-xs text-ink" title={executor.id}>{executor.id}</p><StatusBadge status={executor.online ? "online" : "offline"} /></div>
                  <p className="mt-1 text-xs text-muted-ink">{executor.version} · 最后心跳 {formatDate(executor.last_heartbeat_at)}</p>
                  <p className="mt-0.5 truncate text-xs text-muted-ink" title={executor.current_job_id ?? ""}>当前任务：{executor.current_job_id ?? "空闲"}</p>
                </div>
              ))}
              {!overviewData.executors.length ? <p className="text-[13px] text-muted-ink">尚未收到执行器心跳。</p> : null}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle icon={<Database size={16} />} title="各类工作" /></CardHeader>
            <CardContent className="pt-3">
              {workKinds.length ? (
                <table className="w-full table-fixed text-[13px]">
                  <thead className="sr-only"><tr><th>类型</th><th>各状态数量</th></tr></thead>
                  <tbody className="divide-y divide-line">
                    {workKinds.map(([workKind, counts]) => (
                      <tr key={workKind}>
                        <th className="w-[40%] py-2 pr-2 text-left align-top font-medium text-ink-soft" scope="row">{KIND_LABELS[workKind] ?? workKind}</th>
                        <td className="py-2">
                          <div className="flex flex-wrap justify-end gap-1">
                            {Object.entries(counts).map(([workStatus, count]) => (
                              <Badge key={workStatus} tone={workStatus === "failed" ? "danger" : workStatus === "queued" || workStatus === "running" ? "info" : "neutral"}>{statusLabel(workStatus)} <span className="tabular-nums">{count}</span></Badge>
                            ))}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <p className="text-[13px] text-muted-ink">还没有后台工作记录。</p>}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle icon={<Activity size={16} />} title="投影积压" description="向量索引、图谱与文档投影的同步队列。" /></CardHeader>
            <CardContent className="pt-3">
              <div className="grid grid-cols-4 gap-2">
                {(["queued", "running", "failed", "completed"] as const).map((value) => (
                  <Stat key={value} label={statusLabel(value)} tone={value === "failed" && overviewData.projection_backlog.failed > 0 ? "warning" : "default"} value={overviewData.projection_backlog[value]} />
                ))}
              </div>
              {overviewData.projection_backlog.failed > 0 ? <p className="mt-3 text-xs leading-5 text-amber-800">投影失败时，正式知识已保存，只是图谱或文档投影暂未同步。在时间线筛选“外部投影 + 失败”可逐条重试。</p> : null}
            </CardContent>
          </Card>
          <ProjectionMaintenance />
        </aside>
      </section>
    </Page>
  );
}
