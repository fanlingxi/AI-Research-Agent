import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, ChevronLeft, ChevronRight, Copy, ExternalLink, FileText, Quote } from "lucide-react";
import { useState, type ReactNode } from "react";

import { api } from "../../lib/api";
import { documentTitle, fileName, usePaperTitles } from "../../lib/hooks";
import { cn } from "../../lib/utils";
import { ErrorBlock, LoadingBlock } from "../AsyncState";
import { Button } from "../ui/button";
import { SheetContent } from "../ui/dialog";

export function pageLabel(start?: number | null, end?: number | null) {
  if (start == null) return "页码未知";
  return end != null && end !== start ? `第 ${start}–${end} 页` : `第 ${start} 页`;
}

export interface SourceView {
  title: string;
  page: string;
  quote?: string | null;
  claim?: string | null;
  passage?: string | null;
  uri?: string | null;
  /** First cited page, used to open a local PDF at the right place. */
  pageNumber?: number | null;
  meta?: ReactNode;
}

/** Side panel that shows where a cited sentence comes from. */
export function SourceSheet({
  open,
  onOpenChange,
  number,
  total,
  onStep,
  loading,
  error,
  source,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  number: number;
  total: number;
  onStep?: (delta: number) => void;
  loading?: boolean;
  error?: Error | null;
  source?: SourceView;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <SheetContent label="引用来源">
        <div className="sticky top-0 z-10 flex items-center gap-2 border-b bg-panel/95 px-5 py-3.5 pr-14 backdrop-blur">
          <span className="grid size-7 place-items-center rounded-md bg-brand text-[13px] font-semibold text-on-brand tabular-nums">{number}</span>
          <DialogPrimitive.Title className="text-[15px] font-semibold">引用来源</DialogPrimitive.Title>
          {onStep && total > 1 ? (
            <div className="ml-auto flex items-center gap-1">
              <Button aria-label="上一条引用" disabled={number <= 1} size="icon-sm" variant="ghost" onClick={() => onStep(-1)}><ChevronLeft size={16} /></Button>
              <span className="text-xs tabular-nums text-muted-ink">{number}/{total}</span>
              <Button aria-label="下一条引用" disabled={number >= total} size="icon-sm" variant="ghost" onClick={() => onStep(1)}><ChevronRight size={16} /></Button>
            </div>
          ) : null}
        </div>
        <div className="space-y-6 px-5 py-5">
          {loading ? <LoadingBlock label="读取原文定位…" /> : null}
          {error ? <ErrorBlock error={error} /> : null}
          {source ? (
            <>
              <div className="flex gap-3">
                <span className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg bg-subtle text-muted-ink"><FileText size={17} /></span>
                <div className="min-w-0">
                  <p className="font-medium leading-6 text-ink">{source.title}</p>
                  <p className="mt-0.5 text-[13px] text-muted-ink">{source.page}</p>
                </div>
              </div>
              {source.quote ? (
                <section>
                  <h3 className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-ink"><Quote size={13} />引用的原文</h3>
                  <blockquote className="rounded-lg border-l-[3px] border-brand bg-brand-tint px-4 py-3 text-[15px] leading-7 text-ink">{source.quote}</blockquote>
                </section>
              ) : null}
              {source.claim ? (
                <section>
                  <h3 className="mb-1.5 text-xs font-medium text-muted-ink">这段原文支持的知识主张</h3>
                  <p className="text-sm leading-6 text-ink-soft">{source.claim}</p>
                </section>
              ) : null}
              {source.passage ? <Passage text={source.passage} quoted={Boolean(source.quote)} /> : null}
              {source.uri ? <SourceLocation uri={source.uri} page={source.pageNumber} /> : null}
              {source.meta ? <div className="border-t pt-4 text-xs leading-5 text-muted-ink">{source.meta}</div> : null}
            </>
          ) : null}
        </div>
      </SheetContent>
    </DialogPrimitive.Root>
  );
}

