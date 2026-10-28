import { FileText, FolderKanban, MessageSquarePlus, Zap } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { Button, buttonVariants } from "../components/ui/button";
import { Page, PageHeader, Segmented } from "../components/ui/page";
import { useReportLibrary, type LibraryItem } from "../lib/hooks";
import { cn, formatDate } from "../lib/utils";

export function ReportRow({ item, compact }: { item: LibraryItem; compact?: boolean }) {
  const Icon = item.kind === "quick" ? Zap : FolderKanban;
  return (
    <Link
      className={cn("group flex min-w-0 items-start gap-3 rounded-lg px-3 transition-colors hover:bg-subtle", compact ? "py-2.5" : "py-3.5")}
      to={item.href}
    >
      <span className={cn(
        "mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg",
        item.kind === "quick" ? "bg-info-soft text-sky-700" : "bg-brand-soft text-brand",
      )}><Icon size={15} /></span>
      <span className="min-w-0 flex-1">
        <span className={cn("font-medium leading-6 text-ink group-hover:text-brand-strong", compact ? "line-clamp-1 text-[13.5px]" : "line-clamp-2")}>{item.title}</span>
        <span className="mt-0.5 block truncate text-xs text-muted-ink">{item.kind === "quick" ? "快速报告" : "项目报告"} · {item.context} · {formatDate(item.updatedAt)}</span>
      </span>
      {item.status !== "ready" && item.status !== "completed" ? <StatusBadge className="mt-1" status={item.status} /> : null}
    </Link>
  );
}

export function ReportsPage() {
  const [params] = useSearchParams();
  const legacyId = params.get("report");
  const library = useReportLibrary();
  const [filter, setFilter] = useState<"all" | "quick" | "project">("all");
  const visible = useMemo(() => library.items.filter((item) => filter === "all" || item.kind === filter), [filter, library.items]);
  if (legacyId) return <Navigate replace to={`/reports/${encodeURIComponent(legacyId)}`} />;
  const quickCount = library.quickTotal;
  const projectCount = library.items.filter((item) => item.kind === "project").length;
  return (
    <Page>
      <PageHeader
        title="报告"
        description="所有研究报告都在这里：快速报告直接基于已发布知识生成，项目报告来自项目中冻结上下文的研究运行。打开报告可逐条查看引用原文。"
        actions={<Link className={buttonVariants()} to="/"><MessageSquarePlus size={16} />提出新问题</Link>}
      />
      {library.isPending ? <LoadingBlock label="读取报告…" /> : null}
      {library.error ? <ErrorBlock error={library.error} onRetry={library.refetch} /> : null}
      {!library.isPending && !library.error ? (
        <>
          <Segmented
            label="报告类型"
            onChange={setFilter}
            options={[
              { value: "all", label: "全部", count: quickCount + projectCount },
              { value: "quick", label: "快速报告", count: quickCount },
              { value: "project", label: "项目报告", count: projectCount },
            ]}
            value={filter}
          />
          <div className="mt-4 rounded-xl border bg-panel p-1.5 shadow-[var(--shadow-card)]" data-testid="report-library">
            {visible.length ? (
              <div className="divide-y divide-line/70">{visible.map((item) => <ReportRow item={item} key={`${item.kind}:${item.id}`} />)}</div>
            ) : (
              <EmptyBlock
                className="border-0"
                icon={<FileText size={19} />}
                title={library.items.length ? "此分类下没有报告" : "还没有报告"}
                action={<Link className={buttonVariants({ variant: "outline", size: "sm" })} to="/">去首页提问</Link>}
              >
                在首页输入研究问题，系统会基于已审核的知识生成带引用的报告。
              </EmptyBlock>
            )}
          </div>
          {filter !== "project" && library.hasNextPage ? <Button className="mt-4" disabled={library.isFetchingNextPage} variant="outline" onClick={() => void library.fetchNextPage()}>{library.isFetchingNextPage ? "加载中…" : "加载更多报告"}</Button> : null}
        </>
      ) : null}
    </Page>
  );
}
