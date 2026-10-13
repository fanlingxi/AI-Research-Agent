import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, type FollowUpOf, type ResearchCommandInput, type WorkspaceTask } from "../lib/api";
import { ErrorBlock, Notice } from "./AsyncState";
import { splitTitle } from "./report/ReportMarkdown";
import { StatusBadge } from "./StatusBadge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Field, Textarea } from "./ui/field";

export function FollowUpLinks({ runId, task }: { runId: string; task?: WorkspaceTask }) {
  const value = task?.metadata.follow_up_of;
  const parent = value && typeof value === "object" && "run_id" in value && typeof value.run_id === "string"
    ? value as FollowUpOf : undefined;
  const parentOutput = useQuery({ queryKey: ["output", parent?.run_id], queryFn: () => api.output(parent!.run_id), enabled: Boolean(parent && !parent.artifact_id) });
  const outputArtifactId = parentOutput.data?.structured.artifact_id;
  const parentArtifactId = parent?.artifact_id ?? (typeof outputArtifactId === "string" ? outputArtifactId : undefined);
  const parentContent = useQuery({ queryKey: ["artifact-content", parentArtifactId], queryFn: () => api.artifactContent(parentArtifactId!), enabled: Boolean(parent && parentArtifactId) });
  const parentTask = useQuery({ queryKey: ["task", parent?.parent_task_id], queryFn: () => api.task(parent!.parent_task_id!), enabled: Boolean(parent?.parent_task_id) });
  const children = useQuery({ queryKey: ["follow-ups", runId], queryFn: () => api.followUps(runId), refetchInterval: 5000 });
  const parentTitle = splitTitle(parentContent.data?.rendered_markdown ?? "").title ?? parentTask.data?.title ?? "父报告";
  return <>
    {parent ? <p className="break-words text-sm text-muted-ink">追问自：<Link className="text-brand hover:underline" to={parentArtifactId ? `/artifacts/${parentArtifactId}` : `/agent-runs/${parent.run_id}`}>{parentTitle}</Link></p> : null}
    {children.error ? <ErrorBlock error={children.error} title="无法读取后续追问" /> : null}
    {children.data?.length ? <Notice tone="info" title="后续追问">
      <ul className="space-y-2">
        {children.data.map(({ task: child, latest_run: run, artifact }) => <li key={child.id} className="flex flex-wrap items-center gap-2">
          <Link className="min-w-0 break-words text-brand hover:underline" to={artifact ? `/artifacts/${artifact.id}` : run ? `/agent-runs/${run.id}` : `/projects/${child.project_id}/tasks/${child.id}`}>{child.title}</Link>
          <StatusBadge status={run?.status ?? child.status} />
        </li>)}
      </ul>
    </Notice> : null}
  </>;
}

type Pending = { key: string; payload: ResearchCommandInput; commandId?: string };
function readPending(storageKey: string): Pending | null {
  try { return JSON.parse(localStorage.getItem(storageKey) ?? "null") as Pending | null; }
  catch { return null; }
}

