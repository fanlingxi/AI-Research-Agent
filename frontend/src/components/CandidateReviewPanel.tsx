import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, Filter, FlaskConical, PauseCircle, ShieldCheck, X } from "lucide-react";
import { useMemo, useState } from "react";

import { api, type KnowledgeCandidate } from "../lib/api";
import { formatDate } from "../lib/utils";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./AsyncState";
import { StatusBadge } from "./StatusBadge";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader } from "./ui/card";

const PAGE_SIZE = 25;

function candidateTitle(record: KnowledgeCandidate) {
  if ("name" in record.candidate) return record.candidate.name;
  return `${record.candidate.source_name ?? "未解析实体"} → ${record.candidate.target_name ?? "未解析实体"}`;
}

function CandidateCard({ record, onDecide, onSelect, pending, selected }: {
  record: KnowledgeCandidate;
  onDecide: (decision: "approve" | "reject" | "defer") => void;
  onSelect: () => void;
  pending: boolean;
  selected: boolean;
}) {
  const candidate = record.candidate;
  const title = candidateTitle(record);
  return (
    <article className="rounded-lg border bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3"><div className="flex min-w-0 gap-3"><input aria-label={`选择候选：${title}`} checked={selected} className="mt-1 size-4 accent-[#176469]" disabled={pending} onChange={onSelect} type="checkbox" /><div className="min-w-0"><p className="font-medium">{title}</p><p className="mt-1 text-xs text-muted-ink">{record.kind} · {candidate.type} · confidence {Math.round(candidate.confidence * 100)}%</p></div></div><StatusBadge status={candidate.status} /></div>
      <p className="mt-3 text-sm leading-6 text-slate-700">{candidate.summary}</p>
      <blockquote className="mt-3 rounded-md border-l-2 border-brand bg-brand-soft/40 px-3 py-2 text-xs leading-5 text-slate-700">“{candidate.evidence.quote}”<footer className="mt-1 font-mono text-[10px] text-muted-ink">{candidate.evidence.paper_id} · p. {candidate.evidence.page_start}{candidate.evidence.page_end !== candidate.evidence.page_start ? `–${candidate.evidence.page_end}` : ""}</footer></blockquote>
      <div className="mt-3 flex flex-wrap justify-end gap-2"><Button disabled={pending} size="sm" variant="outline" onClick={() => onDecide("defer")}><PauseCircle size={14} /> 待定</Button><Button disabled={pending} size="sm" variant="outline" onClick={() => onDecide("reject")}><X size={14} /> 驳回</Button><Button disabled={pending} size="sm" onClick={() => onDecide("approve")}><Check size={14} /> 批准</Button></div>
    </article>
  );
}

