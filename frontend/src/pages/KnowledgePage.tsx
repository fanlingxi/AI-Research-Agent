import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, BookOpen, FileText, Library, RotateCcw, Search, Upload } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "../components/AsyncState";
import { KnowledgeExplorer } from "../components/KnowledgeExplorer";
import { DocumentVersions } from "../components/DocumentVersions";
import { pageLabel } from "../components/report/SourceDrawer";
import { StatusBadge } from "../components/StatusBadge";
import { Badge } from "../components/ui/badge";
import { Button, buttonVariants } from "../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../components/ui/dialog";
import { Field, Input, Select, Textarea } from "../components/ui/field";
import { Disclosure, Page, PageHeader, Segmented } from "../components/ui/page";
import { api, type KnowledgeCollection, type KnowledgeDocument, type KnowledgeIngestion } from "../lib/api";
import { fileName, useIngestions, usePendingCandidates, useSystemStatus } from "../lib/hooks";
import { entityTypeLabel } from "../lib/labels";
import { cn, formatDate } from "../lib/utils";

const PAGE_LIMITS = [
  { value: 20, hint: "快速预览" },
  { value: 50, hint: "平衡" },
  { value: 100, hint: "较完整" },
  { value: 150, hint: "长文档" },
] as const;

function ImportDialog({ open, onOpenChange, collections }: { open: boolean; onOpenChange: (open: boolean) => void; collections: KnowledgeCollection[] }) {
  const client = useQueryClient();
  const [collection, setCollection] = useState("");
  const [sources, setSources] = useState("");
  const [pageLimit, setPageLimit] = useState<number>(20);
  const sourceList = sources.split("\n").map((source) => source.trim()).filter(Boolean);
  const submit = useMutation({
    mutationFn: () => api.submitAndExecuteIngestion({
      collection: collection.trim() || undefined,
      sources: sourceList,
      pdf_max_pages: pageLimit,
    }),
    onSuccess: () => {
      setSources("");
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
      void client.invalidateQueries({ queryKey: ["knowledge-collections"] });
      void client.invalidateQueries({ queryKey: ["knowledge-health"] });
      onOpenChange(false);
    },
  });
  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (sourceList.length) submit.mutate();
  };
  const formal = collections.filter((item) => !item.is_system);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[min(600px,calc(100vw-2rem))]">
        <DialogTitle>导入资料</DialogTitle>
        <DialogDescription>系统会在后台解析 PDF、建立索引并抽取候选知识。抽取完成后到「审核」中确认，批准的内容才会用于研究。</DialogDescription>
        <form className="mt-5 space-y-4" onSubmit={onSubmit}>
          <Field label="放入知识集合" hint={formal.length ? "可输入已有集合名称，或输入新名称创建集合；留空则放入收件箱。" : "输入名称会创建新集合；留空则放入收件箱，之后可再移动。"}>
            <Input list="collection-options" placeholder="例如：RAG 检索改进" value={collection} onChange={(event) => setCollection(event.target.value)} />
            <datalist id="collection-options">{formal.map((item) => <option key={item.slug} value={item.name} />)}</datalist>
          </Field>
          <Field label="PDF 路径或 URL（每行一个）" hint={`${sourceList.length} 个文件待导入。本地路径需是服务端可访问的文件。`}>
            <Textarea className="min-h-32 font-mono text-xs leading-5" minLength={3} placeholder={"data/raw_papers/paper.pdf\nhttps://arxiv.org/pdf/2302.04761"} required value={sources} onChange={(event) => setSources(event.target.value)} />
          </Field>
          <fieldset>
            <legend className="text-[13px] font-medium text-ink-soft">每篇最多处理页数</legend>
            <div className="mt-1.5 grid grid-cols-2 gap-2 sm:grid-cols-4">
              {PAGE_LIMITS.map((limit) => (
                <button
                  aria-pressed={pageLimit === limit.value}
                  className={cn("rounded-lg border px-3 py-2 text-left", pageLimit === limit.value ? "border-brand bg-brand-soft text-brand-strong" : "border-line-strong text-ink-soft hover:border-brand/40")}
                  key={limit.value}
                  onClick={() => setPageLimit(limit.value)}
                  type="button"
                >
                  <span className="block text-sm font-medium">{limit.value} 页</span>
                  <span className="text-xs opacity-75">{limit.hint}</span>
                </button>
              ))}
            </div>
            <p className="mt-1.5 text-xs leading-5 text-muted-ink">已处理的页面会全部建立索引；候选知识抽取采用分层采样。</p>
          </fieldset>
          {submit.error instanceof Error ? <ErrorBlock error={submit.error} /> : null}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
            <Button disabled={!sourceList.length || submit.isPending} type="submit"><Upload size={15} />{submit.isPending ? "正在提交…" : "提交并开始入库"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function MoveInboxIngestion({ ingestion, collections }: { ingestion: KnowledgeIngestion; collections: KnowledgeCollection[] }) {
  const client = useQueryClient();
  const [targetSlug, setTargetSlug] = useState("");
  const formalCollections = collections.filter((collection) => !collection.is_system);
  const move = useMutation({
    mutationFn: () => {
      const target = formalCollections.find((collection) => collection.slug === targetSlug);
      if (!target) throw new Error("请选择正式知识集合。");
      return api.moveIngestionCollection(ingestion.id, target.name);
    },
    onSuccess: () => {
      setTargetSlug("");
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
      void client.invalidateQueries({ queryKey: ["knowledge-collections"] });
      void client.invalidateQueries({ queryKey: ["knowledge-health"] });
    },
  });
  if (ingestion.collection_slug !== "inbox" || ["queued", "running", "publishing"].includes(ingestion.status)) return null;
  return (
    <div className="mt-3 rounded-lg border border-amber-200 bg-warning-soft p-3">
      <p className="text-xs text-amber-950">这批资料还在收件箱，研究时不会被使用。移动到一个知识集合后即可使用。</p>
      <div className="mt-2 flex flex-wrap gap-2">
        <Select aria-label={`移动 ${ingestion.collection} 到正式知识集合`} className="h-8 min-w-0 flex-1 text-xs" value={targetSlug} onChange={(event) => setTargetSlug(event.target.value)}>
          <option value="">选择知识集合</option>
          {formalCollections.map((collection) => <option key={collection.slug} value={collection.slug}>{collection.name}</option>)}
        </Select>
        <Button disabled={!targetSlug || move.isPending} size="sm" variant="outline" onClick={() => move.mutate()}>{move.isPending ? "移动中…" : "移动"}</Button>
      </div>
      {!formalCollections.length ? <p className="mt-2 text-xs text-amber-900">还没有知识集合：导入时填写集合名称即可创建。</p> : null}
      {move.error instanceof Error ? <div className="mt-2"><ErrorBlock error={move.error} /></div> : null}
    </div>
  );
}

function IngestionRecord({ item, collections, highlighted, documents }: { item: KnowledgeIngestion; collections: KnowledgeCollection[]; highlighted: boolean; documents: KnowledgeDocument[] }) {
  const client = useQueryClient();
  const retry = useMutation({
    mutationFn: () => api.retryIngestion(item.id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["knowledge-ingestions"] });
      void client.invalidateQueries({ queryKey: ["knowledge-health"] });
    },
  });
  const active = ["queued", "running", "publishing"].includes(item.status);
  return (
    <article className={cn("rounded-lg border bg-panel p-3.5", highlighted && "border-brand ring-2 ring-brand/15")} id={`ingestion-${item.id}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[13.5px] font-medium">{item.collection}</p>
          <p className="mt-0.5 text-xs text-muted-ink">{formatDate(item.created_at)}</p>
        </div>
        <StatusBadge status={item.status} />
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-ink">
        <span>{item.document_count} / {item.sources.length} 篇已解析</span>
        <span>{item.candidate_count} 个候选</span>
        <span>{item.published_count} 条已发布</span>
        {item.queue_position ? <span>队列第 {item.queue_position} 位</span> : null}
      </div>
      {active ? <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-subtle"><div className="h-full w-1/3 animate-pulse rounded-full bg-brand" /></div> : null}
      {item.error ? <p className="mt-2.5 break-words rounded-md bg-danger-soft p-2 text-xs leading-5 text-red-900">{item.error}</p> : null}
      {["failed", "interrupted"].includes(item.status) ? (
        <div className="mt-2.5">
          <Button disabled={retry.isPending} size="sm" variant="outline" onClick={() => retry.mutate()}><RotateCcw size={13} />{retry.isPending ? "正在重新启动…" : "重新执行"}</Button>
          {retry.error instanceof Error ? <div className="mt-2"><ErrorBlock error={retry.error} /></div> : null}
        </div>
      ) : null}
      {item.status === "needs_review" ? <Link className="mt-2.5 inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline" to={`/review?ingestion=${encodeURIComponent(item.id)}`}>审核候选知识<ArrowRight size={12} /></Link> : null}
      <details className="group mt-2">
        <summary className="list-none text-xs text-muted-ink hover:text-ink">文件列表（{item.sources.length}）· 每篇最多 {item.pdf_max_pages} 页</summary>
        <ul className="mt-1.5 space-y-0.5 text-xs text-ink-soft">{item.sources.map((source) => <li className="truncate" key={source} title={source}>{fileName(source)}</li>)}</ul>
        {documents.map((document) => <div className="mt-3 border-t pt-3" key={document.document_id}><p className="mb-2 break-words text-xs font-medium">{document.title}</p><DocumentVersions document={document} /></div>)}
      </details>
      <MoveInboxIngestion collections={collections} ingestion={item} />
    </article>
  );
}

/** One row per source: every version of a document shares the same version list. */
function documentFamilies(documents: KnowledgeDocument[]) {
  const families = new Map<string, KnowledgeDocument>();
  for (const document of documents) {
    const key = document.versions.map((version) => version.source_id).sort().join("|") || document.document_id;
    const current = families.get(key);
    if (!current || (!current.is_active && document.is_active)) families.set(key, document);
  }
  return [...families.values()];
}

function CollectionBrowser({ collection, ingestions, pending, parsedDocuments }: { collection: KnowledgeCollection; ingestions: KnowledgeIngestion[]; pending: number; parsedDocuments: KnowledgeDocument[] }) {
  const detail = useQuery({ queryKey: ["knowledge-collection", collection.slug], queryFn: () => api.collection(collection.slug), staleTime: 60_000 });
  const [view, setView] = useState<"papers" | "entities">("papers");
  const families = documentFamilies(parsedDocuments);
  const [type, setType] = useState("all");
  const [filter, setFilter] = useState("");
  const entities = detail.data?.entities ?? [];
  const papers = entities.filter((entity) => entity.type === "Paper");
  const types = Array.from(new Set(entities.map((entity) => entity.type))).filter((value) => value !== "Paper").sort();
  const visible = entities.filter((entity) => entity.type !== "Paper"
    && (type === "all" || entity.type === type)
    && (!filter.trim() || `${entity.name} ${entity.summary} ${entity.aliases.join(" ")}`.toLowerCase().includes(filter.trim().toLowerCase())));
  const documents = ingestions.reduce((sum, item) => sum + item.document_count, 0);
  const sources = ingestions.flatMap((item) => item.sources);
  return (
    <section className="min-w-0 rounded-xl border bg-panel shadow-[var(--shadow-card)]" id={`collection-${collection.slug}`}>
      <div className="flex flex-wrap items-start justify-between gap-3 p-4 sm:p-5">
        <div className="flex min-w-0 gap-3">
          <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand"><Library size={18} /></span>
          <div className="min-w-0">
            <h2 className="break-words text-base font-semibold">{collection.name}</h2>
            <p className="mt-0.5 text-[13px] text-muted-ink">
              {documents} 篇文献 · {detail.data ? `${entities.length - papers.length} 个知识实体 · ${detail.data.relations.length} 条关系` : "统计中…"}
              {collection.updated_at ? ` · 更新于 ${formatDate(collection.updated_at)}` : ""}
            </p>
          </div>
        </div>
        {pending ? <Link className={buttonVariants({ size: "sm", variant: "secondary" })} to={`/review?collection=${encodeURIComponent(collection.slug)}`}>{pending} 条待审核<ArrowRight size={14} /></Link> : null}
      </div>
      <div className="border-t px-4 pb-4 pt-3 sm:px-5">
        <Segmented label="浏览方式" value={view} onChange={setView} options={[{ value: "papers", label: "文献", count: papers.length || sources.length }, { value: "entities", label: "知识条目", count: entities.length - papers.length }]} />
        {detail.isPending ? <p className="mt-3 text-[13px] text-muted-ink">读取中…</p> : null}
        {detail.error instanceof Error ? <div className="mt-3"><ErrorBlock error={detail.error} onRetry={() => void detail.refetch()} /></div> : null}
        {detail.data && view === "papers" ? (
          <ul className="mt-3 grid gap-2 sm:grid-cols-2">
            {(papers.length ? papers.map((paper) => ({ key: paper.id, title: paper.name, note: paper.summary })) : sources.map((source) => ({ key: source, title: fileName(source), note: "" }))).map((paper) => (
              <li className="flex min-w-0 gap-2.5 rounded-lg border px-3 py-2.5" key={paper.key}>
                <FileText className="mt-0.5 shrink-0 text-muted-ink" size={15} />
                <span className="min-w-0"><span className="block text-[13.5px] font-medium leading-5">{paper.title}</span>{paper.note ? <span className="mt-0.5 line-clamp-2 text-xs leading-5 text-muted-ink">{paper.note}</span> : null}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {detail.data && view === "papers" && families.length ? (
          <Disclosure className="mt-3" summary={`资料版本与重新解析（${families.length}）`}>
            <ul className="space-y-3 border-t px-3.5 py-3">
              {families.map((document) => <li className="min-w-0 space-y-1.5" key={document.document_id}><p className="break-words text-[13px] font-medium">{document.title}</p><DocumentVersions document={document} /></li>)}
            </ul>
          </Disclosure>
        ) : null}
        {detail.data && view === "entities" ? (
          <div className="mt-3">
            <div className="flex flex-wrap gap-2">
              <div className="relative min-w-0 flex-1 basis-48">
                <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-ink" size={14} />
                <Input aria-label="筛选知识条目" className="h-8 pl-8 text-[13px]" placeholder="按名称或描述筛选" value={filter} onChange={(event) => setFilter(event.target.value)} />
              </div>
              <Select aria-label="条目类型" className="h-8 w-auto text-[13px]" value={type} onChange={(event) => setType(event.target.value)}>
                <option value="all">全部类型</option>
                {types.map((value) => <option key={value} value={value}>{entityTypeLabel(value)}</option>)}
              </Select>
            </div>
            <ul className="scroll-thin mt-3 max-h-[26rem] divide-y overflow-y-auto rounded-lg border">
              {visible.map((entity) => (
                <li key={entity.id}>
                  <details className="group">
                    <summary className="flex list-none items-start gap-2 px-3 py-2.5 hover:bg-subtle">
                      <Badge tone="neutral" className="mt-0.5">{entityTypeLabel(entity.type)}</Badge>
                      <span className="min-w-0"><span className="block text-[13.5px] font-medium">{entity.name}</span><span className="line-clamp-1 text-xs text-muted-ink group-open:line-clamp-none">{entity.summary}</span></span>
                    </summary>
                    <div className="space-y-2 px-3 pb-3">
                      {entity.evidence.slice(0, 2).map((evidence) => (
                        <blockquote className="rounded-md border-l-2 border-brand/50 bg-brand-tint px-3 py-2 text-xs leading-5 text-ink-soft" key={`${evidence.chunk_id}:${evidence.quote.slice(0, 12)}`}>
                          <p className="line-clamp-4">{evidence.quote}</p>
                          <footer className="mt-1 text-muted-ink">{pageLabel(evidence.page_start, evidence.page_end)}</footer>
                        </blockquote>
                      ))}
                    </div>
                  </details>
                </li>
              ))}
              {!visible.length ? <li className="p-4 text-center text-[13px] text-muted-ink">没有匹配的条目。</li> : null}
            </ul>
          </div>
        ) : null}
      </div>
    </section>
  );
}

export function KnowledgePage() {
  const [params, setParams] = useSearchParams();
  const ingestions = useIngestions();
  const parsedDocuments = useQuery({ queryKey: ["knowledge-documents"], queryFn: api.knowledgeDocuments, refetchInterval: 10_000 });
  const pending = usePendingCandidates();
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections });
  const health = useSystemStatus();
  const [importOpen, setImportOpen] = useState(params.get("import") === "1");
  const highlighted = params.get("ingestion");
  useEffect(() => {
    if (params.get("import") === "1") setImportOpen(true);
  }, [params]);
  const setImport = (open: boolean) => {
    setImportOpen(open);
    if (!open && params.get("import")) setParams((current) => { const next = new URLSearchParams(current); next.delete("import"); return next; });
  };
  const formal = useMemo(() => collections.data?.filter((item) => !item.is_system) ?? [], [collections.data]);
  if (ingestions.isPending || collections.isPending) return <Page><LoadingBlock label="读取知识库…" /></Page>;
  if (ingestions.error instanceof Error || collections.error instanceof Error) {
    return <Page><ErrorBlock error={(ingestions.error ?? collections.error) as Error} onRetry={() => { void ingestions.refetch(); void collections.refetch(); }} /></Page>;
  }
  const items = ingestions.data ?? [];
  const documents = items.reduce((sum, item) => sum + item.document_count, 0);
  const published = items.reduce((sum, item) => sum + item.published_count, 0);
  const workerOnline = health.data?.services.worker?.available;
  const llmReady = health.data?.knowledge.live_llm_configured;
  const importButton = <Button onClick={() => setImport(true)}><Upload size={15} />导入资料</Button>;
  return (
    <Page>
      <PageHeader
        title="知识库"
        description="导入论文后，系统会自动解析并抽取候选知识；经你审核批准的内容才会成为正式知识，用于检索和研究。"
        actions={importButton}
      />
      {health.data && (!workerOnline || !llmReady) ? (
        <Notice className="mb-6" tone="warning" icon={<AlertTriangle size={15} />} title={!llmReady ? "未配置语言模型，新导入的资料暂时无法抽取知识" : "后台执行器未运行，新导入会排队等待"} action={<Link className="text-[13px] font-medium underline" to="/runtime">查看详情</Link>}>
          已发布的知识不受影响，可以照常检索、研究和阅读报告。
        </Notice>
      ) : null}

      <div className="mb-8 grid grid-cols-2 gap-px overflow-hidden rounded-xl border bg-line shadow-[var(--shadow-card)] sm:grid-cols-4">
        {[
          ["知识集合", formal.length, null],
          ["文献", documents, null],
          ["已发布知识", published, null],
          ["待审核", pending.total, "/review"],
        ].map(([label, value, to]) => (
          <div className="bg-panel px-4 py-3.5" key={String(label)}>
            <p className="text-[13px] text-muted-ink">{label}</p>
            {to && Number(value) > 0 ? <Link className="mt-0.5 inline-flex items-center gap-1 text-xl font-semibold tabular-nums text-amber-700 hover:underline" to={String(to)}>{value}<ArrowRight size={15} /></Link> : <p className="mt-0.5 text-xl font-semibold tabular-nums">{value}</p>}
          </div>
        ))}
      </div>

      <div className="grid min-w-0 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-6">
          {parsedDocuments.error instanceof Error ? <ErrorBlock error={parsedDocuments.error} onRetry={() => void parsedDocuments.refetch()} /> : null}
          {formal.map((collection) => (
            <CollectionBrowser
              collection={collection}
              parsedDocuments={(parsedDocuments.data ?? []).filter((document) => document.ingestion_ids.some((id) => items.some((item) => item.id === id && item.collection_slug === collection.slug)))}
              ingestions={items.filter((item) => item.collection_slug === collection.slug)}
              key={collection.slug}
              pending={items.filter((item) => item.collection_slug === collection.slug).reduce((sum, item) => sum + (pending.byIngestion.get(item.id) ?? 0), 0)}
            />
          ))}
          {!formal.length ? (
            <EmptyBlock icon={<BookOpen size={19} />} title="还没有知识集合">导入第一批论文 PDF，并为它们起一个集合名称。</EmptyBlock>
          ) : null}
          <KnowledgeExplorer collections={formal} />
        </div>
        <aside className="min-w-0">
          <h2 className="mb-2.5 text-[15px] font-semibold">导入记录</h2>
          <div className="scroll-thin max-h-[40rem] space-y-2.5 overflow-y-auto overscroll-contain pr-1" data-testid="ingestion-history-list">
            {items.map((item) => <IngestionRecord documents={(parsedDocuments.data ?? []).filter((document) => document.ingestion_ids.includes(item.id))} collections={collections.data ?? []} highlighted={highlighted === item.id} item={item} key={item.id} />)}
            {!items.length ? <EmptyBlock className="py-8" title="还没有导入记录">点击右上角「导入资料」开始。</EmptyBlock> : null}
          </div>
          <Disclosure className="mt-4" summary="导入流程说明">
            <ol className="list-decimal space-y-1 pl-4 text-xs leading-5 text-muted-ink">
              <li>后台解析 PDF、切分段落并建立检索索引。</li>
              <li>语言模型抽取候选实体与关系，每条都附带原文出处。</li>
              <li>你在「审核」中批准、驳回或搁置候选。</li>
              <li>批准的内容成为正式知识，供检索、报告和项目研究使用。</li>
            </ol>
          </Disclosure>
        </aside>
      </div>
      <ImportDialog collections={collections.data ?? []} open={importOpen} onOpenChange={setImport} />
    </Page>
  );
}
