import { queryOptions, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";

import { api, type Artifact, type ResearchReport } from "./api";

const ACTIVE_INGESTION = ["queued", "running", "publishing"];

export function useIngestions() {
  return useQuery({
    queryKey: ["knowledge-ingestions"],
    queryFn: api.ingestions,
    refetchInterval: (query) => query.state.data?.some((item) => ACTIVE_INGESTION.includes(item.status)) ? 2_500 : false,
  });
}

/**
 * Exact number of candidates still waiting for a human decision, per ingestion.
 * Uses the paginated endpoint with limit=1 so only the total is transferred.
 */
export function usePendingCandidates() {
  const ingestions = useIngestions();
  const reviewable = (ingestions.data ?? []).filter((item) => item.candidate_count > 0);
  const totals = useQueries({
    queries: reviewable.map((item) => ({
      queryKey: ["knowledge-candidates", item.id, "pending-total", item.updated_at],
      queryFn: () => api.candidatePage(item.id, { limit: 1 }),
      staleTime: 30_000,
    })),
  });
  const byIngestion = new Map<string, number>();
  reviewable.forEach((item, index) => {
    const total = totals[index]?.data?.total;
    if (total != null) byIngestion.set(item.id, total);
  });
  const total = Array.from(byIngestion.values()).reduce((sum, value) => sum + value, 0);
  return { ingestions, byIngestion, total, isPending: ingestions.isPending || totals.some((query) => query.isPending) };
}

/** Paper titles derived from approved "Paper" entities, for places that only carry a paper id. */
export function usePaperTitles() {
  const collections = useQuery({ queryKey: ["knowledge-collections"], queryFn: api.collections });
  const formal = (collections.data ?? []).filter((item) => !item.is_system);
  const details = useQueries({
    queries: formal.map((item) => ({
      queryKey: ["knowledge-collection", item.slug],
      queryFn: () => api.collection(item.slug),
      staleTime: 60_000,
    })),
  });
  const signature = details.map((query) => query.dataUpdatedAt).join(":");
  return useMemo(() => {
    const titles = new Map<string, string>();
    for (const query of details) {
      for (const entity of query.data?.entities ?? []) {
        if (entity.type !== "Paper") continue;
        for (const evidence of entity.evidence) {
          if (!titles.has(evidence.paper_id)) titles.set(evidence.paper_id, entity.name);
        }
      }
    }
    return titles;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);
}

export function fileName(uri: string | null | undefined) {
  if (!uri) return "";
  const isUrl = /^[a-z][a-z\d+.-]*:\/\//i.test(uri);
  const clean = isUrl ? uri.split(/[?#]/)[0] : uri;
  const name = clean.split(/[\\/]/).filter(Boolean).at(-1) ?? clean;
  if (!isUrl) return name;
  try {
    return decodeURIComponent(name);
  } catch {
    return name;
  }
}

export function documentTitle(
  title: string | null | undefined,
  paperId: string | null | undefined,
  titles: Map<string, string>,
  uri?: string | null,
) {
  // Prefer the reviewed paper title from formal knowledge so the same paper is named
  // consistently everywhere; stored parser titles (possibly frozen in snapshots) are the fallback.
  if (paperId && titles.has(paperId)) return titles.get(paperId)!;
  if (title && title.trim()) return title.trim();
  const name = fileName(uri);
  if (name) return name;
  return "未识别标题的文献";
}

export interface LibraryItem {
  kind: "quick" | "project";
  id: string;
  title: string;
  status: string;
  updatedAt: string;
  href: string;
  context: string;
  report?: ResearchReport;
  artifact?: Artifact;
}

/** Quick reports and project-run reports, newest first, as one reading list. */
export function useReportLibrary() {
  const client = useQueryClient();
  const reports = useQuery({
    queryKey: ["reports"],
    queryFn: api.reports,
    refetchInterval: (query) => query.state.data?.some((report) => ["queued", "running"].includes(report.status)) ? 2_500 : false,
  });
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const projectList = projects.data ?? [];
  const runs = useQueries({
    queries: projectList.map((project) => queryOptions({
      queryKey: ["project-runs", project.id],
      queryFn: () => api.projectRuns(project.id),
      refetchInterval: (query) => query.state.data?.items.some((run) => ["queued", "preparing", "running", "validating"].includes(run.status)) ? 2_500 : false,
    })),
  });
  const activeProjectIds = projectList.filter((_project, index) => runs[index]?.data?.items.some((run) => ["queued", "preparing", "running", "validating"].includes(run.status))).map((project) => project.id);
  const activeSignature = JSON.stringify(activeProjectIds);
  const previousActive = useRef<string[]>([]);
  useEffect(() => {
    const activeIds = JSON.parse(activeSignature) as string[];
    for (const id of previousActive.current) {
      if (!activeIds.includes(id)) void client.invalidateQueries({ queryKey: ["artifacts", id] });
    }
    previousActive.current = activeIds;
  }, [activeSignature, client]);
  const artifacts = useQueries({
    queries: projectList.map((project) => queryOptions({
      queryKey: ["artifacts", project.id],
      queryFn: () => api.artifacts(project.id),
      refetchInterval: activeProjectIds.includes(project.id) ? 2_500 : false,
    })),
  });
  const tasks = useQueries({
    queries: projectList.map((project) => ({ queryKey: ["tasks", project.id], queryFn: () => api.tasks(project.id, true) })),
  });
  const signature = [reports.dataUpdatedAt, ...artifacts.map((query) => query.dataUpdatedAt), ...tasks.map((query) => query.dataUpdatedAt)].join(":");
  const items = useMemo<LibraryItem[]>(() => {
    const result: LibraryItem[] = (reports.data ?? []).map((report) => ({
      kind: "quick",
      id: report.id,
      title: report.query,
      status: report.status === "failed" && report.content.trim() ? "quality_failed" : report.status,
      updatedAt: report.updated_at,
      href: `/reports/${encodeURIComponent(report.id)}`,
      context: `${report.evidence.length} 条证据`,
      report,
    }));
    projectList.forEach((project, index) => {
      const taskTitles = new Map((tasks[index]?.data ?? []).map((task) => [task.id, task.title]));
      const reportsOnly = (artifacts[index]?.data ?? [])
        .filter((artifact) => artifact.type === "research_report")
        .sort((left, right) => left.created_at.localeCompare(right.created_at));
      const perTask = new Map<string, number>();
      for (const artifact of reportsOnly) perTask.set(artifact.task_id ?? "", (perTask.get(artifact.task_id ?? "") ?? 0) + 1);
      const seen = new Map<string, number>();
      for (const artifact of reportsOnly) {
        const key = artifact.task_id ?? "";
        const edition = (seen.get(key) ?? 0) + 1;
        seen.set(key, edition);
        result.push({
          kind: "project",
          id: artifact.id,
          title: (artifact.task_id && taskTitles.get(artifact.task_id)) || "项目研究报告",
          status: artifact.status,
          updatedAt: artifact.created_at,
          href: `/artifacts/${encodeURIComponent(artifact.id)}`,
          context: (perTask.get(key) ?? 0) > 1 ? `${project.name} · 第 ${edition} 版` : project.name,
          artifact,
        });
      }
    });
    return result.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, projects.dataUpdatedAt]);
  return {
    items,
    isPending: reports.isPending || projects.isPending || [...artifacts, ...tasks, ...runs].some((query) => query.isPending),
    error: (reports.error ?? projects.error ?? [...artifacts, ...tasks, ...runs].find((query) => query.error)?.error ?? null) as Error | null,
    refetch: () => {
      void reports.refetch();
      void projects.refetch();
      for (const query of [...artifacts, ...tasks, ...runs]) void query.refetch();
    },
  };
}

export function useSystemStatus() {
  return useQuery({ queryKey: ["knowledge-health"], queryFn: api.knowledgeHealth, refetchInterval: 15_000 });
}
