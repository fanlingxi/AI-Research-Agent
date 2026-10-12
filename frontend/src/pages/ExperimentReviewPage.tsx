import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useSearchParams } from "react-router-dom";
import remarkGfm from "remark-gfm";

import { EmptyBlock, ErrorBlock, LoadingBlock, Notice } from "../components/AsyncState";
import { Button, buttonVariants } from "../components/ui/button";
import { Card, CardContent } from "../components/ui/card";
import { Field, Input, Select } from "../components/ui/field";
import { Disclosure, Page, PageHeader } from "../components/ui/page";
import { api, type AgreementMetric, type ReviewDetail, type ReviewVerdict } from "../lib/api";
import { cn } from "../lib/utils";

function Choice({ label, value, onChange, yes = "是", no = "否" }: {
  label: string; value: boolean | null; onChange: (value: boolean) => void; yes?: string; no?: string;
}) {
  return <div role="group" aria-label={label} className="space-y-2">
    <p className="text-sm leading-6 text-ink">{label}</p>
    <div className="flex gap-2">
      <Button size="sm" variant={value === true ? "default" : "outline"} aria-pressed={value === true} onClick={() => onChange(true)}>{yes}</Button>
      <Button size="sm" variant={value === false ? "default" : "outline"} aria-pressed={value === false} onClick={() => onChange(false)}>{no}</Button>
      {value === null && <span className="self-center text-xs text-muted-ink">待判断</span>}
    </div>
  </div>;
}

