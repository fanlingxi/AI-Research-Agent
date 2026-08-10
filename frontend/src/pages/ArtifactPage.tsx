import { useQuery } from "@tanstack/react-query";
import { FileText, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link, useParams } from "react-router-dom";

import { api } from "../lib/api";
import { formatDate, shortId } from "../lib/utils";
import { CitationDrawer } from "../components/CitationDrawer";
import { ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardHeader } from "../components/ui/card";

const CITATION_PATTERN = /\[cite:([A-Za-z0-9._:-]+)\]/g;

export function ArtifactPage() {
  const { artifactId = "" } = useParams();
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const artifact = useQuery({ queryKey: ["artifact", artifactId], queryFn: () => api.artifact(artifactId) });
  const content = useQuery({ queryKey: ["artifact-content", artifactId], queryFn: () => api.artifactContent(artifactId) });
  const markdown = content.data?.rendered_markdown ?? "";
  const citations = useMemo<string[]>(
    () => Array.from(new Set(Array.from(markdown.matchAll(CITATION_PATTERN), (match) => match[1]))),
    [markdown],
  );
  if (artifact.isPending || content.isPending) return <div className="mx-auto max-w-5xl p-6 lg:p-8"><LoadingBlock label="读取 Artifact…" /></div>;
  if (artifact.error instanceof Error || content.error instanceof Error) return <div className="mx-auto max-w-5xl p-6 lg:p-8"><ErrorBlock error={(artifact.error ?? content.error) as Error} /></div>;
  if (!artifact.data || !content.data) return <div className="mx-auto max-w-5xl p-6 lg:p-8"><LoadingBlock label="等待 Artifact 数据…" /></div>;
  const record = artifact.data!;
  const body = content.data!;
  return <div className="mx-auto max-w-5xl p-6 lg:p-8"><header className="border-b pb-6"><div className="flex flex-wrap items-start justify-between gap-4"><div><p className="text-sm font-medium text-brand">Artifact</p><h1 className="mt-1 text-2xl font-semibold tracking-tight">{record.type} · version {record.version}</h1><p className="mt-2 font-mono text-xs text-muted-ink">{record.reference}</p></div><StatusBadge status={record.status} /></div><div className="mt-4 flex flex-wrap gap-2 text-xs text-muted-ink"><Badge tone="neutral">created {formatDate(record.created_at)}</Badge><Badge tone="brand">snapshot {shortId(body.context_snapshot_id)}</Badge><Link className="rounded-full bg-slate-100 px-2 py-0.5 hover:text-brand" to={`/agent-runs/${body.run_id}`}>AgentRun</Link></div></header><section className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_260px]"><Card><CardContent className="prose-workspace"><ReactMarkdown remarkPlugins={[remarkGfm]}>{body.rendered_markdown}</ReactMarkdown></CardContent></Card><aside className="space-y-5"><Card><CardHeader><div className="flex items-center gap-2"><ShieldCheck size={17} className="text-brand" /><h2 className="font-semibold">Citation evidence</h2></div></CardHeader><CardContent className="space-y-2">{citations.map((citation) => <button key={citation} className="w-full rounded-lg border bg-white px-3 py-2 text-left text-xs font-medium text-brand hover:border-brand/40" onClick={() => setEvidenceId(citation)}>[cite:{citation}]</button>)}{!citations.length ? <p className="text-sm text-muted-ink">Artifact 未包含可点击 citation。</p> : null}</CardContent></Card><Card><CardHeader><div className="flex items-center gap-2"><FileText size={17} className="text-brand" /><h2 className="font-semibold">Provenance</h2></div></CardHeader><CardContent className="space-y-2 text-xs text-muted-ink"><p>output {shortId(body.output_id)}</p><p>SHA-256 {shortId(body.output_sha256)}</p><p>Context {shortId(body.context_sha256)}</p></CardContent></Card></aside></section><CitationDrawer evidenceId={evidenceId} snapshotId={body.context_snapshot_id} onOpenChange={(open) => !open && setEvidenceId(null)} /></div>;
}
