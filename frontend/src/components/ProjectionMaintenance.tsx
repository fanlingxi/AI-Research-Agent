import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Wrench } from "lucide-react";
import { useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, type ProjectionRebuild, type ProjectionTarget } from "../lib/api";
import { ErrorBlock } from "./AsyncState";
import { JsonDetails } from "./JsonDetails";
import { StatusBadge } from "./StatusBadge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Field, Select } from "./ui/field";

const TARGET_LABELS: Record<string, string> = { qdrant: "向量索引", neo4j: "图谱", obsidian: "Obsidian 文档", all: "全部投影" };

export function ProjectionMaintenance() {
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const jobId = params.get("rebuild");
  const [expanded, setExpanded] = useState(false);
  const [raw, setRaw] = useState(false);
  const [target, setTarget] = useState<ProjectionTarget>("qdrant");
  const [collection, setCollection] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const submission = useRef<{ signature: string; key: string } | null>(null);
  const collections = useQuery({ queryKey: ["collections"], queryFn: api.collections, enabled: expanded });
  const health = useQuery({ queryKey: ["raw-health"], queryFn: api.knowledgeHealth, enabled: raw });
  const overview = useQuery({ queryKey: ["runtime-overview"], queryFn: api.runtimeOverview, enabled: raw });
  const job = useQuery({
    queryKey: ["projection-rebuild", jobId], queryFn: () => api.rebuild(jobId!), enabled: !!jobId,
    refetchInterval: (query) => !query.state.data || ["queued", "running"].includes(query.state.data.status) ? 2000 : false,
  });
  const refresh = (value: ProjectionRebuild) => {
    client.setQueryData(["projection-rebuild", value.id], value);
    setParams((current) => { const next = new URLSearchParams(current); next.set("rebuild", value.id); return next; });
    void client.invalidateQueries({ queryKey: ["runtime-work"] });
    void client.invalidateQueries({ queryKey: ["runtime-overview"] });
  };
  const submit = useMutation({
    mutationFn: () => {
      const input = { target, collection_slug: collection || null, confirmed: true as const };
      const signature = JSON.stringify(input);
      if (submission.current?.signature !== signature) submission.current = { signature, key: crypto.randomUUID() };
      return api.submitRebuild(input, submission.current.key);
    },
    onSuccess: (value) => { refresh(value); submission.current = null; setConfirmed(false); },
  });
  const retry = useMutation({ mutationFn: () => api.retryRebuild(jobId!), onSuccess: refresh });
  const busy = submit.isPending || retry.isPending || job.data?.status === "queued" || job.data?.status === "running";
  return (
    <Card>
      <CardHeader><CardTitle icon={<Wrench size={16} />} title="投影维护与诊断" description="日常失败事件可在任务时间线重试；需要修复整个索引时，再从已审核知识重建投影。" /></CardHeader>
      <CardContent className="space-y-3 pt-3">
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>投影重建</Button>
          <Button size="sm" variant="ghost" aria-expanded={raw} onClick={() => setRaw(!raw)}>原始状态</Button>
        </div>
        {expanded ? <div className="space-y-3 rounded-lg border bg-subtle/50 p-3">
          <Field label="投影目标">
            <Select aria-label="投影目标" value={target} disabled={busy} onChange={(e) => { setTarget(e.target.value as ProjectionTarget); setConfirmed(false); }}>
              <option value="qdrant">Qdrant 向量索引</option><option value="neo4j">Neo4j 图谱</option><option value="obsidian">Obsidian 文档</option><option value="all">全部投影</option>
            </Select>
          </Field>
          <Field label="资料集合">
            <Select aria-label="资料集合" value={collection} disabled={busy || collections.isPending || !!collections.error} onChange={(e) => { setCollection(e.target.value); setConfirmed(false); }}>
              <option value="">全部集合</option>{collections.data?.map((item) => <option key={item.slug} value={item.slug}>{item.name}</option>)}
            </Select>
          </Field>
          {collections.error instanceof Error ? <ErrorBlock error={collections.error} onRetry={() => { void collections.refetch(); }} /> : null}
          <p className="text-xs leading-5 text-muted-ink">重建会替换对应投影，不修改正式知识。图谱包含跨集合关系，因此 Neo4j 始终重建完整受管图。请在其他写入任务结束后操作。</p>
          <label className="flex items-start gap-2 text-[13px] text-ink-soft"><input className="mt-1 size-3.5 shrink-0 accent-[var(--color-brand)]" type="checkbox" checked={confirmed} disabled={busy} onChange={(e) => setConfirmed(e.target.checked)} />我确认目标和范围，允许替换对应投影。</label>
          <Button size="sm" disabled={!confirmed || busy || collections.isPending || !!collections.error} onClick={() => submit.mutate()}>提交重建</Button>
          <p className="text-xs text-muted-ink">任务在后台执行；关闭页面后可从任务时间线继续查看。</p>
        </div> : null}
        {submit.error instanceof Error ? <ErrorBlock error={submit.error} /> : null}
        {job.isFetching && !job.data ? <p className="text-[13px] text-muted-ink">读取重建记录…</p> : null}
        {job.error instanceof Error ? <ErrorBlock error={job.error} onRetry={() => { void job.refetch(); }} /> : null}
        {job.data ? <div className="min-w-0 space-y-1.5 rounded-lg border px-3 py-2.5" aria-live="polite">
          <div className="flex min-w-0 items-center justify-between gap-2">
            <p className="min-w-0 truncate text-[13px] font-medium text-ink">{TARGET_LABELS[job.data.payload.target] ?? job.data.payload.target} · {job.data.payload.collection_slug || "全部集合"}</p>
            <StatusBadge status={job.data.status} />
          </div>
          <p className="text-xs text-muted-ink">执行尝试 {job.data.attempts} 次</p>
          {job.data.last_error ? <p className="break-words rounded-md bg-danger-soft px-2.5 py-2 text-xs leading-5 text-red-900">{job.data.last_error}</p> : null}
          {job.data.payload.result ? <p className="text-[13px] text-ink-soft">完成：{job.data.payload.result.chunks} 个切片、{job.data.payload.result.entities} 个实体、{job.data.payload.result.relations} 条关系、{job.data.payload.result.topics} 个集合文档。</p> : null}
          {job.data.status === "failed" ? <Button size="sm" variant="outline" disabled={retry.isPending} onClick={() => retry.mutate()}>重试此重建</Button> : null}
        </div> : null}
        {retry.error instanceof Error ? <ErrorBlock error={retry.error} /> : null}
        {raw ? <div className="space-y-2 border-t pt-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs text-muted-ink">只读的服务原始返回，用于排查问题。</p>
            <Button size="sm" variant="ghost" onClick={() => { void health.refetch(); void overview.refetch(); }}><RefreshCw size={13} />刷新</Button>
          </div>
          {health.isPending ? <p className="text-[13px] text-muted-ink">读取健康状态…</p> : null}
          {health.error instanceof Error ? <ErrorBlock error={health.error} /> : null}
          {overview.error instanceof Error ? <ErrorBlock error={overview.error} /> : null}
          {health.data ? <div><p className="text-[13px] font-medium text-ink-soft">API / Knowledge</p><JsonDetails value={{ ...health.data }} /></div> : null}
          {overview.data ? <div><p className="text-[13px] font-medium text-ink-soft">Runtime</p><JsonDetails value={{ ...overview.data }} /></div> : null}
        </div> : null}
      </CardContent>
    </Card>
  );
}