function ReviewForm({ detail, onSave, onSkip, onBusy }: {
  detail: ReviewDetail; onSave: (value: ReviewVerdict) => Promise<void>; onSkip: () => void; onBusy: (busy: boolean) => void;
}) {
  const { packet, human } = detail;
  const [covered, setCovered] = useState<Record<string, boolean | null>>(() => Object.fromEntries(packet.required_conclusions.map((c) => [c.id, human?.covered[c.id] ?? null])));
  const [violated, setViolated] = useState<(boolean | null)[]>(() => packet.forbidden_conclusions.map((_, i) => human?.violated[i] ?? null));
  const [outcome, setOutcome] = useState<boolean | null>(human?.outcome_ok ?? null);
  const [note, setNote] = useState(human?.note ?? "");
  const [correction, setCorrection] = useState(human?.corrected_note ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const locked = useRef(false);
  const complete = outcome !== null && Object.values(covered).every((v) => v !== null) && violated.every((v) => v !== null);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, []);

  async function submit() {
    if (locked.current) return;
    if (!complete) { setError(new Error("请完成每条结论和整体处理方式的判断。")); return; }
    locked.current = true;
    setSaving(true); onBusy(true); setError(null);
    try {
      await onSave({ covered: covered as Record<string, boolean>, violated: violated as boolean[], outcome_ok: outcome!, note, ...(correction ? { corrected_note: correction } : {}) });
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error("提交失败，请重试。"));
    } finally {
      locked.current = false;
      setSaving(false); onBusy(false);
    }
  }

  useEffect(() => {
    function keydown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (event.isComposing || event.repeat || event.ctrlKey || event.altKey || event.metaKey || target?.closest("input, textarea, select, [contenteditable=true]") || locked.current) return;
      const key = event.key.toLowerCase();
      const index = /^[1-9]$/.test(key) ? Number(key) - 1 : -1;
      if (index >= 0 && index < packet.required_conclusions.length + violated.length) {
        event.preventDefault();
        if (index < packet.required_conclusions.length) {
          const id = packet.required_conclusions[index].id;
          setCovered((values) => ({ ...values, [id]: !values[id] }));
        } else {
          const at = index - packet.required_conclusions.length;
          setViolated((values) => values.map((value, i) => i === at ? !value : value));
        }
      } else if (key === "y" || key === "n") {
        event.preventDefault(); setOutcome(key === "y");
      } else if (key === "enter") {
        event.preventDefault(); void submit();
      } else if (key === "arrowright") {
        event.preventDefault(); onSkip();
      }
    }
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  });

  return <section aria-label="当前核验题目" className="space-y-4">
    <div className="space-y-2">
      <h2 ref={heading} tabIndex={-1} className="break-words text-lg font-semibold text-ink outline-none">{packet.question}</h2>
      <p className="text-sm leading-6 text-ink-soft"><strong>期望处理方式：</strong>{packet.outcome_guide[packet.expected_outcome] ?? "未提供说明，请检查题目标签"}</p>
      {packet.missing_information.length > 0 && <p className="text-sm text-muted-ink">缺失信息：{packet.missing_information.join("；")}</p>}
      <p className="break-all text-xs text-muted-ink">{detail.key} · {human ? "已人工标注，可修改后重新提交" : "先独立判断，提交后显示 AI 对照"}</p>
    </div>
    <div className="grid min-w-0 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(300px,0.75fr)]">
      <Card className="min-w-0">
        <CardContent className="p-4">
          <h3 className="mb-3 font-semibold text-ink">报告正文</h3>
          <div tabIndex={0} role="region" aria-label="报告正文" className="prose-compact max-h-[55vh] overflow-auto overscroll-contain break-words [overflow-wrap:anywhere] lg:max-h-[70vh]">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{packet.report}</ReactMarkdown>
          </div>
        </CardContent>
      </Card>
      <Card className="min-w-0">
        <CardContent className="p-4">
          <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
            <fieldset disabled={saving} className="min-w-0 space-y-5">
              <legend className="mb-3 font-semibold text-ink">人工打分</legend>
              {packet.required_conclusions.map((c, i) => <Choice key={c.id} label={`${i + 1}. 必答：${c.statement}`} value={covered[c.id]} onChange={(value) => setCovered((values) => ({ ...values, [c.id]: value }))} yes="已覆盖" no="未覆盖" />)}
              {packet.forbidden_conclusions.map((statement, i) => <Choice key={i} label={`${packet.required_conclusions.length + i + 1}. 禁止：${statement}（报告是否断言了它）`} value={violated[i]} onChange={(value) => setViolated((values) => values.map((v, at) => at === i ? value : v))} yes="已断言" no="未断言" />)}
              <Choice label="整体处理方式是否正确" value={outcome} onChange={setOutcome} />
              <Field label="一句备注（可选）"><Input maxLength={4000} value={note} onChange={(event) => setNote(event.target.value)} placeholder="判断理由或发现的问题" /></Field>
              <Disclosure summary="标签纠正备注（可选）">
                <Field label="期望标签需要怎样修正"><Input maxLength={4000} value={correction} onChange={(event) => setCorrection(event.target.value)} /></Field>
              </Disclosure>
              <p className="text-xs leading-5 text-muted-ink">数字键 1–9 切换对应结论；Y / N 判断整体；Enter 提交；→ 跳过。输入备注时不触发快捷键。</p>
              <div className="flex flex-wrap gap-2">
                <Button type="submit" disabled={!complete || saving}>{saving ? "正在保存…" : "提交并下一题"}</Button>
                <Button variant="outline" onClick={onSkip}>跳过</Button>
              </div>
            </fieldset>
            {error && <div role="alert" className="mt-3 rounded-lg bg-danger-soft p-2 text-sm text-ink">{error.message}</div>}
          </form>
        </CardContent>
      </Card>
    </div>
  </section>;
}

function Comparison({ detail }: { detail: ReviewDetail }) {
  if (!detail.human) return null;
  const human = detail.human;
  function line(label: string, expected: boolean, actual: boolean, yes: string, no: string) {
    return <li key={label} className={cn("rounded-md p-2 text-sm text-ink-soft", expected !== actual && "bg-warning-soft text-ink ring-1 ring-inset ring-line-strong")}>
      {label}：人工「{expected ? yes : no}」 · AI「{actual ? yes : no}」{expected !== actual && <strong> · 不一致</strong>}
    </li>;
  }
  return <section aria-label="AI 裁判对照" className="space-y-3 rounded-xl border bg-panel p-4">
    <h2 className="font-semibold text-ink">已提交 · AI 裁判对照</h2>
    <p className="break-words text-sm text-muted-ink">{detail.packet.question} · {detail.key}</p>
    {!detail.ai_judges ? <LoadingBlock label="正在读取 AI 对照…" /> : !Object.keys(detail.ai_judges).length ? <p className="text-sm text-muted-ink">这项报告还没有 AI 裁判记录。</p> : Object.entries(detail.ai_judges).map(([name, ai]) => <Disclosure key={name} defaultOpen summary={`${name} 裁判`}>
      <ul className="space-y-1">
        {line("整体处理", human.outcome_ok, ai.outcome_ok, "正确", "不正确")}
        {detail.packet.required_conclusions.map((c) => line(c.statement, human.covered[c.id], ai.covered[c.id], "已覆盖", "未覆盖"))}
        {detail.packet.forbidden_conclusions.map((statement, i) => line(`禁止：${statement}`, human.violated[i], ai.violated[i], "已断言", "未断言"))}
      </ul>
      {ai.note && <p className="mt-2 break-words text-sm text-muted-ink">AI 备注：{ai.note}</p>}
    </Disclosure>)}
    {human.note && <p className="break-words text-sm text-ink-soft">人工备注：{human.note}</p>}
    {human.corrected_note && <p className="break-words text-sm text-ink-soft">标签纠正备注：{human.corrected_note}</p>}
  </section>;
}

