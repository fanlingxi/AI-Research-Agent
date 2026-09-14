import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, type WorkspaceTask } from "../lib/api";
import { ErrorBlock } from "./AsyncState";
import { Button } from "./ui/button";

export function TaskGoalEditor({ task }: { task: WorkspaceTask }) {
  const client = useQueryClient();
  const [goal, setGoal] = useState(task.goal);
  const save = useMutation({ mutationFn: () => api.updateTaskGoal(task.id, task.revision, goal.trim()), onSuccess: () => {
    for (const key of ["task", "tasks", "context-preview"]) void client.invalidateQueries({ queryKey: [key] });
  } });
  return <details className="mt-4 rounded-lg border bg-panel p-4"><summary className="cursor-pointer text-sm font-medium text-brand">修订任务目标</summary>
    <form className="mt-3 space-y-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <p className="text-sm text-muted-ink">保存后用于下一次创建的快照；已有报告和快照仍保留原目标。</p>
      <label className="block text-sm">任务目标<textarea aria-label="任务目标" className="mt-1 w-full rounded-lg border bg-white p-3 text-sm" rows={5} required maxLength={4000} value={goal} disabled={save.isPending} onChange={(e) => setGoal(e.target.value)} /></label>
      {save.error && <ErrorBlock error={save.error} />}
      {save.error instanceof ApiError && save.error.status === 409 ? <Button type="button" variant="outline" onClick={() => void client.invalidateQueries({ queryKey: ["task", task.id] })}>重新读取最新目标</Button> : <Button type="submit" disabled={save.isPending || !goal.trim() || goal.trim() === task.goal}>保存任务目标</Button>}
    </form>
  </details>;
}
