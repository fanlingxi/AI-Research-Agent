import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, BookOpen, ExternalLink, LocateFixed, Quote, X } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link } from "react-router-dom";

import { ErrorBlock } from "../../components/AsyncState";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { knowledgeMapApi, type MapEvidence, type MapNodeDetail, type MapNodeRef } from "./api";
import { relationLabel, typeSwatch, typeLabel } from "./palette";

interface NodeDetailPanelProps {
  nodeId: string;
  includePending: boolean;
  onSelect: (nodeId: string) => void;
  onLocate: (nodeId: string) => void;
  onClose: () => void;
}

function TypeDot({ type, hollow = false }: { type: string; hollow?: boolean }) {
  const color = typeSwatch(type);
  return (
    <span
      aria-hidden
      className="inline-block size-2.5 shrink-0 rounded-full"
      style={hollow ? { boxShadow: `inset 0 0 0 2px ${color}`, background: "var(--color-panel)" } : { background: color }}
    />
  );
}

function pageLabel(evidence: MapEvidence) {
  if (!evidence.page_start) return null;
  return evidence.page_end && evidence.page_end !== evidence.page_start
    ? `第 ${evidence.page_start}–${evidence.page_end} 页`
    : `第 ${evidence.page_start} 页`;
}

function EvidenceQuote({ evidence, onSelect }: { evidence: MapEvidence; onSelect: (nodeId: string) => void }) {
  const [open, setOpen] = useState(false);
  const text = evidence.quote.replace(/\s+/g, " ").trim();
  const long = text.length > 160;
  return (
    <figure className="rounded-lg border-l-2 border-brand/40 bg-subtle px-3 py-2">
      <blockquote className={`text-[13px] leading-6 text-ink-soft ${open ? "" : "line-clamp-3"}`}>{text}</blockquote>
      <figcaption className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-ink">
        {evidence.paper_title ? (
          evidence.paper_node_id ? (
            <button className="inline-flex min-w-0 items-center gap-1 text-left font-medium text-brand hover:underline" onClick={() => onSelect(evidence.paper_node_id!)} type="button">
              <BookOpen className="shrink-0" size={12} />
              <span className="line-clamp-1">《{evidence.paper_title}》</span>
            </button>
          ) : (
            <span className="line-clamp-1">《{evidence.paper_title}》</span>
          )
        ) : null}
        {pageLabel(evidence) ? <span>{pageLabel(evidence)}</span> : null}
        {long ? (
          <button className="ml-auto text-xs text-muted-ink hover:text-ink" onClick={() => setOpen((value) => !value)} type="button">
            {open ? "收起" : "展开全文"}
          </button>
        ) : null}
      </figcaption>
    </figure>
  );
}

