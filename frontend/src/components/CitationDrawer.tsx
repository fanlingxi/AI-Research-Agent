import { useQuery } from "@tanstack/react-query";
import { ExternalLink, MapPin } from "lucide-react";

import { api } from "../lib/api";
import { ErrorBlock, LoadingBlock } from "./AsyncState";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";

export function CitationDrawer({ snapshotId, evidenceId, onOpenChange }: { snapshotId: string; evidenceId: string | null; onOpenChange: (open: boolean) => void }) {
  const evidence = useQuery({
    queryKey: ["evidence", snapshotId, evidenceId],
    queryFn: () => api.evidence(snapshotId, evidenceId!),
    enabled: Boolean(evidenceId),
  });
  return (
    <Dialog open={Boolean(evidenceId)} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogTitle>Evidence reference</DialogTitle>
        <DialogDescription>仅展示此 ContextSnapshot 已选入的可定位证据。</DialogDescription>
        <div className="mt-5">
          {evidence.isPending ? <LoadingBlock label="读取证据定位…" /> : null}
          {evidence.error instanceof Error ? <ErrorBlock error={evidence.error} /> : null}
          {evidence.data ? (
            <div className="space-y-5 text-sm">
              <blockquote className="rounded-lg border-l-4 border-brand bg-brand-soft/60 px-4 py-3 leading-7 text-slate-700">“{evidence.data.evidence.quote}”</blockquote>
              <section><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-ink">Claim</p><p className="mt-1 leading-6">{evidence.data.claim.statement}</p></section>
              <section className="rounded-lg border bg-white p-4"><div className="flex gap-2"><MapPin size={17} className="mt-0.5 text-brand" /><div><p className="font-medium">{evidence.data.document.title}</p><p className="mt-1 text-xs text-muted-ink">pages {evidence.data.chunk.page_start}–{evidence.data.chunk.page_end} · parser {evidence.data.document.parser_version}</p><a className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline" href={evidence.data.source.uri} rel="noreferrer" target="_blank">Open source <ExternalLink size={13} /></a></div></div></section>
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
