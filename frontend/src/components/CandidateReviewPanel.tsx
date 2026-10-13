import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Check, ChevronLeft, ChevronRight, GitMerge, PauseCircle, Plus, Sparkles, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api, type CandidateDecisionKind, type EntityCandidate, type KnowledgeCandidate, type MergeSuggestion, type RelationCandidate } from "../lib/api";
import { documentTitle, useIngestions, usePaperTitles } from "../lib/hooks";
import { entityTypeLabel, relationTypeLabel } from "../lib/labels";
import { cn } from "../lib/utils";
import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "./AsyncState";
import { confirmApproveAsNew, DuplicateGroupList, MergeSuggestions } from "./DuplicateReview";
import { pageLabel } from "./report/SourceDrawer";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Select } from "./ui/field";
import { Disclosure, Segmented } from "./ui/page";

const PAGE_SIZE = 25;
type Decision = CandidateDecisionKind;
const DECISION_LABELS: Record<Decision, string> = { approve: "批准", reject: "驳回", defer: "搁置", merge: "合并" };

function EndpointName({ name, canonical }: { name?: string; canonical?: string }) {
  return (
    <>
      {name ?? "未解析实体"}
      {canonical ? <span className="ml-1 text-xs font-normal text-muted-ink" title="该实体已合并，关系会连到合并后的实体">（并入「{canonical}」）</span> : null}
    </>
  );
}

function candidateTitle(record: KnowledgeCandidate) {
  if (record.kind === "entity") return (record.candidate as EntityCandidate).name;
  const relation = record.candidate as RelationCandidate;
  return `${relation.source_name ?? "未解析实体"} → ${relation.target_name ?? "未解析实体"}`;
}

function Confidence({ value }: { value: number }) {
  const percent = Math.round(value * 100);
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-ink" title="抽取模型给出的置信度，仅供参考">
      <span className="h-1.5 w-10 overflow-hidden rounded-full bg-subtle"><span className={cn("block h-full rounded-full", percent >= 90 ? "bg-emerald-500" : percent >= 75 ? "bg-sky-500" : "bg-amber-500")} style={{ width: `${percent}%` }} /></span>
      <span className="tabular-nums">{percent}%</span>
    </span>
  );
}