function NodeChip({ node, onSelect }: { node: MapNodeRef; onSelect: (nodeId: string) => void }) {
  return (
    <button className="inline-flex min-w-0 items-center gap-1.5 rounded-md text-left text-sm font-medium text-ink hover:text-brand" onClick={() => onSelect(node.id)} type="button">
      <TypeDot hollow={node.status === "pending"} type={node.type} />
      <span className="line-clamp-2">{node.label}</span>
    </button>
  );
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="border-t px-5 py-4">
      <h3 className="flex items-center justify-between text-xs font-semibold tracking-[0.08em] text-muted-ink">
        {title}
        {count !== undefined ? <span className="font-normal">{count}</span> : null}
      </h3>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function DetailBody({ detail, onSelect, onLocate }: { detail: MapNodeDetail; onSelect: (id: string) => void; onLocate: (id: string) => void }) {
  const { node } = detail;
  const pending = node.status === "pending";
  const [showAllRelations, setShowAllRelations] = useState(false);
  const relations = showAllRelations ? detail.relations : detail.relations.slice(0, 12);
  return (
    <>
      <div className="px-5 pb-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 rounded-full border bg-panel px-2 py-0.5 text-xs font-medium text-ink-soft">
            <TypeDot hollow={pending} type={node.type} />
            {typeLabel(node.type)}
          </span>
          {pending ? <Badge tone="warning">待审核 · 非正式知识</Badge> : <Badge tone="success">已审核</Badge>}
        </div>
        <h2 className="mt-2 text-lg font-semibold leading-snug tracking-tight">{node.label}</h2>
        {node.aliases.length ? <p className="mt-1 text-xs text-muted-ink">别名：{node.aliases.join("、")}</p> : null}
        {node.summary ? <p className="mt-3 text-sm leading-6 text-ink-soft">{node.summary}</p> : null}
        <div className="mt-4 flex flex-wrap gap-2">
          <Button onClick={() => onLocate(node.id)} size="sm" variant="outline"><LocateFixed size={14} /> 在图中定位</Button>
          {pending ? (
            <Link to="/review"><Button size="sm" variant="secondary">前往审核</Button></Link>
          ) : null}
        </div>
      </div>

      {detail.papers.length ? (
        <Section count={detail.papers.length} title="支撑论文">
          <ul className="space-y-2">
            {detail.papers.map((paper) => (
              <li className="flex items-start justify-between gap-3" key={paper.document_id}>
                <button className="min-w-0 text-left text-sm font-medium text-ink hover:text-brand" onClick={() => onSelect(paper.node_id)} type="button">
                  <span className="line-clamp-2">《{paper.title}》</span>
                  <span className="mt-0.5 block text-xs font-normal text-muted-ink">
                    {paper.evidence_count} 条证据{paper.pages ? ` · 共 ${paper.pages} 页` : ""}
                  </span>
                </button>
                {paper.url ? (
                  <a aria-label="打开原文" className="shrink-0 text-muted-ink hover:text-brand" href={paper.url} rel="noreferrer" target="_blank"><ExternalLink size={14} /></a>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {detail.relations.length ? (
        <Section count={detail.relations.length} title="关联实体">
          <ul className="space-y-2.5">
            {relations.map((relation) => (
              <li className={`rounded-lg border bg-panel p-2.5 ${relation.status === "pending" ? "border-dashed border-amber-300" : ""}`} key={relation.id}>
                <div className="flex items-center gap-1.5 text-xs text-muted-ink">
                  {relation.direction === "out" ? <ArrowRight size={12} /> : <ArrowLeft size={12} />}
                  <span className="font-medium text-ink-soft">{relationLabel(relation.type)}</span>
                  {relation.status === "pending" ? <span className="rounded bg-amber-100 px-1 text-amber-900">待审核</span> : null}
                </div>
                <div className="mt-1"><NodeChip node={relation.other} onSelect={onSelect} /></div>
                {relation.statement ? <p className="mt-1 text-xs leading-5 text-muted-ink">{relation.statement}</p> : null}
              </li>
            ))}
          </ul>
          {detail.relations.length > 12 ? (
            <button className="mt-2 text-xs font-medium text-brand hover:underline" onClick={() => setShowAllRelations((value) => !value)} type="button">
              {showAllRelations ? "收起" : `查看全部 ${detail.relations.length} 条`}
            </button>
          ) : null}
        </Section>
      ) : null}

      <Section count={detail.claims.length} title="主张与证据">
        {detail.claims.length ? (
          <ul className="space-y-4">
            {detail.claims.map((claim) => (
              <li key={claim.id}>
                <p className="flex gap-2 text-sm leading-6 text-ink">
                  <Quote className="mt-1 shrink-0 text-muted-ink" size={13} />
                  <span>{claim.statement || "（无陈述）"}</span>
                </p>
                <div className="mt-2 space-y-2">
                  {claim.evidence.map((evidence) => <EvidenceQuote evidence={evidence} key={evidence.id} onSelect={onSelect} />)}
                  {!claim.evidence.length ? <p className="text-xs text-muted-ink">暂无可定位的原文证据。</p> : null}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-ink">{node.kind === "source" ? "这是论文来源节点，证据挂在它支撑的实体上，可从“关联实体”进入查看。" : "暂无已发布主张。"}</p>
        )}
      </Section>
    </>
  );
}

export function NodeDetailPanel({ nodeId, includePending, onSelect, onLocate, onClose }: NodeDetailPanelProps) {
  const detail = useQuery({
    queryKey: ["knowledge-map-node", nodeId, includePending],
    queryFn: () => knowledgeMapApi.node(nodeId, includePending),
  });
  return (
    <div className="flex h-full flex-col" data-testid="knowledge-map-detail">
      <div className="flex items-center justify-between px-5 pb-2 pt-4">
        <p className="text-xs font-semibold tracking-[0.1em] text-muted-ink">节点详情</p>
        <button aria-label="关闭详情" className="grid size-7 place-items-center rounded-md text-muted-ink hover:bg-subtle" onClick={onClose} type="button"><X size={16} /></button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-4">
        {detail.isPending ? (
          <div className="space-y-3 px-5 py-2" role="status" aria-label="读取节点详情">
            <div className="h-4 w-24 animate-pulse rounded bg-subtle" />
            <div className="h-6 w-3/4 animate-pulse rounded bg-subtle" />
            <div className="h-16 animate-pulse rounded bg-subtle" />
          </div>
        ) : null}
        {detail.error instanceof Error ? <div className="px-5"><ErrorBlock error={detail.error} onRetry={() => void detail.refetch()} /></div> : null}
        {detail.data ? <DetailBody detail={detail.data} key={detail.data.node.id} onLocate={onLocate} onSelect={onSelect} /> : null}
      </div>
    </div>
  );
}
