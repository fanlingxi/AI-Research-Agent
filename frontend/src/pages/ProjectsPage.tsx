import { useQuery } from "@tanstack/react-query";
import { FolderKanban, Plus } from "lucide-react";
import { Link } from "react-router-dom";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { buttonVariants } from "../components/ui/button";
import { Page, PageHeader } from "../components/ui/page";
import { api } from "../lib/api";
import { formatDate } from "../lib/utils";

export function ProjectsPage() {
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const newProject = <Link className={buttonVariants()} to="?createProject=1"><Plus size={16} />新建项目</Link>;
  return (
    <Page>
      <PageHeader
        title="项目"
        description="项目适合需要长期推进的研究主题：在固定的知识范围内拆分任务，每次运行都冻结上下文，报告可反馈、可修订、可追溯。"
        actions={newProject}
      />
      {projects.isPending ? <LoadingBlock label="读取项目…" /> : null}
      {projects.error instanceof Error ? <ErrorBlock error={projects.error} onRetry={() => void projects.refetch()} /> : null}
      {projects.data && !projects.data.length ? (
        <EmptyBlock icon={<FolderKanban size={19} />} title="还没有项目" action={newProject}>
          只是想快速了解一个问题？直接在首页提问即可。需要持续迭代的研究再建项目。
        </EmptyBlock>
      ) : null}
      <div className="grid gap-4 md:grid-cols-2">
        {projects.data?.map((project) => (
          <Link className="group flex min-w-0 flex-col rounded-xl border bg-panel p-5 shadow-[var(--shadow-card)] transition-colors hover:border-brand/40" key={project.id} to={`/projects/${project.id}/overview`}>
            <div className="flex items-start justify-between gap-3">
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand"><FolderKanban size={17} /></span>
              <StatusBadge status={project.status} />
            </div>
            <h2 className="mt-3 font-semibold leading-6 text-ink group-hover:text-brand-strong">{project.name}</h2>
            <p className="mt-1 line-clamp-2 text-[13px] leading-6 text-muted-ink">{project.goal || "尚未填写研究目标。"}</p>
            <p className="mt-auto pt-4 text-xs text-muted-ink">更新于 {formatDate(project.updated_at)}</p>
          </Link>
        ))}
      </div>
    </Page>
  );
}