function CandidateCard({ record, onDecide, onSelect, pending, selected, titles }: {
  record: KnowledgeCandidate;
  onDecide: (decision: Decision, canonicalId?: string) => void;
  onSelect: () => void;
  pending: boolean;
  selected: boolean;
  titles: Map<string, string>;
}) {
  const candidate = record.candidate;
  const title = candidateTitle(record);
  const relation = record.kind === "relation" ? candidate as RelationCandidate : null;
  const suggestions: MergeSuggestion[] = record.kind === "entity" ? (candidate as EntityCandidate).merge_suggestions ?? [] : [];
  const [mergeTarget, setMergeTarget] = useState("");
  const target = suggestions.find((item) => item.entity_id === mergeTarget) ?? suggestions[0];
  return (
    <article className={cn("rounded-xl border bg-panel p-4 shadow-[var(--shadow-card)] transition-colors", selected && "border-brand/50 bg-brand-tint/60")}>
      <div className="flex gap-3">
        <input aria-label={`选择候选：${title}`} checked={selected} className="mt-1 size-4 shrink-0 accent-[var(--color-brand)]" disabled={pending} onChange={onSelect} type="checkbox" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Badge tone={record.kind === "entity" ? "brand" : "info"}>{record.kind === "entity" ? "实体" : "关系"}</Badge>
            <span className="text-xs text-muted-ink">{record.kind === "entity" ? entityTypeLabel(candidate.type) : relationTypeLabel(candidate.type)}</span>
            <Confidence value={candidate.confidence} />
          </div>
          {relation ? (
            <p className="mt-2 break-words text-[15px] font-medium leading-6 text-ink">
              <EndpointName canonical={relation.source_canonical_name} name={relation.source_name} />
              <span className="mx-2 inline-flex items-center gap-1 rounded-md bg-info-soft px-1.5 align-middle text-xs font-medium text-sky-800">{relationTypeLabel(relation.type)}<ArrowRight size={11} /></span>
              <EndpointName canonical={relation.target_canonical_name} name={relation.target_name} />
            </p>
          ) : <p className="mt-2 break-words text-[15px] font-medium leading-6 text-ink">{title}</p>}
          <p className="mt-1 text-[13.5px] leading-6 text-ink-soft">{candidate.summary}</p>
          {target ? <MergeSuggestions candidateId={candidate.id} candidateType={candidate.type} disabled={pending} onChange={setMergeTarget} suggestions={suggestions} value={target.entity_id} /> : null}
          {candidate.metadata?.evidence_located === false ? (
            <p className="mt-2.5 flex gap-1.5 rounded-md border border-amber-200 bg-warning-soft/60 px-2.5 py-1.5 text-xs leading-5 text-amber-950" role="note">
              <AlertTriangle className="mt-0.5 shrink-0" size={13} />
              <span>模型给出的引文未能在原文中找到，下面显示的是该切片开头，不一定支持这条知识。请核对原文后再批准。</span>
            </p>
          ) : null}
          <details className="group mt-2.5">
            <summary className="list-none">
              <blockquote className="rounded-md border-l-2 border-brand/50 bg-subtle/70 px-3 py-2 text-[13px] leading-6 text-ink-soft">
                <span className="line-clamp-2 group-open:line-clamp-none">“{candidate.evidence.quote}”</span>
                <footer className="mt-1 text-xs text-muted-ink">{documentTitle(null, candidate.evidence.paper_id, titles)} · {pageLabel(candidate.evidence.page_start, candidate.evidence.page_end)}</footer>
              </blockquote>
            </summary>
          </details>
          <div className="mt-3 flex flex-wrap justify-end gap-1.5">
            <Button disabled={pending} size="sm" variant="ghost" onClick={() => onDecide("defer")}><PauseCircle size={14} />搁置</Button>
            <Button disabled={pending} size="sm" variant="danger-outline" onClick={() => onDecide("reject")}><X size={14} />驳回</Button>
            {target ? (
              <>
                <Button disabled={pending} size="sm" variant="outline" onClick={() => { if (confirmApproveAsNew(title, suggestions)) onDecide("approve"); }}><Plus size={14} />作为新实体批准</Button>
                <Button disabled={pending} size="sm" onClick={() => onDecide("merge", target.entity_id)}><GitMerge size={14} />合并到「{target.name}」</Button>
              </>
            ) : <Button disabled={pending} size="sm" onClick={() => onDecide("approve")}><Check size={14} />批准</Button>}
          </div>
        </div>
      </div>
    </article>
  );
}

