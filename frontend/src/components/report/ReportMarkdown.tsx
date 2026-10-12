import { useMemo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "../../lib/utils";

/** Matches both citation styles: project reports use [cite:evidence-id], quick reports use [E3]. */
const CITATION_PATTERN = /\[cite:([A-Za-z0-9._:-]+)\]|\[(E\d+)\]/g;
const CITE_PREFIX = "#cite-";

/** Citation ids in order of first appearance. Index + 1 is the number shown to the reader. */
export function extractCitations(markdown: string) {
  const ids: string[] = [];
  for (const match of markdown.matchAll(CITATION_PATTERN)) {
    const id = match[1] ?? match[2];
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** Splits a leading "# Title" from the body so the page header can show it once. */
export function splitTitle(markdown: string) {
  const match = markdown.match(/^\s*#\s+(.+?)\s*\n/);
  if (!match) return { title: null as string | null, body: markdown };
  return { title: match[1].trim(), body: markdown.slice(match[0].length) };
}

export function ReportMarkdown({
  markdown,
  onCite,
  activeId,
  className,
}: {
  markdown: string;
  onCite: (id: string) => void;
  activeId?: string | null;
  className?: string;
}) {
  const citations = useMemo(() => extractCitations(markdown), [markdown]);
  const prepared = useMemo(() => {
    const numbers = new Map(citations.map((id, index) => [id, index + 1]));
    return markdown.replace(CITATION_PATTERN, (_match, citeId: string | undefined, shortId: string | undefined) => {
      const id = citeId ?? shortId ?? "";
      return `[${numbers.get(id) ?? "?"}](${CITE_PREFIX}${encodeURIComponent(id)})`;
    });
  }, [citations, markdown]);
  const components = useMemo<Components>(() => ({
    a({ href, children, ...props }) {
      if (href?.startsWith(CITE_PREFIX)) {
        const id = decodeURIComponent(href.slice(CITE_PREFIX.length));
        return <CitationChip active={activeId === id} id={id} onClick={() => onCite(id)}>{children}</CitationChip>;
      }
      return <a href={href} rel="noreferrer" target="_blank" {...props}>{children}</a>;
    },
    table({ children }) {
      return <div className="table-scroll"><table>{children}</table></div>;
    },
  }), [activeId, onCite]);
  return (
    <div className={cn("prose-report", className)}>
      <ReactMarkdown components={components} remarkPlugins={[remarkGfm]}>{prepared}</ReactMarkdown>
    </div>
  );
}

export function CitationChip({ id, children, onClick, active }: { id: string; children: ReactNode; onClick: () => void; active?: boolean }) {
  return (
    <button
      aria-label={`查看引用 ${String(children)} 的原文`}
      className={cn(
        "relative -top-[0.35em] mx-[1px] inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-[5px] px-1 align-baseline text-[11px] font-semibold leading-none tabular-nums no-underline transition-colors",
        active ? "bg-brand text-on-brand" : "bg-brand-soft text-brand-strong hover:bg-brand hover:text-on-brand",
      )}
      data-citation={id}
      onClick={onClick}
      type="button"
    >
      {children}
    </button>
  );
}
