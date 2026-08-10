import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, FlaskConical, PauseCircle, X } from "lucide-react";
import { useMemo, useState } from "react";

import { api, type KnowledgeCandidate } from "../lib/api";
import { formatDate } from "../lib/utils";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./AsyncState";
import { StatusBadge } from "./StatusBadge";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader } from "./ui/card";

function candidateTitle(record: KnowledgeCandidate) {
  return "name" in record.candidate
    ? record.candidate.name
    : `${record.candidate.source_candidate_id} → ${record.candidate.target_candidate_id}`;
}

function CandidateCard({ record, onDecide, pending }: {
  record: KnowledgeCandidate;
  onDecide: (decision: "approve" | "reject" | "defer") => void;
  pending: boolean;
}) {
  const candidate = record.candidate;
  return (
    <article className="rounded-lg border bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">{candidateTitle(record)}</p><p className="mt-1 text-xs text-muted-ink">{record.kind} · {candidate.type} · confidence {Math.round(candidate.confidence * 100)}%</p></div><StatusBadge status={candidate.status} /></div>
      <p className="mt-3 text-sm leading-6 text-slate-700">{candidate.summary}</p>
      <blockquote className="mt-3 rounded-md border-l-2 border-brand bg-brand-soft/40 px-3 py-2 text-xs leading-5 text-slate-700">“{candidate.evidence.quote}”<footer className="mt-1 font-mono text-[10px] text-muted-ink">{candidate.evidence.paper_id} · p. {candidate.evidence.page_start}{candidate.evidence.page_end !== candidate.evidence.page_start ? `–${candidate.evidence.page_end}` : ""}</footer></blockquote>
      <div className="mt-3 flex flex-wrap justify-end gap-2"><Button disabled={pending} size="sm" variant="outline" onClick={() => onDecide("defer")}><PauseCircle size={14} /> Defer</Button><Button disabled={pending} size="sm" variant="outline" onClick={() => onDecide("reject")}><X size={14} /> Reject</Button><Button disabled={pending} size="sm" onClick={() => onDecide("approve")}><Check size={14} /> Approve</Button></div>
    </article>
  );
}

export function CandidateReviewPanel() {
  const [ingestionId, setIngestionId] = useState("");
  const client = useQueryClient();
  const ingestions = useQuery({ queryKey: ["knowledge-ingestions"], queryFn: api.ingestions });
  const selectedId = ingestionId || ingestions.data?.[0]?.id || "";
  const candidates = useQuery({
    queryKey: ["knowledge-candidates", selectedId, "draft"],
    queryFn: () => api.candidates(selectedId),
    enabled: Boolean(selectedId),
  });
  const decide = useMutation({
    mutationFn: ({ candidateId, decision }: { candidateId: string; decision: "approve" | "reject" | "defer" }) => api.decideCandidate(candidateId, decision),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["knowledge-candidates", selectedId] });
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
    },
  });
  const selectedIngestion = useMemo(
    () => ingestions.data?.find((item) => item.id === selectedId),
    [ingestions.data, selectedId],
  );
  return (
    <Card>
      <CardHeader>
        <div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Knowledge</p><h2 className="mt-1 text-lg font-semibold">Candidate review</h2></div>
        <FlaskConical className="text-brand" size={20} />
      </CardHeader>
      <CardContent>
        <p className="text-sm leading-6 text-muted-ink">候选记录始终保持在 ingestion 范围内。审核不会写入 Project Memory 或越过发布治理。</p>
        {ingestions.isPending ? <LoadingBlock label="读取 ingestions…" /> : null}
        {ingestions.error instanceof Error ? <ErrorBlock error={ingestions.error} onRetry={() => ingestions.refetch()} /> : null}
        {ingestions.data?.length ? <label className="mt-4 block text-sm font-medium">Ingestion<select className="mt-1.5 w-full rounded-lg border bg-white px-3 py-2 text-sm" value={selectedId} onChange={(event) => setIngestionId(event.target.value)}>{ingestions.data.map((ingestion) => <option key={ingestion.id} value={ingestion.id}>{ingestion.collection} · {ingestion.topic} · {ingestion.status}</option>)}</select></label> : null}
        {selectedIngestion ? <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-ink"><Badge tone="neutral">{selectedIngestion.collection_slug}</Badge><span>{selectedIngestion.candidate_count} candidates</span><span>updated {formatDate(selectedIngestion.updated_at)}</span></div> : null}
        {selectedId && candidates.isPending ? <div className="mt-5"><LoadingBlock label="读取 draft candidates…" /></div> : null}
        {candidates.error instanceof Error ? <div className="mt-5"><ErrorBlock error={candidates.error} onRetry={() => candidates.refetch()} /></div> : null}
        <div className="mt-5 space-y-3">{candidates.data?.map((record) => <CandidateCard key={record.candidate.id} pending={decide.isPending} record={record} onDecide={(decision) => decide.mutate({ candidateId: record.candidate.id, decision })} />)}</div>
        {decide.error instanceof Error ? <div className="mt-4"><ErrorBlock error={decide.error} /></div> : null}
        {ingestions.data && !ingestions.data.length ? <EmptyBlock title="没有 Knowledge ingestion">先从 Knowledge Core 创建 ingestion，审核队列才会出现候选。</EmptyBlock> : null}
        {selectedId && !candidates.isPending && candidates.data && !candidates.data.length ? <EmptyBlock title="没有 draft candidate">当前 ingestion 的候选已经审核完成，或仍在等待抽取。</EmptyBlock> : null}
        <p className="mt-4 flex items-center gap-1 text-xs text-muted-ink"><ChevronRight size={13} />需要 merge/link 的候选继续使用现有 Knowledge Core 审核流程，以明确选择 canonical entity。</p>
      </CardContent>
    </Card>
  );
}
