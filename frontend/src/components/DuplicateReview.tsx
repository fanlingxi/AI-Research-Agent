import { Check, Copy, ExternalLink, GitMerge, Plus, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { DuplicateGroup, EntityCandidate, MergeSuggestion } from "../lib/api";
import { documentTitle } from "../lib/hooks";
import { entityTypeLabel } from "../lib/labels";
import { cn } from "../lib/utils";
import { EmptyBlock } from "./AsyncState";
import { pageLabel } from "./report/SourceDrawer";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";

export function matchLabel(suggestion: MergeSuggestion) {
  return suggestion.match_kind === "exact" ? "同名" : `相似 ${Math.round(suggestion.similarity * 100)}%`;
}

/** Flag for a suggestion whose existing entity has a different (interchangeable) type. */
export function TypeDiffersBadge({ candidateType, suggestion }: { candidateType: string; suggestion: MergeSuggestion }) {
  if (!suggestion.type_differs) return null;
  return (
    <Badge tone="info" title={`候选标为「${entityTypeLabel(candidateType)}」，已有实体是「${entityTypeLabel(suggestion.type)}」。合并后保留已有实体的类型，请确认两者是同一个东西。`}>
      类型不同（{entityTypeLabel(candidateType)}/{entityTypeLabel(suggestion.type)}）
    </Badge>
  );
}

/** Ask before creating a same-name sense next to an existing entity. */
export function confirmApproveAsNew(name: string, suggestions: MergeSuggestion[]) {
  const exact = suggestions.find((item) => item.match_kind === "exact");
  if (!exact) return true;
  const kind = exact.type_differs ? `同名的${entityTypeLabel(exact.type)}实体` : "同名实体";
  return window.confirm(`知识库中已有${kind}「${exact.name}」。作为新实体批准会再建一个同名的独立条目，只有两者含义确实不同时才这样做。确定将「${name}」作为新实体批准吗？`);
}

/** Merge target picker shown on an entity candidate card. */
export function MergeSuggestions({ candidateId, candidateType, suggestions, value, onChange, disabled }: {
  candidateId: string;
  candidateType: string;
  suggestions: MergeSuggestion[];
  value: string;
  onChange: (entityId: string) => void;
  disabled?: boolean;
}) {
  const selected = suggestions.find((item) => item.entity_id === value);
  return (
    <fieldset className="mt-2.5 rounded-lg border border-amber-200 bg-warning-soft/60 px-3 py-2.5">
      <legend className="sr-only">选择合并目标</legend>
      <p className="flex items-center gap-1.5 text-xs font-medium text-amber-900"><Copy size={13} />可能重复 · 可合并到已有实体</p>
      <div className="mt-1.5 space-y-1">
        {suggestions.slice(0, 4).map((suggestion) => (
          <label className={cn("flex min-w-0 cursor-pointer flex-wrap items-center gap-x-2 gap-y-1 rounded-md px-1.5 py-1 text-[13px] hover:bg-panel/70", value === suggestion.entity_id && "bg-panel")} key={suggestion.entity_id}>
            <input checked={value === suggestion.entity_id} className="size-3.5 shrink-0 accent-[var(--color-brand)]" disabled={disabled} name={`merge-target-${candidateId}`} onChange={() => onChange(suggestion.entity_id)} type="radio" />
            <span className="min-w-[6rem] flex-1 truncate font-medium text-ink">{suggestion.name}</span>
            {suggestion.type_differs ? null : <span className="shrink-0 text-xs text-muted-ink">{entityTypeLabel(suggestion.type)}</span>}
            <TypeDiffersBadge candidateType={candidateType} suggestion={suggestion} />
            <Badge tone={suggestion.match_kind === "exact" ? "warning" : "neutral"}>{matchLabel(suggestion)}</Badge>
            <Link aria-label={`在知识地图中查看「${suggestion.name}」`} className="shrink-0 text-muted-ink hover:text-brand" target="_blank" to={`/knowledge/map?node=${encodeURIComponent(suggestion.entity_id)}`}><ExternalLink size={13} /></Link>
          </label>
        ))}
      </div>
      <p className="mt-1.5 text-xs leading-5 text-amber-900/80">合并后，这条候选的证据与原文出处会挂到所选实体上，不会新建条目；涉及它的关系也会连到该实体。{selected?.type_differs ? `所选实体类型为「${entityTypeLabel(selected.type)}」，合并后保留这个类型。` : ""}</p>
    </fieldset>
  );
}

function papersOf(candidates: EntityCandidate[]) {
  return new Set(candidates.map((item) => item.evidence.paper_id)).size;
}

function DuplicateGroupCard({ group, pending, titles, onMergeAll, onMerge, onApproveAsNew, onApproveAsCanonical, onReject }: {
  group: DuplicateGroup;
  pending: boolean;
  titles: Map<string, string>;
  onMergeAll: (candidateIds: string[], target: MergeSuggestion) => void;
  onMerge: (candidateId: string, target: MergeSuggestion) => void;
  onApproveAsNew: (candidate: EntityCandidate) => void;
  onApproveAsCanonical: (candidate: EntityCandidate, others: EntityCandidate[]) => void;
  onReject: (candidateId: string) => void;
}) {
  const { target, candidates } = group;
  const crossType = candidates.some((item) => item.type !== group.type);
  return (
    <article aria-label={`疑似重复：${group.name}`} className="rounded-xl border bg-panel p-4 shadow-[var(--shadow-card)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-[15px] font-semibold text-ink">{group.name}</p>
            <span className="text-xs text-muted-ink">{entityTypeLabel(group.type)}</span>
            <Badge tone="warning">{candidates.length} 条候选 · {papersOf(candidates)} 篇文献</Badge>
          </div>
          {target ? (
            <p className="mt-1 text-[13px] text-ink-soft">
              已有实体
              <Link className="mx-1 font-medium text-brand hover:underline" target="_blank" to={`/knowledge/map?node=${encodeURIComponent(target.entity_id)}`}>「{target.name}」</Link>
              <Badge tone={target.match_kind === "exact" ? "warning" : "neutral"}>{matchLabel(target)}</Badge>
              {crossType ? <Badge className="ml-1" tone="info">含类型不同的候选（{[...new Set(candidates.map((item) => entityTypeLabel(item.type)))].join("/")} → {entityTypeLabel(target.type)}）</Badge> : null}
            </p>
          ) : <p className="mt-1 text-[13px] text-ink-soft">知识库里还没有这个实体。选一条作为规范实体批准，其余候选会合并到它。</p>}
        </div>
        {target ? (
          <Button disabled={pending} size="sm" onClick={() => onMergeAll(candidates.map((item) => item.id), target)}>
            <GitMerge size={14} />全部合并到「{target.name}」
          </Button>
        ) : null}
      </div>
      <ul className="mt-3 divide-y rounded-lg border">
        {candidates.map((candidate) => (
          <li className="flex flex-wrap items-start gap-3 px-3 py-2.5" key={candidate.id}>
            <div className="min-w-0 flex-1">
              <p className="text-[13.5px] font-medium text-ink">{candidate.name}{candidate.type !== group.type ? <Badge className="ml-2 align-middle" tone="info">类型不同（{entityTypeLabel(candidate.type)}）</Badge> : null}<span className="ml-2 text-xs font-normal text-muted-ink">{documentTitle(null, candidate.evidence.paper_id, titles)} · {pageLabel(candidate.evidence.page_start, candidate.evidence.page_end)}</span></p>
              <p className="mt-0.5 line-clamp-2 text-xs leading-5 text-ink-soft">“{candidate.evidence.quote}”</p>
            </div>
            <div className="flex shrink-0 flex-wrap gap-1.5">
              <Button aria-label={`驳回：${candidate.name}`} disabled={pending} size="sm" variant="ghost" onClick={() => onReject(candidate.id)}><X size={14} />驳回</Button>
              {target ? (
                <>
                  <Button disabled={pending} size="sm" variant="ghost" onClick={() => onApproveAsNew(candidate)}><Plus size={14} />作为新实体</Button>
                  <Button aria-label={`合并：${candidate.name}`} disabled={pending} size="sm" variant="secondary" onClick={() => onMerge(candidate.id, target)}><GitMerge size={14} />合并</Button>
                </>
              ) : (
                <Button disabled={pending} size="sm" variant="secondary" onClick={() => onApproveAsCanonical(candidate, candidates.filter((item) => item.id !== candidate.id))}><Check size={14} />设为规范实体并合并其余</Button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </article>
  );
}

/** Groups safe to merge in one step: an existing entity with the same name and the same type. */
export function exactMergeGroups(groups: DuplicateGroup[]) {
  return groups.filter((group) => group.target?.match_kind === "exact" && group.candidates.every((item) => item.type === group.target?.type));
}

function MergeExactBar({ groups, pending, onConfirm }: { groups: DuplicateGroup[]; pending: boolean; onConfirm: () => void }) {
  const [open, setOpen] = useState(false);
  const count = groups.reduce((sum, group) => sum + group.candidates.length, 0);
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-warning-soft/60 px-3 py-2.5 text-[13px] text-amber-950">
      <span>{groups.length} 组共 {count} 条候选与已有实体同名且类型相同（例如重新解析或重复导入的资料）。</span>
      <Button disabled={pending} size="sm" onClick={() => setOpen(true)}><GitMerge size={14} />全部合并到同名实体</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="w-[min(560px,calc(100vw-2rem))]">
          <DialogTitle>合并 {groups.length} 组同名候选</DialogTitle>
          <DialogDescription>每组候选的证据和出处会挂到同名的已有实体上，不新建条目。名称相近或类型不同的组不在此列，仍需逐组处理。</DialogDescription>
          <ul className="scroll-thin mt-3 max-h-64 space-y-1 overflow-y-auto text-[13px]">
            {groups.map((group) => <li className="break-words" key={group.key}>{group.candidates.length} 条 →「{group.target?.name}」<span className="ml-1 text-xs text-muted-ink">{entityTypeLabel(group.type)}</span></li>)}
          </ul>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outline" onClick={() => setOpen(false)}>取消</Button>
            <Button disabled={pending} onClick={() => { setOpen(false); onConfirm(); }}>确认合并</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export function DuplicateGroupList({ groups, onMergeExact, ...handlers }: {
  groups: DuplicateGroup[];
  onMergeExact?: (groups: DuplicateGroup[]) => void;
  pending: boolean;
  titles: Map<string, string>;
  onMergeAll: (candidateIds: string[], target: MergeSuggestion) => void;
  onMerge: (candidateId: string, target: MergeSuggestion) => void;
  onApproveAsNew: (candidate: EntityCandidate) => void;
  onApproveAsCanonical: (candidate: EntityCandidate, others: EntityCandidate[]) => void;
  onReject: (candidateId: string) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  if (!groups.length) {
    return <EmptyBlock className="mt-4" icon={<Check size={19} />} title="没有疑似重复的实体候选">同一概念在不同文献中的候选都已处理，或者与已有实体都不重名。</EmptyBlock>;
  }
  const visible = showAll ? groups : groups.slice(0, 20);
  const exact = exactMergeGroups(groups);
  return (
    <div className="mt-4 space-y-3">
      {onMergeExact && exact.length > 1 ? <MergeExactBar groups={exact} pending={handlers.pending} onConfirm={() => onMergeExact(exact)} /> : null}
      <p className="px-1 text-[13px] text-muted-ink">同名或名称相近的实体候选会被归到一起；概念、方法、任务之间也会互相提示，并标出“类型不同”，合并后保留规范实体的类型。合并后同一概念只保留一个节点，各篇文献都会连到它。</p>
      {visible.map((group) => <DuplicateGroupCard group={group} key={group.key} {...handlers} />)}
      {groups.length > visible.length ? <Button size="sm" variant="outline" onClick={() => setShowAll(true)}>显示其余 {groups.length - visible.length} 组</Button> : null}
    </div>
  );
}