export function CandidateReviewPanel() {
  const [ingestionId, setIngestionId] = useState("");
  const [kind, setKind] = useState<"all" | "entity" | "relation">("all");
  const [paperId, setPaperId] = useState("");
  const [minConfidence, setMinConfidence] = useState<"all" | "0.8" | "0.9">("all");
  const [offset, setOffset] = useState(0);
  const [selectedCandidateIds, setSelectedCandidateIds] = useState<string[]>([]);
  const [reviewFeedback, setReviewFeedback] = useState<{ summary: string; issues: Array<{ candidate_id: string; reason: string }> }>();
  const client = useQueryClient();
  const ingestions = useQuery({ queryKey: ["knowledge-ingestions"], queryFn: api.ingestions });
  const selectedId = ingestionId || ingestions.data?.[0]?.id || "";
  const candidates = useQuery({
    queryKey: ["knowledge-candidates", selectedId, "draft", kind, paperId, minConfidence, offset],
    queryFn: () => api.candidatePage(selectedId, {
      kind: kind === "all" ? undefined : kind,
      paperId: paperId.trim() || undefined,
      minConfidence: minConfidence === "all" ? undefined : Number(minConfidence),
      offset,
      limit: PAGE_SIZE,
    }),
    enabled: Boolean(selectedId),
  });
  const decide = useMutation({
    mutationFn: ({ candidateId, decision }: { candidateId: string; decision: "approve" | "reject" | "defer" }) => api.decideCandidate(candidateId, decision),
    onSuccess: (_result, variables) => {
      setSelectedCandidateIds((current) => current.filter((candidateId) => candidateId !== variables.candidateId));
      void client.invalidateQueries({ queryKey: ["knowledge-candidates", selectedId] });
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
    },
  });
  const decideBulk = useMutation({
    mutationFn: ({ candidateIds, decision }: { candidateIds: string[]; decision: "approve" | "reject" | "defer" }) => api.decideCandidatesBulk(selectedId, candidateIds, decision),
    onSuccess: (result) => {
      setSelectedCandidateIds([]);
      setReviewFeedback({
        summary: `本次请求 ${result.requested} 条：已写入 ${result.applied} 条，幂等重放 ${result.replayed} 条，需逐条处理 ${result.skipped.length} 条。`,
        issues: result.skipped,
      });
      void client.invalidateQueries({ queryKey: ["knowledge-candidates", selectedId] });
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
    },
  });
  const approveReady = useMutation({
    mutationFn: () => api.approveReadyCandidates(selectedId),
    onSuccess: (result) => {
      setSelectedCandidateIds([]);
      setReviewFeedback({
        summary: `已自动批准 ${result.published_entities} 条无冲突实体；${result.skipped_conflicts} 条冲突实体及 ${result.blocked_relations} 条关系保留逐条审核。`,
        issues: [],
      });
      void client.invalidateQueries({ queryKey: ["knowledge-candidates", selectedId] });
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
    },
  });
  const autoApproveHighConfidence = useMutation({
    mutationFn: () => api.autoApproveHighConfidence(selectedId),
    onSuccess: (result) => {
      setSelectedCandidateIds([]);
      setReviewFeedback({
        summary: `已按置信度 > ${Math.round(result.min_confidence_exclusive * 100)}% 自动处理 ${result.requested} 条：发布 ${result.applied} 条，保留 ${result.skipped.length} 条人工审核。`,
        issues: result.skipped,
      });
      void client.invalidateQueries({ queryKey: ["knowledge-candidates", selectedId] });
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
    },
  });
  const selectedIngestion = useMemo(() => ingestions.data?.find((item) => item.id === selectedId), [ingestions.data, selectedId]);
  const pageCandidateIds = candidates.data?.items.map((record) => record.candidate.id) ?? [];
  const allPageSelected = pageCandidateIds.length > 0 && pageCandidateIds.every((candidateId) => selectedCandidateIds.includes(candidateId));
  const reviewPending = decide.isPending || decideBulk.isPending || approveReady.isPending || autoApproveHighConfidence.isPending;
  const pageStart = candidates.data ? candidates.data.total ? candidates.data.offset + 1 : 0 : 0;
  const pageEnd = candidates.data ? Math.min(candidates.data.offset + candidates.data.items.length, candidates.data.total) : 0;
  const clearSelection = () => setSelectedCandidateIds([]);
  const changeIngestion = (value: string) => { setIngestionId(value); setOffset(0); clearSelection(); setReviewFeedback(undefined); };
  const changeKind = (value: "all" | "entity" | "relation") => { setKind(value); setOffset(0); clearSelection(); };
  const changePaperId = (value: string) => { setPaperId(value); setOffset(0); clearSelection(); };
  const changeMinConfidence = (value: "all" | "0.8" | "0.9") => { setMinConfidence(value); setOffset(0); clearSelection(); };
  const changePage = (nextOffset: number) => { setOffset(nextOffset); clearSelection(); };
  const toggleCandidate = (candidateId: string) => setSelectedCandidateIds((current) => current.includes(candidateId) ? current.filter((id) => id !== candidateId) : [...current, candidateId]);
  const togglePageSelection = () => setSelectedCandidateIds((current) => allPageSelected ? current.filter((candidateId) => !pageCandidateIds.includes(candidateId)) : [...new Set([...current, ...pageCandidateIds])]);
  const runBulkDecision = (decision: "approve" | "reject" | "defer") => {
    if (!selectedCandidateIds.length) return;
    const label = { approve: "批准", reject: "驳回", defer: "待定" }[decision];
    if (window.confirm(`确认将所选 ${selectedCandidateIds.length} 条候选批量${label}吗？该操作会留下独立的审核记录。`)) {
      decideBulk.mutate({ candidateIds: selectedCandidateIds, decision });
    }
  };
  const runApproveReady = () => {
    if (window.confirm("确认批准本次 ingestion 中所有无冲突实体吗？存在同名冲突的实体和全部关系都会保留给人工审核。")) {
      approveReady.mutate();
    }
  };
  const runAutoApproveHighConfidence = () => {
    if (window.confirm("确认自动通过当前 ingestion 内置信度严格大于 90% 的候选吗？同名冲突实体和端点未发布的关系仍会保留人工审核。")) {
      autoApproveHighConfidence.mutate();
    }
  };
  return (
    <Card>
      <CardHeader>
        <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">知识治理</p><h2 className="mt-1 text-lg font-semibold">知识候选审核</h2></div>
        <FlaskConical className="text-brand" size={20} />
      </CardHeader>
      <CardContent>
        <p className="text-sm leading-6 text-muted-ink">候选保持在 ingestion 范围内。关系卡会解析为实体名称，审核操作不会写入 Project Memory 或绕过正式发布治理。</p>
        {ingestions.isPending ? <LoadingBlock label="读取 ingestions…" /> : null}
        {ingestions.error instanceof Error ? <ErrorBlock error={ingestions.error} onRetry={() => ingestions.refetch()} /> : null}
        {ingestions.data?.length ? <label className="mt-4 block text-sm font-medium">Ingestion<select className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2 text-sm" value={selectedId} onChange={(event) => changeIngestion(event.target.value)}>{ingestions.data.map((ingestion) => <option key={ingestion.id} value={ingestion.id}>{ingestion.collection} · {ingestion.topic} · {ingestion.status}</option>)}</select></label> : null}
        {selectedIngestion ? <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-ink"><Badge tone="neutral">{selectedIngestion.collection_slug}</Badge><span>{selectedIngestion.candidate_count} candidates</span><span>updated {formatDate(selectedIngestion.updated_at)}</span></div> : null}
        <div className="mt-5 rounded-lg border bg-slate-50 p-3"><div className="flex items-center gap-2 text-sm font-medium"><Filter size={15} />筛选待审核候选</div><div className="mt-3 grid gap-2 sm:grid-cols-3"><select aria-label="Candidate kind" className="rounded-lg border bg-white px-3 py-1.5 text-sm" value={kind} onChange={(event) => changeKind(event.target.value as typeof kind)}><option value="all">全部类型</option><option value="entity">仅实体</option><option value="relation">仅关系</option></select><select aria-label="Minimum confidence" className="rounded-lg border bg-white px-3 py-1.5 text-sm" value={minConfidence} onChange={(event) => changeMinConfidence(event.target.value as typeof minConfidence)}><option value="all">全部置信度</option><option value="0.8">≥ 80% 置信度</option><option value="0.9">≥ 90% 置信度</option></select><input aria-label="Paper ID" className="rounded-lg border bg-white px-3 py-1.5 text-sm" onChange={(event) => changePaperId(event.target.value)} placeholder="按文献 ID 筛选" value={paperId} /></div></div>
        {candidates.data?.items.length ? <div className="mt-4 rounded-lg border border-brand/20 bg-brand-soft/35 p-3"><div className="flex flex-wrap items-center justify-between gap-3"><label className="flex items-center gap-2 text-sm font-medium"><input aria-label="全选当前页候选" checked={allPageSelected} className="size-4 accent-[#176469]" disabled={reviewPending} onChange={togglePageSelection} type="checkbox" />全选当前页</label><span className="text-xs text-muted-ink">已选 {selectedCandidateIds.length} 条；每页最多 {PAGE_SIZE} 条。</span></div><div className="mt-3 flex flex-wrap gap-2"><Button disabled={reviewPending} onClick={runAutoApproveHighConfidence} size="sm"><ShieldCheck size={14} />自动通过 &gt;90%</Button><Button disabled={reviewPending} onClick={runApproveReady} size="sm" variant="secondary"><ShieldCheck size={14} />自动批准无冲突实体</Button><Button disabled={!selectedCandidateIds.length || reviewPending} onClick={() => runBulkDecision("approve")} size="sm"><Check size={14} />批量批准</Button><Button disabled={!selectedCandidateIds.length || reviewPending} onClick={() => runBulkDecision("defer")} size="sm" variant="outline"><PauseCircle size={14} />批量待定</Button><Button disabled={!selectedCandidateIds.length || reviewPending} onClick={() => runBulkDecision("reject")} size="sm" variant="danger"><X size={14} />批量驳回</Button></div><p className="mt-3 text-xs leading-5 text-muted-ink">先用筛选定位同类候选，再勾选批量处理。自动通过仅处理置信度严格大于 90% 的候选；实体同名冲突和端点未发布的关系不会被绕过。</p></div> : null}
        {reviewFeedback ? <div className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-950" role="status"><p>{reviewFeedback.summary}</p>{reviewFeedback.issues.length ? <ul className="mt-2 list-disc space-y-1 pl-5 text-xs"><>{reviewFeedback.issues.slice(0, 5).map((issue) => <li key={issue.candidate_id}>{issue.reason}</li>)}</>{reviewFeedback.issues.length > 5 ? <li>其余 {reviewFeedback.issues.length - 5} 条请保留在当前筛选中逐条处理。</li> : null}</ul> : null}</div> : null}
        {selectedId && candidates.isPending ? <div className="mt-5"><LoadingBlock label="读取本页 draft candidates…" /></div> : null}
        {candidates.error instanceof Error ? <div className="mt-5"><ErrorBlock error={candidates.error} onRetry={() => candidates.refetch()} /></div> : null}
        <div className="mt-5 space-y-3">{candidates.data?.items.map((record) => <CandidateCard key={record.candidate.id} onSelect={() => toggleCandidate(record.candidate.id)} pending={reviewPending} record={record} selected={selectedCandidateIds.includes(record.candidate.id)} onDecide={(decision) => decide.mutate({ candidateId: record.candidate.id, decision })} />)}</div>
        {decide.error instanceof Error || decideBulk.error instanceof Error || approveReady.error instanceof Error || autoApproveHighConfidence.error instanceof Error ? <div className="mt-4"><ErrorBlock error={(decide.error ?? decideBulk.error ?? approveReady.error ?? autoApproveHighConfidence.error) as Error} /></div> : null}
        {candidates.data?.total ? <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t pt-4"><p className="text-xs text-muted-ink">显示 {pageStart}–{pageEnd} / {candidates.data.total} 条待审候选</p><div className="flex gap-2"><Button disabled={!offset || candidates.isFetching} size="sm" variant="outline" onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft size={14} />上一页</Button><Button disabled={candidates.data.next_offset === null || candidates.isFetching} size="sm" variant="outline" onClick={() => changePage(candidates.data?.next_offset ?? offset)}>下一页<ChevronRight size={14} /></Button></div></div> : null}
        {ingestions.data && !ingestions.data.length ? <EmptyBlock title="没有 Knowledge ingestion">请先在知识库提交 PDF，审核队列才会出现候选。</EmptyBlock> : null}
        {selectedId && !candidates.isPending && candidates.data && !candidates.data.total ? <EmptyBlock title="没有 draft candidate">当前筛选下没有待审候选；它们可能已经审核完成，或仍在等待抽取。</EmptyBlock> : null}
        <p className="mt-4 flex items-center gap-1 text-xs text-muted-ink"><ChevronRight size={13} />需要 merge/link 的实体候选保留在 Operations Console，以明确选择 canonical entity。</p>
      </CardContent>
    </Card>
  );
}
