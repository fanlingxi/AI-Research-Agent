import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";

const DashboardPage = lazy(async () => ({ default: (await import("./pages/DashboardPage")).DashboardPage }));
const ProjectWorkspacePage = lazy(async () => ({ default: (await import("./pages/ProjectWorkspacePage")).ProjectWorkspacePage }));
const TaskDetailPage = lazy(async () => ({ default: (await import("./pages/TaskDetailPage")).TaskDetailPage }));
const AgentRunPage = lazy(async () => ({ default: (await import("./pages/AgentRunPage")).AgentRunPage }));
const ArtifactPage = lazy(async () => ({ default: (await import("./pages/ArtifactPage")).ArtifactPage }));
const ReviewPage = lazy(async () => ({ default: (await import("./pages/ReviewPage")).ReviewPage }));

export function App() {
  return (
    <AppShell>
      <Suspense fallback={<div className="p-6 text-sm text-muted-ink">加载工作区…</div>}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/projects/:projectId/:section" element={<ProjectWorkspacePage />} />
          <Route path="/projects/:projectId/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/agent-runs/:runId" element={<AgentRunPage />} />
          <Route path="/artifacts/:artifactId" element={<ArtifactPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="*" element={<Navigate replace to="/" />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}
