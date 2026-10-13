import { useQueries, useQuery } from "@tanstack/react-query";
import { ArrowRight, FileDown, GitBranch, Lock, MessageSquareWarning, Workflow } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ErrorBlock, LoadingBlock, Notice } from "../components/AsyncState";
import { FollowUpLinks, FollowUpPanel } from "../components/FollowUpPanel";
import { ReportLayout, RailSection } from "../components/report/ReportLayout";
import { extractCitations, ReportMarkdown, splitTitle } from "../components/report/ReportMarkdown";
import { pageLabel, SnapshotSourceSheet } from "../components/report/SourceDrawer";
import { SourceList } from "../components/report/SourceList";
import { RunFeedbackPanel } from "../components/RunFeedbackPanel";
import { StatusBadge } from "../components/StatusBadge";
import { Badge } from "../components/ui/badge";
import { buttonVariants } from "../components/ui/button";
import { Disclosure, Page } from "../components/ui/page";
import { api } from "../lib/api";
import { documentTitle, usePaperTitles } from "../lib/hooks";
import { formatDate, shortId } from "../lib/utils";

export function ArtifactPage() {
  const { artifactId = "" } = useParams();
  const [active, setActive] = useState<string | null>(null);
  const titles = usePaperTitles();
  const artifact = useQuery({ queryKey: ["artifact", artifactId], queryFn: () => api.artifact(artifactId) });
  const content = useQuery({ queryKey: ["artifact-content", artifactId], queryFn: () => api.artifactContent(artifactId) });
  const runId = content.data?.run_id;
  const projectId = artifact.data?.project_id;
  const taskId = artifact.data?.task_id;
  const run = useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId!), enabled: Boolean(runId) });
  const output = useQuery({ queryKey: ["output", runId], queryFn: () => api.output(runId!), enabled: run.data?.status === "completed" });
  const task = useQuery({ queryKey: ["task", taskId], queryFn: () => api.task(taskId!), enabled: Boolean(taskId) });
  const project = useQuery({ queryKey: ["project", projectId], queryFn: () => api.project(projectId!), enabled: Boolean(projectId) });
  const siblings = useQuery({ queryKey: ["artifacts", projectId], queryFn: () => api.artifacts(projectId!), enabled: Boolean(projectId) });
  const feedback = useQuery({ queryKey: ["feedback", runId], queryFn: () => api.feedback(runId!), enabled: Boolean(runId) });

  const markdown = content.data?.rendered_markdown ?? "";
  const { title: markdownTitle, body } = useMemo(() => splitTitle(markdown), [markdown]);
  const citations = useMemo(() => extractCitations(body), [body]);
  const snapshotId = content.data?.context_snapshot_id ?? "";
  const evidence = useQueries({
    queries: citations.map((id) => ({
      queryKey: ["evidence", snapshotId, id],
      queryFn: () => api.evidence(snapshotId, id),
      enabled: Boolean(snapshotId),
      staleTime: Infinity,
    })),
  });
  const onCite = useCallback((id: string) => setActive(id), []);

  if (artifact.isPending || content.isPending) return <Page><LoadingBlock label="读取报告…" /></Page>;
  if (artifact.error instanceof Error || content.error instanceof Error || !artifact.data || !content.data) {
    return <Page><ErrorBlock error={(artifact.error ?? content.error ?? new Error("找不到这份报告。")) as Error} title="无法打开这份报告" /></Page>;
  }
  const record = artifact.data;
  const body_ = content.data;
  const artifactForRun = (id: string) => siblings.data?.find((item) => item.metadata.agent_run_id === id);
  const parentLink = feedback.data?.links.find((link) => link.child_run_id === body_.run_id);
  const childLinks = feedback.data?.links.filter((link) => link.parent_run_id === body_.run_id && link.child_artifact_id) ?? [];
  const parentArtifact = parentLink ? artifactForRun(parentLink.parent_run_id) : undefined;
  const sources = citations.map((id, index) => {
    const data = evidence[index]?.data;
    return {
      id,
      title: data ? documentTitle(data.document.title, data.document.id, titles, data.source.uri) : evidence[index]?.isError ? "无法定位此引用" : "读取中…",
      page: data ? pageLabel(data.chunk.page_start, data.chunk.page_end) : "",
      excerpt: data?.evidence.quote,
    };
  });
  const downloadHref = `data:text/markdown;charset=utf-8,${encodeURIComponent(body_.rendered_markdown)}`;
  return (
    <>
      <ReportLayout
        kind={<>
          <Badge tone="brand">项目报告</Badge>
          {parentLink ? <Badge tone="info"><GitBranch size={12} />修订版</Badge> : null}
          {record.status !== "ready" ? <StatusBadge status={record.status} /> : null}
        </>}
        title={markdownTitle ?? task.data?.title ?? "项目研究报告"}
        meta={<>
          {project.data ? <Link className="hover:text-brand" to={`/projects/${project.data.id}/overview`}>{project.data.name}</Link> : null}
          {task.data ? <Link className="hover:text-brand" to={`/projects/${record.project_id}/tasks/${task.data.id}`}>任务：{task.data.title}</Link> : null}
          <span>{formatDate(record.created_at)}</span>
          <span>引用 {citations.length} 处原文</span>
        </>}
        actions={<>
          <a className={buttonVariants({ variant: "outline" })} download={`${(markdownTitle ?? "report").slice(0, 40)}.md`} href={downloadHref}><FileDown size={15} />下载 Markdown</a>
          <Link className={buttonVariants({ variant: "outline" })} to={`/agent-runs/${body_.run_id}`}><Workflow size={15} />查看研究过程</Link>
          <a className={buttonVariants({ variant: "ghost" })} href="#feedback"><MessageSquareWarning size={15} />反馈问题</a>
        </>}
        notices={<>
          <FollowUpLinks key={body_.run_id} runId={body_.run_id} task={task.data} />
          {childLinks.map((link) => (
            <Notice key={link.id} tone="info" icon={<GitBranch size={15} />} title="这份报告已有修订版本"
              action={<Link className={buttonVariants({ size: "sm" })} to={`/artifacts/${link.child_artifact_id}`}>阅读修订版<ArrowRight size={14} /></Link>}>
              根据反馈「{link.request.resolution_note.slice(0, 60)}{link.request.resolution_note.length > 60 ? "…" : ""}」重新运行生成。本报告保持原样以便对照。
            </Notice>
          ))}
          {parentLink ? (
            <Notice tone="info" icon={<GitBranch size={15} />} title="这是根据反馈生成的修订版本"
              action={parentArtifact ? <Link className={buttonVariants({ size: "sm", variant: "outline" })} to={`/artifacts/${parentArtifact.id}`}>对照原报告</Link> : undefined}>
              {parentLink.recheck ? `复检结论：${parentLink.recheck.decision === "resolved" ? "问题已解决" : "仍未解决"}。` : "尚未复检。"}
            </Notice>
          ) : null}
        </>}
        body={<ReportMarkdown activeId={active} markdown={body} onCite={onCite} />}
        rail={<>
          <RailSection title={`引用来源 · ${citations.length}`}>
            <SourceList activeId={active} items={sources} onSelect={setActive} />
          </RailSection>
          <RailSection title={<span className="inline-flex items-center gap-1.5"><Lock size={13} />关于这份报告</span>}>
            <p className="text-[13px] leading-6 text-muted-ink">由一次研究运行基于冻结的上下文快照生成，内容不会再被修改。发现问题请在下方登记反馈；受理后会创建新的修订运行并生成新报告，原报告保留。</p>
          </RailSection>
          <Disclosure summary="技术详情">
            <dl className="space-y-1.5 break-all text-xs text-muted-ink">
              <div><dt className="inline">运行：</dt><dd className="inline font-mono text-ink-soft">{shortId(body_.run_id)}</dd></div>
              <div><dt className="inline">上下文快照：</dt><dd className="inline font-mono text-ink-soft">{shortId(body_.context_snapshot_id)}</dd></div>
              <div><dt className="inline">输出指纹：</dt><dd className="inline font-mono text-ink-soft">{shortId(body_.output_sha256)}</dd></div>
              <div><dt className="inline">引用校验：</dt><dd className="inline text-ink-soft">{body_.validation.passed === true ? "通过" : body_.validation.passed === false ? "未通过" : "未知"}</dd></div>
            </dl>
          </Disclosure>
        </>}
        after={<div className="space-y-6">
          {run.data?.status === "completed" ? <FollowUpPanel key={body_.run_id} runId={body_.run_id} artifactId={record.id} projectId={record.project_id} /> : null}
          {run.data ? <RunFeedbackPanel key={run.data.id} output={output.data} run={run.data} /> : null}
        </div>}
        backLabel="全部报告"
      />
      <SnapshotSourceSheet citations={citations} evidenceId={active} snapshotId={body_.context_snapshot_id} onSelect={setActive} />
    </>
  );
}
