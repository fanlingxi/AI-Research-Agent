import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";

const DashboardPage = lazy(async () => ({ default: (await import("./pages/DashboardPage")).DashboardPage }));
const ProjectWorkspacePage = lazy(async () => ({ default: (await import("./pages/ProjectWorkspacePage")).ProjectWorkspacePage }));
const TaskDetailPage = lazy(async () => ({ default: (await import("./pages/TaskDetailPage")).TaskDetailPage }));
const AgentRunPage = lazy(async () => ({ default: (await import("./pages/AgentRunPage")).AgentRunPage }));
const ArtifactPage = lazy(async () => ({ default: (await import("./pages/ArtifactPage")).ArtifactPage }));
const ReviewPage = lazy(async () => ({ default: (await import("./pages/ReviewPage")).ReviewPage }));
const KnowledgePage = lazy(async () => ({ default: (await import("./pages/KnowledgePage")).KnowledgePage }));
const ReportsPage = lazy(async () => ({ default: (await import("./pages/ReportsPage")).ReportsPage }));
const RuntimePage = lazy(async () => ({ default: (await import("./pages/RuntimePage")).RuntimePage }));

export function App() {
  return (
    <AppShell>
      <Suspense fallback={<div className="p-6 text-sm text-muted-ink">加载工作区…</div>}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/projects/:projectId/:section" element={<ProjectWorkspacePage />} />
          <Route path="/projects/:projectId/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/agent-runs/:runId" element={<AgentRunPage />} />
          <Route path="/artifacts/:artifactId" element={<ArtifactPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/runtime" element={<RuntimePage />} />
          <Route path="*" element={<Navigate replace to="/" />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}