const metricText = (metric: AgreementMetric) => metric.rate === null ? "暂无配对" : `${(metric.rate * 100).toFixed(1)}%（${metric.matched}/${metric.total}）`;

function Agreement({ run }: { run: string }) {
  const query = useQuery({ queryKey: ["review-agreement", run], queryFn: () => api.reviewAgreement(run) });
  return <Card><CardContent className="space-y-3 p-4">
    <h2 className="font-semibold text-ink">人工与 AI 的一致率</h2>
    <p className="text-xs leading-5 text-muted-ink">仅统计双方都已评分的核验项；结论逐条计数。分歧优先抽样，结果不代表全量准确率。期望标签来自 AI 共识，人工评分不等于标签已获验证。</p>
    {query.isPending && <LoadingBlock label="正在汇总一致率…" />}
    {query.isError && <ErrorBlock error={query.error} onRetry={() => void query.refetch()} />}
    {query.data && <>
      <p className="text-sm text-ink-soft">已人工标注 {query.data.human_count} 项</p>
      <div className="grid gap-3 md:grid-cols-3">{Object.entries(query.data.judges).map(([name, score]) => <div key={name} className="min-w-0 space-y-2 rounded-lg border p-3 text-sm text-ink-soft">
        <h3 className="break-all font-medium text-ink">{name} · 配对 {score.paired_count} 项</h3>
        <p>处理方式：{metricText(score.outcome_ok)}</p><p>必答结论：{metricText(score.covered)}</p><p>禁止结论：{metricText(score.violated)}</p>
        {!!Object.keys(score.covered_by_id).length && <Disclosure summary="各必答结论一致率">{Object.entries(score.covered_by_id).map(([id, metric]) => <p className="break-all" key={id}>{id}：{metricText(metric)}</p>)}</Disclosure>}
      </div>)}</div>
      {!Object.keys(query.data.judges).length && <p className="text-sm text-muted-ink">暂无 AI 裁判记录。</p>}
    </>}
  </CardContent></Card>;
}

