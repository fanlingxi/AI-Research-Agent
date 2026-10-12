import { ArrowLeft } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { cn } from "../../lib/utils";
import { Page } from "../ui/page";

/** Shared reading layout for quick reports and project reports. */
export function ReportLayout({
  kind,
  title,
  meta,
  actions,
  notices,
  body,
  rail,
  after,
  backTo = "/reports",
  backLabel = "全部报告",
}: {
  kind: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  notices?: ReactNode;
  body: ReactNode;
  rail?: ReactNode;
  after?: ReactNode;
  backTo?: string;
  backLabel?: string;
}) {
  return (
    <Page width="wide">
      <Link className="inline-flex items-center gap-1.5 text-[13px] text-muted-ink hover:text-ink" to={backTo}><ArrowLeft size={15} />{backLabel}</Link>
      <header className="mt-5 min-w-0 border-b pb-6">
        <div className="flex flex-wrap items-center gap-2">{kind}</div>
        <h1 className={cn("mt-3 max-w-4xl break-words font-semibold leading-[1.45] tracking-tight text-ink", typeof title === "string" && title.length > 60 ? "text-lg sm:text-xl" : "text-[22px] sm:text-[26px]")}>{title}</h1>
        {meta ? <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[13px] text-muted-ink">{meta}</div> : null}
        {actions ? <div className="mt-5 flex flex-wrap gap-2">{actions}</div> : null}
      </header>
      {notices ? <div className="mt-5 space-y-3">{notices}</div> : null}
      <div className="mt-6 grid min-w-0 items-start gap-8 xl:grid-cols-[minmax(0,1fr)_320px]">
        <article className="min-w-0 rounded-xl border bg-panel px-5 py-6 shadow-[var(--shadow-card)] sm:px-10 sm:py-9">
          <div className="mx-auto max-w-[44rem]">{body}</div>
        </article>
        {rail ? <aside className="min-w-0 space-y-5 xl:sticky xl:top-6 xl:max-h-[calc(100vh-3rem)] xl:overflow-y-auto scroll-thin">{rail}</aside> : null}
      </div>
      {after ? <div className="mt-10 min-w-0">{after}</div> : null}
    </Page>
  );
}

export function RailSection({ title, children, action }: { title: ReactNode; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="rounded-xl border bg-panel p-4 shadow-[var(--shadow-card)]">
      <div className="mb-2.5 flex items-center justify-between gap-2">
        <h2 className="text-[13px] font-semibold text-ink">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}
