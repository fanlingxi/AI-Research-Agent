import { useQuery } from "@tanstack/react-query";
import { ArrowRight, FileText, Library, ListChecks, Upload } from "lucide-react";
import { Link } from "react-router-dom";

import { EmptyBlock } from "../components/AsyncState";
import { ResearchCommandLauncher } from "../components/ResearchCommandLauncher";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Page } from "../components/ui/page";
import { api } from "../lib/api";
import { usePendingCandidates, useReportLibrary } from "../lib/hooks";
import { ReportRow } from "./ReportsPage";

function MoreLink({ to, children }: { to: string; children: string }) {
  return <Link className="inline-flex shrink-0 items-center gap-1 text-[13px] font-medium text-brand hover:underline" to={to}>{children}<ArrowRight size={13} /></Link>;
}

function RecentReports() {
  const library = useReportLibrary();
  const items = library.items.slice(0, 6);
  return (
    <Card>
      <CardHeader><CardTitle icon={<FileText size={16} />} title="最近的报告" />{library.items.length ? <MoreLink to="/reports">全部报告</MoreLink> : null}</CardHeader>
      <CardContent className="p-2 pt-2 sm:p-3">
        {library.isPending ? <p className="px-3 py-6 text-[13px] text-muted-ink">读取报告…</p> : null}
        {!library.isPending && items.length ? <div className="divide-y divide-line/70">{items.map((item) => <ReportRow compact item={item} key={`${item.kind}:${item.id}`} />)}</div> : null}
        {!library.isPending && !items.length ? <EmptyBlock className="m-2 border-0 py-8" title="还没有报告">在上方输入一个研究问题，报告完成后会出现在这里。</EmptyBlock> : null}
      </CardContent>
    </Card>
  );
}

function ReviewSummary() {
  const pending = usePendingCandidates();
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: api.dashboard });
  const proposals = dashboard.data?.pending_memory_proposals.length ?? 0;
  const ingestions = pending.ingestions.data ?? [];
  const rows = ingestions.filter((item) => (pending.byIngestion.get(item.id) ?? 0) > 0);
  const total = pending.total + proposals;
  return (
    <Card>
      <CardHeader><CardTitle icon={<ListChecks size={16} />} title="待你审核" description="批准后才会成为正式知识，供研究使用。" /></CardHeader>
      <CardContent className="space-y-3">
        {pending.isPending && !rows.length ? <p className="text-[13px] text-muted-ink">统计中…</p> : null}
        {!pending.isPending && !total ? <p className="text-[13px] text-muted-ink">没有待审核的内容。</p> : null}
        {rows.map((item) => (
          <Link className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2.5 hover:border-brand/40 hover:bg-brand-tint" key={item.id} to={`/review?ingestion=${encodeURIComponent(item.id)}`}>
            <span className="min-w-0"><span className="block truncate text-[13.5px] font-medium">{item.collection}</span><span className="text-xs text-muted-ink">抽取的实体与关系</span></span>
            <span className="shrink-0 text-lg font-semibold tabular-nums text-amber-700">{pending.byIngestion.get(item.id)}</span>
          </Link>
        ))}
        {proposals ? (
          <Link className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2.5 hover:border-brand/40 hover:bg-brand-tint" to="/review?tab=memory">
            <span className="min-w-0"><span className="block text-[13.5px] font-medium">项目记忆提案</span><span className="text-xs text-muted-ink">研究智能体提出的决策</span></span>
            <span className="text-lg font-semibold tabular-nums text-amber-700">{proposals}</span>
          </Link>
        ) : null}
        {total ? <Link className="inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-lg bg-brand-soft text-[13px] font-medium text-brand-strong hover:bg-brand-hover" to="/review">开始审核<ArrowRight size={14} /></Link> : null}
      </CardContent>
    </Card>
  );
}

function KnowledgeSummary() {
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections });
  const pending = usePendingCandidates();
  const ingestions = pending.ingestions.data ?? [];
  const formal = (collections.data ?? []).filter((item) => !item.is_system);
  return (
    <Card>
      <CardHeader><CardTitle icon={<Library size={16} />} title="知识库" />{formal.length ? <MoreLink to="/knowledge">查看</MoreLink> : null}</CardHeader>
      <CardContent className="space-y-2.5">
        {formal.map((collection) => {
          const related = ingestions.filter((item) => item.collection_slug === collection.slug);
          const documents = related.reduce((sum, item) => sum + item.document_count, 0);
          const published = related.reduce((sum, item) => sum + item.published_count, 0);
          return (
            <Link className="block rounded-lg px-1 py-1 hover:text-brand" key={collection.slug} to={`/knowledge?collection=${encodeURIComponent(collection.slug)}`}>
              <p className="truncate text-[13.5px] font-medium">{collection.name}</p>
              <p className="text-xs text-muted-ink">{documents} 篇文献 · {published} 条已发布知识</p>
            </Link>
          );
        })}
        {collections.data && !formal.length ? <p className="text-[13px] text-muted-ink">还没有资料。导入论文 PDF 后即可开始研究。</p> : null}
        <Link className="mt-1 inline-flex h-8 items-center gap-1.5 rounded-lg border border-line-strong px-3 text-[13px] font-medium text-ink-soft hover:bg-subtle" to="/knowledge?import=1"><Upload size={14} />导入资料</Link>
      </CardContent>
    </Card>
  );
}

export function DashboardPage() {
  return (
    <Page>
      <div className="mb-6">
        <h1 className="text-[22px] font-semibold tracking-tight sm:text-2xl">研究工作台</h1>
        <p className="mt-1.5 text-sm text-muted-ink">基于你审核过的论文证据提出问题，获得每句话都能追溯到原文的报告。</p>
      </div>
      <ResearchCommandLauncher />
      <div className="mt-8 grid min-w-0 items-start gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <RecentReports />
        <div className="min-w-0 space-y-5">
          <ReviewSummary />
          <KnowledgeSummary />
        </div>
      </div>
    </Page>
  );
}