function ReviewSession({ run, onBusy }: { run: string; onBusy: (busy: boolean) => void }) {
  const client = useQueryClient();
  const items = useQuery({ queryKey: ["review-items", run], queryFn: () => api.reviewItems(run) });
  const [selected, setSelected] = useState<string | null | undefined>(undefined);
  const [skipped, setSkipped] = useState<string[]>([]);
  const [lastKey, setLastKey] = useState("");
  const [busy, setBusy] = useState(false);
  const active = selected === undefined ? items.data?.items.find((item) => !item.human_reviewed)?.key : selected;
  const detail = useQuery({ queryKey: ["review-detail", run, active], queryFn: () => api.reviewItem(run, active!), enabled: Boolean(active) });
  const comparisonKey = lastKey || (detail.data?.human ? active : "");
  const comparison = useQuery({ queryKey: ["review-detail", run, comparisonKey], queryFn: () => api.reviewItem(run, comparisonKey!), enabled: Boolean(comparisonKey) });
  function next(exclude: string[]) {
    return items.data?.items.find((item) => !item.human_reviewed && !exclude.includes(item.key))?.key ?? null;
  }
  function skip() {
    if (!active) return;
    const excluded = [...skipped, active];
    setSkipped(excluded); setSelected(next(excluded));
  }
  async function save(value: ReviewVerdict) {
    if (!active) return;
    const saved = await api.reviewHuman(run, active, value);
    const nextKey = next([...skipped, active]);
    client.setQueryData(["review-detail", run, active], { ...detail.data!, human: saved.human });
    client.setQueryData(["review-items", run], (old: typeof items.data) => old && ({ ...old,
      human_count: old.human_count + (old.items.find((item) => item.key === active)?.human_reviewed ? 0 : 1),
      items: old.items.map((item) => item.key === active ? { ...item, human_reviewed: true } : item),
    }));
    setLastKey(active); setSelected(nextKey);
    for (const key of [["review-detail", run, active], ["review-items", run], ["review-agreement", run], ["review-runs"]]) {
      void client.invalidateQueries({ queryKey: key });
    }
  }
  return <div className="space-y-5">
    {items.isPending && <LoadingBlock label="正在读取核验项…" />}
    {items.isError && <ErrorBlock error={items.error} onRetry={() => void items.refetch()} />}
    {items.data && <>
      <div className="flex min-w-0 flex-wrap items-center gap-3">
        <p role="status" aria-label="核验进度" className="text-sm font-medium text-ink">已标 {items.data.human_count} / {items.data.total} 项</p>
        <Select aria-label="核验项" className="w-full sm:w-auto sm:max-w-lg" disabled={busy} value={active ?? ""} onChange={(event) => { setSelected(event.target.value || null); setLastKey(""); }}>
          <option value="">选择核验项或查看汇总</option>
          {items.data.items.map((item) => <option key={item.key} value={item.key}>{item.key} · {item.human_reviewed ? "已标" : "待标"} · {item.ai_agree === null ? "裁判不足两位" : item.ai_agree ? "AI 一致" : "AI 有分歧"}</option>)}
        </Select>
      </div>
      {active && detail.isPending && <LoadingBlock label="正在读取题目与报告…" />}
      {detail.isError && <ErrorBlock error={detail.error} onRetry={() => void detail.refetch()} />}
      {active && detail.data && <ReviewForm key={active} detail={detail.data} onSave={save} onSkip={skip} onBusy={(value) => { setBusy(value); onBusy(value); }} />}
      {!active && <EmptyBlock title={items.data.human_count === items.data.total ? "本次运行已全部标注" : "本轮核验已结束"}>
        {skipped.length > 0 ? <Button variant="outline" onClick={() => { setSkipped([]); setSelected(undefined); }}>继续核验跳过的题目</Button> : "可从上方选择已标注项查看对照或修改判断。"}
      </EmptyBlock>}
      {comparisonKey && comparison.isError && <ErrorBlock title="人工已保存，AI 对照读取失败" error={comparison.error} onRetry={() => void comparison.refetch()} />}
      {comparison.data?.human && <Comparison detail={comparison.data} />}
    </>}
    <Agreement run={run} />
  </div>;
}

export function ExperimentReviewPage() {
  const [params, setParams] = useSearchParams();
  const run = params.get("run") ?? "";
  const [busy, setBusy] = useState(false);
  const runs = useQuery({ queryKey: ["review-runs"], queryFn: api.reviewRuns });
  return <Page width="wide" className="space-y-5">
    <PageHeader title="人工核验" description="阅读真实评测报告，独立判断每条结论。优先检查 AI 裁判有分歧的报告。" actions={<Link className={buttonVariants({ variant: "outline" })} to="/experiments">返回实验对比</Link>} />
    <Notice>期望标签来自 AI 共识标注；你的判断将单独保存为人工标签，仅用于评测核验。</Notice>
    {runs.isPending && <LoadingBlock label="正在读取评测运行…" />}
    {runs.isError && <ErrorBlock error={runs.error} onRetry={() => void runs.refetch()} />}
    {runs.data && <Field label="选择评测运行">
      <Select disabled={busy} value={run} onChange={(event) => setParams(event.target.value ? { run: event.target.value } : {})}>
        <option value="">请选择运行</option>
        {runs.data.runs.map((item) => <option key={item.run_ref} value={item.run_ref}>{item.dataset} / {item.name} · {item.question_count} 题 / {item.item_count} 项 · 已标 {item.human_count} · AI 裁判 {item.ai_judge_count} 位</option>)}
      </Select>
    </Field>}
    {runs.data && !runs.data.runs.length && <EmptyBlock title="尚无可核验的运行">归档包含题目与报告后，可在这里开始核验。</EmptyBlock>}
    {run && <ReviewSession key={run} run={run} onBusy={setBusy} />}
  </Page>;
}
