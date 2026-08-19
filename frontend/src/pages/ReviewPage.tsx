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
  const proposalsLoading = proposalQueries.some((query) => query.isPending);
  if (projects.isPending) return <div className="p-6 lg:p-8"><LoadingBlock label="读取审核队列…" /></div>;
  if (projects.error instanceof Error) return <div className="p-6 lg:p-8"><ErrorBlock error={projects.error} /></div>;
  const proposalQueue = <Card><CardHeader><div><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">项目记忆</p><h2 className="mt-1 text-lg font-semibold">记忆提案</h2></div><span className="text-sm text-muted-ink">{proposals.length}</span></CardHeader><CardContent className="space-y-3">{proposalsLoading ? <LoadingBlock label="聚合项目提案…" /> : null}{proposals.map((proposal) => <button className="w-full rounded-lg border bg-white p-4 text-left hover:border-brand/40" key={proposal.id} onClick={() => setParams({ proposal: proposal.id })}><div className="flex justify-between gap-3"><div><p className="font-medium">{proposal.proposal_type}</p><p className="mt-1 line-clamp-2 text-sm text-muted-ink">{proposal.rationale || "暂无审核说明。"}</p></div><StatusBadge status={proposal.status} /></div><p className="mt-3 text-xs text-muted-ink">{proposal.project_id} · {formatDate(proposal.updated_at)}</p></button>)}</CardContent></Card>;
  return <div className="mx-auto max-w-6xl p-6 lg:p-8"><header className="flex flex-wrap items-end justify-between gap-4"><div><p className="text-sm font-medium text-brand">人工治理</p><h1 className="mt-1 text-3xl font-semibold tracking-tight">审核中心</h1><p className="mt-2 text-sm text-muted-ink">正式项目记忆只能由人审核和提交；知识候选始终限制在本次入库范围内。</p></div><ClipboardCheck className="text-brand" size={28} /></header>{selectedProposal.error instanceof Error ? <div className="mt-6"><ErrorBlock error={selectedProposal.error} /></div> : null}<section className="mt-8">{proposalsLoading || proposals.length ? <div className="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]" data-testid="review-split-layout">{proposalQueue}<CandidateReviewPanel /></div> : <div className="space-y-4"><div className="flex items-center justify-between gap-4 rounded-xl border border-dashed bg-panel px-4 py-3 text-sm" data-testid="memory-proposal-empty-banner" role="status"><div><p className="font-medium">当前没有待审核的记忆提案</p><p className="mt-1 text-xs text-muted-ink">新的 Agent 提案会显示在这里；现在可以集中处理知识候选。</p></div><span className="shrink-0 text-xs text-muted-ink">0 条</span></div><CandidateReviewPanel /></div>}</section>{selected ? <ProposalDialog proposal={selected} onClose={() => setParams({})} /> : null}</div>;
}
