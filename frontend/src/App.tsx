import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { LoadingBlock } from "./components/AsyncState";
import { Page } from "./components/ui/page";

const DashboardPage = lazy(async () => ({ default: (await import("./pages/DashboardPage")).DashboardPage }));
const ProjectsPage = lazy(async () => ({ default: (await import("./pages/ProjectsPage")).ProjectsPage }));
const ProjectWorkspacePage = lazy(async () => ({ default: (await import("./pages/ProjectWorkspacePage")).ProjectWorkspacePage }));
const TaskDetailPage = lazy(async () => ({ default: (await import("./pages/TaskDetailPage")).TaskDetailPage }));
const AgentRunPage = lazy(async () => ({ default: (await import("./pages/AgentRunPage")).AgentRunPage }));
const ArtifactPage = lazy(async () => ({ default: (await import("./pages/ArtifactPage")).ArtifactPage }));
const ReviewPage = lazy(async () => ({ default: (await import("./pages/ReviewPage")).ReviewPage }));
const KnowledgePage = lazy(async () => ({ default: (await import("./pages/KnowledgePage")).KnowledgePage }));
const ReportsPage = lazy(async () => ({ default: (await import("./pages/ReportsPage")).ReportsPage }));
const QuickReportPage = lazy(async () => ({ default: (await import("./pages/QuickReportPage")).QuickReportPage }));
const RuntimePage = lazy(async () => ({ default: (await import("./pages/RuntimePage")).RuntimePage }));
const ExperimentsPage = lazy(async () => ({ default: (await import("./pages/ExperimentsPage")).ExperimentsPage }));

export function App() {
  return (
    <AppShell>
      <Suspense fallback={<Page><LoadingBlock label="加载页面…" /></Page>}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/reports/:reportId" element={<QuickReportPage />} />
          <Route path="/artifacts/:artifactId" element={<ArtifactPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId" element={<ProjectRedirect />} />
          <Route path="/projects/:projectId/:section" element={<ProjectWorkspacePage />} />
          <Route path="/projects/:projectId/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/agent-runs/:runId" element={<AgentRunPage />} />
          <Route path="/runtime" element={<RuntimePage />} />
          <Route path="/experiments" element={<ExperimentsPage />} />
          <Route path="*" element={<Navigate replace to="/" />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}

function ProjectRedirect() {
  return <Navigate replace to="overview" relative="path" />;
}
