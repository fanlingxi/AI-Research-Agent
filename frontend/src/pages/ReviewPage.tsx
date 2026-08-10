import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ClipboardCheck, DatabaseZap, X } from "lucide-react";
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import { api, type MemoryProposal } from "../lib/api";
import { formatDate } from "../lib/utils";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { CandidateReviewPanel } from "../components/CandidateReviewPanel";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../components/ui/dialog";

function ProposalDialog({ proposal, onClose }: { proposal: MemoryProposal; onClose: () => void }) {
  const client = useQueryClient();
  const approve = useMutation({ mutationFn: () => api.reviewProposal(proposal.id, proposal.revision, "approved"), onSuccess: () => void client.invalidateQueries({ queryKey: ["proposals"] }) });
  const reject = useMutation({ mutationFn: () => api.reviewProposal(proposal.id, proposal.revision, "rejected"), onSuccess: () => void client.invalidateQueries({ queryKey: ["proposals"] }) });
  const commit = useMutation({ mutationFn: () => api.commitProposal(proposal.id), onSuccess: () => { void client.invalidateQueries({ queryKey: ["proposals"] }); void client.invalidateQueries({ queryKey: ["proposal", proposal.id] }); } });
  const current = commit.data?.proposal ?? approve.data ?? reject.data ?? proposal;
  const error = approve.error ?? reject.error ?? commit.error;
  return <Dialog open onOpenChange={(open) => !open && onClose()}><DialogContent><DialogTitle>Review MemoryProposal</DialogTitle><DialogDescription>Proposal 必须经历 proposed → approved / rejected → committed；Agent 不会自动 commit。</DialogDescription><div className="mt-5 space-y-4"><div className="flex items-center justify-between rounded-lg border bg-white p-3"><div><p className="font-medium">{current.proposal_type}</p><p className="mt-1 text-xs text-muted-ink">revision {current.revision} · {formatDate(current.updated_at)}</p></div><StatusBadge status={current.status} /></div><section><p className="text-sm font-medium">Rationale</p><p className="mt-1 rounded-lg bg-slate-50 p-3 text-sm leading-6 text-slate-600">{current.rationale || "No rationale provided."}</p></section><section><p className="text-sm font-medium">Proposed payload</p><pre className="mt-1 max-h-56 overflow-auto rounded-lg bg-slate-950 p-3 text-xs leading-5 text-slate-100">{JSON.stringify(current.payload, null, 2)}</pre></section>{error instanceof Error ? <ErrorBlock error={error} /> : null}<div className="flex flex-wrap justify-end gap-2">{current.status === "proposed" ? <><Button variant="outline" disabled={reject.isPending} onClick={() => reject.mutate()}><X size={15} /> Reject</Button><Button disabled={approve.isPending} onClick={() => approve.mutate()}><Check size={15} /> Approve</Button></> : null}{current.status === "approved" ? <Button disabled={commit.isPending} onClick={() => commit.mutate()}><DatabaseZap size={15} /> Confirm commit</Button> : null}</div></div></DialogContent></Dialog>;
}

export function ReviewPage() {
  const [params, setParams] = useSearchParams();
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const proposalQueries = useQueries({ queries: (projects.data ?? []).map((project) => ({ queryKey: ["proposals", project.id], queryFn: () => api.proposals(project.id) })) });
  const proposals = useMemo(() => proposalQueries.flatMap((query) => query.data ?? []).filter((proposal) => ["proposed", "approved"].includes(proposal.status)), [proposalQueries]);
  const selectedId = params.get("proposal");
  const selectedProposal = useQuery({ queryKey: ["proposal", selectedId], queryFn: () => api.proposal(selectedId!), enabled: Boolean(selectedId) });
  const selected = selectedProposal.data ?? proposals.find((proposal) => proposal.id === selectedId);
  if (projects.isPending) return <div className="p-6 lg:p-8"><LoadingBlock label="读取审核队列…" /></div>;
  if (projects.error instanceof Error) return <div className="p-6 lg:p-8"><ErrorBlock error={projects.error} /></div>;
  return <div className="mx-auto max-w-6xl p-6 lg:p-8"><header className="flex flex-wrap items-end justify-between gap-4"><div><p className="text-sm font-medium text-brand">Human governance</p><h1 className="mt-1 text-3xl font-semibold tracking-tight">Review queue</h1><p className="mt-2 text-sm text-muted-ink">正式 Memory 只能由人审核和提交；Knowledge Candidate 保持 ingestion-scoped。</p></div><ClipboardCheck className="text-brand" size={28} /></header>{selectedProposal.error instanceof Error ? <div className="mt-6"><ErrorBlock error={selectedProposal.error} /></div> : null}<section className="mt-8 grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(340px,0.85fr)]"><Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Memory</p><h2 className="mt-1 text-lg font-semibold">MemoryProposal queue</h2></div><span className="text-sm text-muted-ink">{proposals.length}</span></CardHeader><CardContent className="space-y-3">{proposalQueries.some((query) => query.isPending) ? <LoadingBlock label="聚合项目 Proposal…" /> : null}{proposals.map((proposal) => <button className="w-full rounded-lg border bg-white p-4 text-left hover:border-brand/40" key={proposal.id} onClick={() => setParams({ proposal: proposal.id })}><div className="flex justify-between gap-3"><div><p className="font-medium">{proposal.proposal_type}</p><p className="mt-1 line-clamp-2 text-sm text-muted-ink">{proposal.rationale || "No rationale provided."}</p></div><StatusBadge status={proposal.status} /></div><p className="mt-3 text-xs text-muted-ink">{proposal.project_id} · {formatDate(proposal.updated_at)}</p></button>)}{!proposalQueries.some((query) => query.isPending) && !proposals.length ? <EmptyBlock title="审核队列为空">当 Agent 或用户创建 MemoryProposal 后，它会在这里等待人工审核。</EmptyBlock> : null}</CardContent></Card><CandidateReviewPanel /></section>{selected ? <ProposalDialog proposal={selected} onClose={() => setParams({})} /> : null}</div>;
}
