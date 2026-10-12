import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PencilLine } from "lucide-react";
import { useState } from "react";
import { api, ApiError, type WorkspaceTask } from "../lib/api";
import { ErrorBlock } from "./AsyncState";
import { Button } from "./ui/button";
import { Textarea } from "./ui/field";

export function TaskGoalEditor({ task }: { task: WorkspaceTask }) {
  const client = useQueryClient();
  const [goal, setGoal] = useState(task.goal);
  const save = useMutation({ mutationFn: () => api.updateTaskGoal(task.id, task.revision, goal.trim()), onSuccess: () => {
    for (const key of ["task", "tasks", "context-preview"]) void client.invalidateQueries({ queryKey: [key] });
  } });
  return <details className="group mt-4 max-w-3xl rounded-lg border bg-panel">
    <summary className="flex list-none items-center gap-1.5 px-3.5 py-2.5 text-[13px] font-medium text-brand hover:bg-subtle"><PencilLine size={14} />修订任务目标</summary>
    <form className="space-y-3 border-t px-3.5 py-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <p className="text-[13px] text-muted-ink">新目标用于之后冻结的快照；已有的报告和快照保持原来的目标。</p>
      <label className="block text-[13px] font-medium text-ink-soft">任务目标<Textarea aria-label="任务目标" className="mt-1.5" rows={6} required maxLength={4000} value={goal} disabled={save.isPending} onChange={(e) => setGoal(e.target.value)} /></label>
      {save.error && <ErrorBlock error={save.error} />}
      {save.error instanceof ApiError && save.error.status === 409 ? <Button type="button" variant="outline" onClick={() => void client.invalidateQueries({ queryKey: ["task", task.id] })}>重新读取最新目标</Button> : <Button type="submit" disabled={save.isPending || !goal.trim() || goal.trim() === task.goal}>保存任务目标</Button>}
    </form>
  </details>;
}