export function FollowUpPanel({ runId, artifactId, projectId }: { runId: string; artifactId: string; projectId: string }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const storageKey = `research-follow-up:${runId}`;
  const [pending, setPending] = useState(() => readPending(storageKey));
  const [question, setQuestion] = useState("");
  const [standalone, setStandalone] = useState(pending?.payload.instruction ?? "");
  const [hasDraft, setHasDraft] = useState(Boolean(pending));
  const submitLock = useRef(false);
  const draft = useMutation({
    mutationFn: () => api.followUpDraft(runId, question.trim()),
    onSuccess: (data) => { setStandalone(data.standalone_question); setHasDraft(true); },
  });
  const remember = (value: Pending) => {
    localStorage.setItem(storageKey, JSON.stringify(value));
    setPending(value);
  };
  const start = useMutation({
    mutationFn: (value: Pending) => api.submitResearchCommand(value.payload, value.key),
    onSuccess: (command, value) => {
      remember({ ...value, commandId: command.id });
      client.setQueryData(["research-command", command.id], command);
      void client.invalidateQueries({ queryKey: ["follow-ups", runId] });
    },
    onSettled: () => { submitLock.current = false; },
  });
  const command = useQuery({
    queryKey: ["research-command", pending?.commandId],
    queryFn: () => api.researchCommand(pending!.commandId!),
    enabled: Boolean(pending?.commandId),
    refetchInterval: (query) => ["failed", "completed"].includes(query.state.data?.status ?? "") || query.state.data?.target_route ? false : 1000,
  });
  const retry = useMutation({
    mutationFn: () => api.retryResearchCommand(pending!.commandId!),
    onSuccess: (data) => client.setQueryData(["research-command", data.id], data),
  });
  const current = command.data ?? start.data;
  useEffect(() => {
    if (!current?.target_route) return;
    localStorage.removeItem(storageKey);
    navigate(current.target_route);
  }, [current?.target_route, navigate, storageKey]);
  const locked = Boolean(pending?.commandId) || start.isPending;
  const error = draft.error ?? start.error ?? command.error ?? retry.error;
  return <Card aria-label="追问">
    <CardHeader><CardTitle title="追问" description="把想继续了解的内容整理成独立问题，在当前项目中开始新的研究。留空可获取推荐追问。" /></CardHeader>
    <CardContent className="space-y-4">
      <Field label="你的追问"><Textarea maxLength={3000} rows={3} value={question} disabled={locked || draft.isPending} onChange={(event) => { setQuestion(event.target.value); setStandalone(""); setHasDraft(false); draft.reset(); }} placeholder="例如：这个方法与另一篇论文相比有哪些局限？" /></Field>
      <Button variant="outline" disabled={locked || draft.isPending} onClick={() => draft.mutate()}>{draft.isPending ? "正在生成…" : "生成问题"}</Button>
      {draft.data?.fallback ? <p className="text-sm text-muted-ink" role="status">暂时无法使用模型，已使用基础模板；请补全论文或方法名后开始研究。</p> : null}
      {draft.data?.suggestions.length ? <div className="space-y-2"><p className="text-sm font-medium">推荐追问</p>{draft.data.suggestions.map((suggestion) => <button type="button" key={suggestion} disabled={locked} className="block w-full rounded-lg border bg-subtle p-3 text-left text-sm leading-6 text-ink hover:border-brand disabled:opacity-50" onClick={() => { setQuestion(suggestion.slice(0, 3000)); setStandalone(suggestion); }}>{suggestion}</button>)}</div> : null}
      {hasDraft || pending ? <>
        <Field label="独立研究问题" hint="请确认问题包含必要背景；可以直接修改。"><Textarea maxLength={4000} rows={4} value={standalone} disabled={locked || draft.isPending} onChange={(event) => setStandalone(event.target.value)} /></Field>
        <Button disabled={locked || draft.isPending || standalone.trim().length < 3} onClick={() => {
          if (submitLock.current) return;
          submitLock.current = true;
          const payload: ResearchCommandInput = {
            mode: "project_run", instruction: standalone.trim(), project_id: projectId,
            task: { kind: "new", priority: "normal" }, follow_up_of: { run_id: runId, artifact_id: artifactId },
            create_memory_proposal: false, max_steps: 10, max_tool_calls: 3,
          };
          const value = pending && JSON.stringify(pending.payload) === JSON.stringify(payload) ? pending : { key: crypto.randomUUID(), payload };
          remember(value);
          start.mutate(value);
        }}>{start.isPending ? "正在提交…" : "开始研究"}</Button>
      </> : null}
      {current && !current.target_route ? <Notice tone={current.status === "failed" ? "danger" : "info"} title={current.status === "failed" ? "研究未能启动" : "正在准备新的研究…"}>
        {current.error ?? "指令已保存，准备完成后将自动打开新运行。"}
        {current.status === "failed" ? <div className="mt-2 flex flex-wrap gap-2">
          <Button variant="outline" disabled={retry.isPending} onClick={() => retry.mutate()}>重试启动</Button>
          <Button variant="ghost" disabled={retry.isPending} onClick={() => { localStorage.removeItem(storageKey); setPending(null); start.reset(); retry.reset(); }}>修改问题</Button>
        </div> : null}
      </Notice> : null}
      {error ? <ErrorBlock error={error} title="追问操作未完成" /> : null}
    </CardContent>
  </Card>;
}
