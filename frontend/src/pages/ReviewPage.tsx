import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, DatabaseZap, Lightbulb, X } from "lucide-react";
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import { api, type MemoryProposal } from "../lib/api";
import { usePendingCandidates } from "../lib/hooks";
import { formatDate } from "../lib/utils";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { CandidateReviewPanel } from "../components/CandidateReviewPanel";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../components/ui/dialog";
import { Disclosure, Page, PageHeader, Segmented } from "../components/ui/page";

const PROPOSAL_TYPES: Record<string, string> = {
  decision_create: "新增项目决策",
  decision_update: "更新项目决策",
};

const PAYLOAD_LABELS: Record<string, string> = {
  summary: "决策摘要",
  rationale: "理由",
  impact: "影响",
  title: "标题",
  status: "状态",
};

function proposalTitle(proposal: MemoryProposal) {
  const summary = proposal.payload.summary;
  return typeof summary === "string" && summary.trim() ? summary : PROPOSAL_TYPES[proposal.proposal_type] ?? proposal.proposal_type;
}

function ProposalDialog({ proposal, onClose }: { proposal: MemoryProposal; onClose: () => void }) {
  const client = useQueryClient();
  const refresh = () => { void client.invalidateQueries({ queryKey: ["proposals"] }); void client.invalidateQueries({ queryKey: ["proposal", proposal.id] }); void client.invalidateQueries({ queryKey: ["dashboard"] }); };
  const approve = useMutation({ mutationFn: () => api.reviewProposal(proposal.id, proposal.revision, "approved"), onSuccess: refresh });
  const reject = useMutation({ mutationFn: () => api.reviewProposal(proposal.id, proposal.revision, "rejected"), onSuccess: refresh });
  const commit = useMutation({ mutationFn: () => api.commitProposal(proposal.id), onSuccess: refresh });
  const current = commit.data?.proposal ?? approve.data ?? reject.data ?? proposal;
  const error = approve.error ?? reject.error ?? commit.error;
  const readable = Object.entries(current.payload).filter(([key, value]) => PAYLOAD_LABELS[key] && typeof value === "string" && value.trim());
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogTitle>审核记忆提案</DialogTitle>
        <DialogDescription>研究智能体只能提出建议。先批准或驳回；批准后再确认写入，才会成为项目记忆。</DialogDescription>
        <div className="mt-5 space-y-4">
          <div className="flex items-start justify-between gap-3 rounded-lg border p-3">
            <div className="min-w-0"><p className="font-medium">{PROPOSAL_TYPES[current.proposal_type] ?? current.proposal_type}</p><p className="mt-0.5 text-xs text-muted-ink">{formatDate(current.updated_at)}</p></div>
            <StatusBadge status={current.status} />
          </div>
          {readable.length ? (
            <dl className="space-y-3">
              {readable.map(([key, value]) => <div key={key}><dt className="text-xs font-medium text-muted-ink">{PAYLOAD_LABELS[key]}</dt><dd className="mt-0.5 whitespace-pre-wrap text-sm leading-6">{String(value)}</dd></div>)}
            </dl>
          ) : null}
          <section><p className="text-xs font-medium text-muted-ink">提出理由</p><p className="mt-1 rounded-lg bg-subtle p-3 text-sm leading-6 text-ink-soft">{current.rationale || "未提供理由。"}</p></section>
          <Disclosure summary="完整内容（JSON）"><pre className="max-h-56 overflow-auto text-xs leading-5 text-ink-soft">{JSON.stringify(current.payload, null, 2)}</pre></Disclosure>
          {error instanceof Error ? <ErrorBlock error={error} /> : null}
          <div className="flex flex-wrap justify-end gap-2">
            {current.status === "proposed" ? <><Button variant="danger-outline" disabled={reject.isPending} onClick={() => reject.mutate()}><X size={15} />驳回</Button><Button disabled={approve.isPending} onClick={() => approve.mutate()}><Check size={15} />批准</Button></> : null}
            {current.status === "approved" ? <Button disabled={commit.isPending} onClick={() => commit.mutate()}><DatabaseZap size={15} />确认写入项目记忆</Button> : null}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function ReviewPage() {
  const [params, setParams] = useSearchParams();
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const pending = usePendingCandidates();
  const proposalQueries = useQueries({ queries: (projects.data ?? []).map((project) => ({ queryKey: ["proposals", project.id], queryFn: () => api.proposals(project.id) })) });
  const proposals = useMemo(() => proposalQueries.flatMap((query) => query.data ?? []).filter((proposal) => ["proposed", "approved"].includes(proposal.status)), [proposalQueries]);
  const projectNames = new Map((projects.data ?? []).map((project) => [project.id, project.name]));
  const selectedId = params.get("proposal");
  const selectedProposal = useQuery({ queryKey: ["proposal", selectedId], queryFn: () => api.proposal(selectedId!), enabled: Boolean(selectedId) });
  const selected = selectedProposal.data ?? proposals.find((proposal) => proposal.id === selectedId);
  const proposalsLoading = proposalQueries.some((query) => query.isPending);
  const tab = params.get("tab") === "memory" ? "memory" : "candidates";
  const setTab = (value: "candidates" | "memory") => setParams((current) => {
    const next = new URLSearchParams(current);
    if (value === "memory") next.set("tab", "memory"); else next.delete("tab");
    return next;
  });
  const closeProposal = () => setParams((current) => { const next = new URLSearchParams(current); next.delete("proposal"); return next; });
  return (
    <Page>
      <PageHeader
        title="审核"
        description="系统从资料中抽取的知识、研究智能体提出的项目记忆，都需要你确认后才会生效。未经批准的内容不会用于研究。"
      />
      <Segmented
        className="mb-5"
        label="审核类型"
        value={tab}
        onChange={setTab}
        options={[
          { value: "candidates", label: "知识候选", count: pending.isPending ? undefined : pending.total },
          { value: "memory", label: "记忆提案", count: proposalsLoading ? undefined : proposals.length },
        ]}
      />
      {projects.error instanceof Error ? <div className="mb-4"><ErrorBlock error={projects.error} /></div> : null}
      {selectedProposal.error instanceof Error ? <div className="mb-4"><ErrorBlock error={selectedProposal.error} /></div> : null}
      {tab === "candidates" ? (
        <CandidateReviewPanel collectionSlug={params.get("collection")} initialIngestionId={params.get("ingestion")} />
      ) : (
        <section aria-label="记忆提案" className="space-y-3">
          {projects.isPending || proposalsLoading ? <LoadingBlock label="读取记忆提案…" /> : null}
          {proposals.map((proposal) => (
            <button className="flex w-full min-w-0 items-start gap-3 rounded-xl border bg-panel p-4 text-left shadow-[var(--shadow-card)] hover:border-brand/40" key={proposal.id} onClick={() => setParams((current) => { const next = new URLSearchParams(current); next.set("proposal", proposal.id); return next; })}>
              <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-warning-soft text-amber-800"><Lightbulb size={16} /></span>
              <span className="min-w-0 flex-1">
                <span className="block font-medium leading-6">{proposalTitle(proposal)}</span>
                <span className="mt-0.5 line-clamp-2 text-[13px] text-muted-ink">{proposal.rationale || "未提供理由。"}</span>
                <span className="mt-1.5 block text-xs text-muted-ink">{projectNames.get(proposal.project_id) ?? "项目"} · {formatDate(proposal.updated_at)}</span>
              </span>
              <StatusBadge status={proposal.status} />
            </button>
          ))}
          {!projects.isPending && !proposalsLoading && !proposals.length ? (
            <EmptyBlock icon={<Lightbulb size={19} />} title="当前没有待审核的记忆提案">在项目研究中允许智能体提出项目记忆后，它的建议会出现在这里。</EmptyBlock>
          ) : null}
        </section>
      )}
      {selected ? <ProposalDialog proposal={selected} onClose={closeProposal} /> : null}
    </Page>
  );
}