export function CandidateReviewPanel({ initialIngestionId, collectionSlug }: { initialIngestionId?: string | null; collectionSlug?: string | null } = {}) {
  const [ingestionId, setIngestionId] = useState("");
  const [view, setView] = useState<"all" | "duplicates">("all");
  const [kind, setKind] = useState<"all" | "entity" | "relation">("all");
  const [paperId, setPaperId] = useState("");
  const [minConfidence, setMinConfidence] = useState<"all" | "0.8" | "0.9">("all");
  const [offset, setOffset] = useState(0);
  const [selectedCandidateIds, setSelectedCandidateIds] = useState<string[]>([]);
  const [reviewFeedback, setReviewFeedback] = useState<{ summary: string; issues: Array<{ candidate_id: string; reason: string }> }>();
  const client = useQueryClient();
  const titles = usePaperTitles();
  const ingestions = useIngestions();
  const reviewable = useMemo(() => (ingestions.data ?? []).filter((item) => item.candidate_count > 0 || item.status === "needs_review"), [ingestions.data]);
  const preferred = reviewable.find((item) => item.id === initialIngestionId)
    ?? reviewable.find((item) => item.collection_slug === collectionSlug)
    ?? reviewable.find((item) => item.status === "needs_review")
    ?? reviewable[0];
  const selectedId = ingestionId || preferred?.id || "";
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
  const duplicates = useQuery({
    queryKey: ["knowledge-duplicates", selectedId],
    queryFn: () => api.duplicateGroups(selectedId),
    enabled: Boolean(selectedId),
  });
  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ["knowledge-candidates", selectedId] });
    void client.invalidateQueries({ queryKey: ["knowledge-duplicates", selectedId] });
    void client.invalidateQueries({ queryKey: ["knowledge-map"] });
    void client.invalidateQueries({ queryKey: ["knowledge-map-node"] });
    void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
    void client.invalidateQueries({ queryKey: ["knowledge-collection"] });
  };
  const decide = useMutation({
    mutationFn: ({ candidateId, decision, canonicalId }: { candidateId: string; decision: Decision; canonicalId?: string }) => api.decideCandidate(candidateId, decision, canonicalId),
    onSuccess: (_result, variables) => {
      setSelectedCandidateIds((current) => current.filter((candidateId) => candidateId !== variables.candidateId));
      if (variables.decision === "merge") setReviewFeedback({ summary: "已合并到已有实体，证据和出处已挂到该实体上。", issues: [] });
      invalidate();
    },
  });
  const mergeGroup = useMutation({
    mutationFn: async ({ candidateIds, canonicalId, canonicalName, approveFirst }: { candidateIds: string[]; canonicalId?: string; canonicalName: string; approveFirst?: string }) => {
      let target = canonicalId;
      if (approveFirst) {
        const approved = await api.decideCandidate(approveFirst, "approve");
        target = approved.candidate.candidate.canonical_id ?? undefined;
        if (!target) throw new Error("规范实体批准后没有返回实体编号，请刷新后重试。");
      }
      const result = candidateIds.length ? await api.decideCandidatesBulk(selectedId, candidateIds, "merge", target) : null;
      return { result, canonicalName, approved: Boolean(approveFirst) };
    },
    onSuccess: ({ result, canonicalName, approved }) => {
      const merged = result ? result.applied + result.replayed : 0;
      setReviewFeedback({
        summary: `${approved ? `已将「${canonicalName}」批准为规范实体，` : ""}${merged} 条候选已合并到「${canonicalName}」${result?.skipped.length ? `，${result.skipped.length} 条需要逐条处理` : ""}。`,
        issues: result?.skipped ?? [],
      });
      invalidate();
    },
    onError: invalidate,
  });
  const decideBulk = useMutation({
    mutationFn: ({ candidateIds, decision }: { candidateIds: string[]; decision: Decision }) => api.decideCandidatesBulk(selectedId, candidateIds, decision),
    onSuccess: (result) => {
      setSelectedCandidateIds([]);
      setReviewFeedback({
        summary: `已${DECISION_LABELS[result.decision]} ${result.applied} 条${result.replayed ? `，${result.replayed} 条此前已处理` : ""}${result.skipped.length ? `，${result.skipped.length} 条需要逐条处理` : ""}。`,
        issues: result.skipped,
      });
      invalidate();
    },
  });
  const approveReady = useMutation({
    mutationFn: () => api.approveReadyCandidates(selectedId),
    onSuccess: (result) => {
      setSelectedCandidateIds([]);
      setReviewFeedback({
        summary: `已批准 ${result.published_entities} 个无冲突实体；${result.skipped_conflicts} 个有同名冲突的实体和 ${result.blocked_relations} 条关系保留给你逐条审核。`,
        issues: [],
      });
      invalidate();
    },
  });
  const autoApproveHighConfidence = useMutation({
    mutationFn: () => api.autoApproveHighConfidence(selectedId),
    onSuccess: (result) => {
      setSelectedCandidateIds([]);
      setReviewFeedback({
        summary: `按置信度高于 ${Math.round(result.min_confidence_exclusive * 100)}% 处理了 ${result.requested} 条：批准 ${result.applied} 条，${result.skipped.length} 条保留人工审核。`,
        issues: result.skipped,
      });
      invalidate();
    },
  });
  const selectedIngestion = reviewable.find((item) => item.id === selectedId);
  const pageCandidateIds = candidates.data?.items.map((record) => record.candidate.id) ?? [];
  const allPageSelected = pageCandidateIds.length > 0 && pageCandidateIds.every((candidateId) => selectedCandidateIds.includes(candidateId));
  const reviewPending = decide.isPending || decideBulk.isPending || approveReady.isPending || autoApproveHighConfidence.isPending || mergeGroup.isPending;
  const pageStart = candidates.data?.total ? candidates.data.offset + 1 : 0;
  const pageEnd = candidates.data ? Math.min(candidates.data.offset + candidates.data.items.length, candidates.data.total) : 0;
  const clearSelection = () => setSelectedCandidateIds([]);
  const resetPaging = () => { setOffset(0); clearSelection(); };
  const toggleCandidate = (candidateId: string) => setSelectedCandidateIds((current) => current.includes(candidateId) ? current.filter((id) => id !== candidateId) : [...current, candidateId]);
  const togglePageSelection = () => setSelectedCandidateIds((current) => allPageSelected ? current.filter((candidateId) => !pageCandidateIds.includes(candidateId)) : [...new Set([...current, ...pageCandidateIds])]);
  const runBulkDecision = (decision: Decision) => {
    if (!selectedCandidateIds.length) return;
    if (window.confirm(`确认将所选 ${selectedCandidateIds.length} 条候选全部${DECISION_LABELS[decision]}吗？每条都会留下独立的审核记录。`)) {
      decideBulk.mutate({ candidateIds: selectedCandidateIds, decision });
    }
  };
  const runApproveReady = () => {
    if (window.confirm("确认批准这批资料中所有没有同名冲突的实体吗？有冲突的实体和全部关系仍会保留给你逐条审核。")) approveReady.mutate();
  };
  const runAutoApproveHighConfidence = () => {
    if (window.confirm("确认自动批准置信度高于 90% 的候选吗？有同名冲突的实体、端点尚未发布的关系仍会保留人工审核。")) autoApproveHighConfidence.mutate();
  };
  const paperOptions = Array.from(titles.entries());
  const mutationError = (decide.error ?? decideBulk.error ?? approveReady.error ?? autoApproveHighConfidence.error ?? mergeGroup.error) as Error | null;
  const duplicateCount = duplicates.data?.groups.length;
  const publishedShare = selectedIngestion && selectedIngestion.candidate_count ? selectedIngestion.published_count / selectedIngestion.candidate_count : 0;

  if (ingestions.isPending) return <LoadingBlock label="读取待审核资料…" />;
  if (ingestions.error instanceof Error) return <ErrorBlock error={ingestions.error} onRetry={() => ingestions.refetch()} />;
  if (!reviewable.length) {
    return <EmptyBlock title="还没有可审核的资料" action={<Link className="text-[13px] font-medium text-brand hover:underline" to="/knowledge?import=1">去导入资料</Link>}>导入论文后，系统抽取出的候选知识会出现在这里。</EmptyBlock>;
  }
  return (
    <section className="min-w-0" aria-label="知识候选审核">
      <h2 className="sr-only">知识候选审核</h2>
      <div className="rounded-xl border bg-panel p-4 shadow-[var(--shadow-card)]">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            {reviewable.length > 1 ? (
              <label className="block text-[13px] font-medium text-ink-soft">资料来源
                <Select className="mt-1.5 max-w-md" value={selectedId} onChange={(event) => { setIngestionId(event.target.value); resetPaging(); setReviewFeedback(undefined); }}>
                  {reviewable.map((ingestion) => <option key={ingestion.id} value={ingestion.id}>{ingestion.collection} · {ingestion.sources.length} 篇</option>)}
                </Select>
              </label>
            ) : <p className="text-[15px] font-semibold">{selectedIngestion?.collection}</p>}
            {selectedIngestion ? <p className="mt-1 text-[13px] text-muted-ink">已发布 {selectedIngestion.published_count} / {selectedIngestion.candidate_count} 条候选</p> : null}
          </div>
          {candidates.data ? <p className="text-[13px] text-ink-soft"><span className="text-xl font-semibold tabular-nums text-amber-700">{candidates.data.total}</span> 条待审核{kind !== "all" || paperId || minConfidence !== "all" ? "（当前筛选）" : ""}</p> : null}
        </div>
        <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-subtle"><div className="h-full rounded-full bg-brand transition-[width]" style={{ width: `${Math.round(publishedShare * 100)}%` }} /></div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Segmented label="审核视图" value={view} onChange={(value) => { setView(value); clearSelection(); }} options={[{ value: "all", label: "全部候选" }, { value: "duplicates", label: "疑似重复", count: duplicateCount }]} />
        </div>
        {view === "all" ? <div className="mt-2 flex flex-wrap items-center gap-2">
          <Segmented label="候选类型" value={kind} onChange={(value) => { setKind(value); resetPaging(); }} options={[{ value: "all", label: "全部" }, { value: "entity", label: "实体" }, { value: "relation", label: "关系" }]} />
          <Select aria-label="最低置信度" className="h-9 w-auto" value={minConfidence} onChange={(event) => { setMinConfidence(event.target.value as typeof minConfidence); resetPaging(); }}>
            <option value="all">全部置信度</option><option value="0.8">置信度 ≥ 80%</option><option value="0.9">置信度 ≥ 90%</option>
          </Select>
          {paperOptions.length ? (
            <Select aria-label="来源文献" className="h-9 w-auto max-w-full sm:max-w-72" value={paperId} onChange={(event) => { setPaperId(event.target.value); resetPaging(); }}>
              <option value="">全部文献</option>
              {paperOptions.map(([id, title]) => <option key={id} value={id}>{title}</option>)}
            </Select>
          ) : null}
        </div> : null}
        <Disclosure className="mt-3" summary={<span className="inline-flex items-center gap-1.5"><Sparkles size={14} />批量快捷处理</span>}>
          <div className="space-y-3 text-[13px]">
            <div className="flex flex-wrap items-center justify-between gap-2"><p className="min-w-0 flex-1 text-muted-ink">批准置信度高于 90% 的候选。冲突实体和端点未发布的关系会被跳过。</p><Button disabled={reviewPending} onClick={runAutoApproveHighConfidence} size="sm" variant="outline">自动通过 &gt;90%</Button></div>
            <div className="flex flex-wrap items-center justify-between gap-2"><p className="min-w-0 flex-1 text-muted-ink">批准所有没有同名冲突的实体；关系仍需逐条审核。</p><Button disabled={reviewPending} onClick={runApproveReady} size="sm" variant="outline">批准无冲突实体</Button></div>
          </div>
        </Disclosure>
      </div>

      {reviewFeedback ? (
        <Notice className="mt-4" tone="success" title={reviewFeedback.summary} action={<Button size="sm" variant="ghost" onClick={() => setReviewFeedback(undefined)}>知道了</Button>}>
          {reviewFeedback.issues.length ? <ul className="list-disc pl-5 text-xs">{reviewFeedback.issues.slice(0, 5).map((issue) => <li key={issue.candidate_id}>{issue.reason}</li>)}{reviewFeedback.issues.length > 5 ? <li>其余 {reviewFeedback.issues.length - 5} 条请在列表中逐条处理。</li> : null}</ul> : null}
        </Notice>
      ) : null}
      {mutationError ? <div className="mt-4"><ErrorBlock error={mutationError} /></div> : null}

      {view === "duplicates" ? (
        <>
          {duplicates.isPending ? <div className="mt-4"><LoadingBlock label="查找疑似重复…" /></div> : null}
          {duplicates.error instanceof Error ? <div className="mt-4"><ErrorBlock error={duplicates.error} onRetry={() => duplicates.refetch()} /></div> : null}
          {duplicates.data ? (
            <DuplicateGroupList
              groups={duplicates.data.groups}
              pending={reviewPending}
              titles={titles}
              onApproveAsCanonical={(candidate, others) => mergeGroup.mutate({ approveFirst: candidate.id, candidateIds: others.map((item) => item.id), canonicalName: candidate.name })}
              onApproveAsNew={(candidate) => { if (confirmApproveAsNew(candidate.name, candidate.merge_suggestions ?? [])) decide.mutate({ candidateId: candidate.id, decision: "approve" }); }}
              onMerge={(candidateId, target) => decide.mutate({ candidateId, decision: "merge", canonicalId: target.entity_id })}
              onMergeAll={(candidateIds, target) => mergeGroup.mutate({ candidateIds, canonicalId: target.entity_id, canonicalName: target.name })}
              onReject={(candidateId) => decide.mutate({ candidateId, decision: "reject" })}
            />
          ) : null}
        </>
      ) : null}

      {view === "all" && candidates.data?.items.length ? (
        <div className="mt-5 flex items-center justify-between gap-3 px-1">
          <label className="flex items-center gap-2 text-[13px] font-medium text-ink-soft"><input aria-label="全选当前页候选" checked={allPageSelected} className="size-4 accent-[var(--color-brand)]" disabled={reviewPending} onChange={togglePageSelection} type="checkbox" />全选本页</label>
          <span className="text-xs tabular-nums text-muted-ink">第 {pageStart}–{pageEnd} 条，共 {candidates.data.total} 条</span>
        </div>
      ) : null}
      {view === "all" && selectedId && candidates.isPending ? <div className="mt-4"><LoadingBlock label="读取候选…" /></div> : null}
      {view === "all" && candidates.error instanceof Error ? <div className="mt-4"><ErrorBlock error={candidates.error} onRetry={() => candidates.refetch()} /></div> : null}
      {view === "all" ? <div className="mt-3 space-y-3">
        {candidates.data?.items.map((record) => <CandidateCard key={record.candidate.id} onSelect={() => toggleCandidate(record.candidate.id)} pending={reviewPending} record={record} selected={selectedCandidateIds.includes(record.candidate.id)} titles={titles} onDecide={(decision, canonicalId) => decide.mutate({ candidateId: record.candidate.id, decision, canonicalId })} />)}
      </div> : null}
      {view === "all" && selectedId && candidates.data && !candidates.data.total ? (
        <EmptyBlock className="mt-4" icon={<Check size={19} />} title={kind !== "all" || paperId || minConfidence !== "all" ? "当前筛选下没有待审核候选" : "这批资料已全部审核完"}>
          {kind !== "all" || paperId || minConfidence !== "all" ? "调整筛选条件看看其他候选。" : "批准的知识已经可以在检索和研究中使用。"}
        </EmptyBlock>
      ) : null}
      {view === "all" && candidates.data && candidates.data.total > PAGE_SIZE ? (
        <div className="mt-5 flex items-center justify-between gap-3">
          <Button disabled={!offset || candidates.isFetching} size="sm" variant="outline" onClick={() => { setOffset(Math.max(0, offset - PAGE_SIZE)); clearSelection(); }}><ChevronLeft size={14} />上一页</Button>
          <span className="text-xs tabular-nums text-muted-ink">{pageStart}–{pageEnd} / {candidates.data.total}</span>
          <Button disabled={candidates.data.next_offset === null || candidates.isFetching} size="sm" variant="outline" onClick={() => { setOffset(candidates.data?.next_offset ?? offset); clearSelection(); }}>下一页<ChevronRight size={14} /></Button>
        </div>
      ) : null}

      {selectedCandidateIds.length ? (
        <div className="sticky bottom-4 z-20 mt-5 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-ink/10 bg-toolbar px-4 py-2.5 text-white shadow-[var(--shadow-pop)]" role="toolbar" aria-label="批量操作">
          <span className="text-[13px]">已选 <strong className="tabular-nums">{selectedCandidateIds.length}</strong> 条</span>
          <div className="flex flex-wrap gap-1.5">
            <Button className="text-white/80 hover:bg-white/10 hover:text-white" size="sm" variant="ghost" onClick={clearSelection}>取消选择</Button>
            <Button className="text-white/90 hover:bg-white/10 hover:text-white" disabled={reviewPending} size="sm" variant="ghost" onClick={() => runBulkDecision("defer")}>批量搁置</Button>
            <Button className="bg-transparent text-red-200 ring-1 ring-red-300/40 hover:bg-red-500/20" disabled={reviewPending} size="sm" variant="ghost" onClick={() => runBulkDecision("reject")}>批量驳回</Button>
            <Button className="bg-white text-toolbar hover:bg-white/90" disabled={reviewPending} size="sm" onClick={() => runBulkDecision("approve")}><Check size={14} />批量批准</Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