function Passage({ text, quoted }: { text: string; quoted: boolean }) {
  const [expanded, setExpanded] = useState(!quoted);
  const long = text.length > 600;
  return (
    <section>
      <h3 className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-ink"><BookOpen size={13} />{quoted ? "所在段落" : "原文片段"}</h3>
      <p className={cn("whitespace-pre-wrap break-words rounded-lg border bg-subtle/50 p-3.5 text-[13.5px] leading-7 text-ink-soft", long && !expanded && "line-clamp-6")}>{text}</p>
      {long ? <Button className="mt-1.5" size="sm" variant="link" onClick={() => setExpanded(!expanded)}>{expanded ? "收起" : "展开全文"}</Button> : null}
    </section>
  );
}

function SourceLocation({ uri, page }: { uri: string; page?: number | null }) {
  const [copied, setCopied] = useState(false);
  const isWeb = /^https?:\/\//i.test(uri);
  return (
    <section className="rounded-lg border p-3.5 text-[13px]">
      <p className="text-xs font-medium text-muted-ink">原始文件</p>
      <p className="mt-1 break-all text-ink-soft">{fileName(uri)}</p>
      <div className="mt-2.5 flex flex-wrap gap-2">
        {isWeb ? (
          <a className="inline-flex items-center gap-1 text-[13px] font-medium text-brand hover:underline" href={uri} rel="noreferrer" target="_blank">打开原文 <ExternalLink size={13} /></a>
        ) : (
          <>
            {/\.pdf$/i.test(uri) ? (
              <a
                className="inline-flex items-center gap-1 text-[13px] font-medium text-brand hover:underline"
                href={`/api/knowledge/source-file?uri=${encodeURIComponent(uri)}${page ? `#page=${page}` : ""}`}
                rel="noreferrer"
                target="_blank"
              >
                打开原文 PDF{page ? `（第 ${page} 页）` : ""} <ExternalLink size={13} />
              </a>
            ) : null}
            <Button size="sm" variant="outline" onClick={() => { void navigator.clipboard?.writeText(uri).then(() => setCopied(true)); }}>
              <Copy size={13} />{copied ? "已复制本地路径" : "复制本地路径"}
            </Button>
          </>
        )}
      </div>
    </section>
  );
}

/** Citation drawer for project reports: resolves evidence inside the run's frozen snapshot. */
export function SnapshotSourceSheet({
  snapshotId,
  evidenceId,
  citations,
  onSelect,
}: {
  snapshotId: string;
  evidenceId: string | null;
  citations: string[];
  onSelect: (id: string | null) => void;
}) {
  const titles = usePaperTitles();
  const evidence = useQuery({
    queryKey: ["evidence", snapshotId, evidenceId],
    queryFn: () => api.evidence(snapshotId, evidenceId!),
    enabled: Boolean(evidenceId),
  });
  const index = evidenceId ? citations.indexOf(evidenceId) : -1;
  const data = evidence.data;
  return (
    <SourceSheet
      error={evidence.error instanceof Error ? evidence.error : null}
      loading={Boolean(evidenceId) && evidence.isPending}
      number={index + 1 || 1}
      open={Boolean(evidenceId)}
      total={citations.length}
      onOpenChange={(open) => !open && onSelect(null)}
      onStep={index >= 0 ? (delta) => onSelect(citations[index + delta] ?? evidenceId) : undefined}
      source={data ? {
        title: documentTitle(data.document.title, data.document.id, titles, data.source.uri),
        page: pageLabel(data.chunk.page_start, data.chunk.page_end),
        pageNumber: data.chunk.page_start,
        quote: data.evidence.quote,
        claim: data.claim.statement,
        uri: data.source.uri,
        meta: <>此引用来自本次运行冻结的上下文快照，内容不会随知识库后续变化而改变。解析器 {data.document.parser_version}。</>,
      } : undefined}
    />
  );
}
