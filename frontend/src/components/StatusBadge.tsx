import { statusLabel } from "../lib/labels";
import { cn } from "../lib/utils";
import { Badge, type BadgeTone } from "./ui/badge";

const TONES: Record<string, BadgeTone> = {
  active: "success",
  ready: "success",
  accepted: "success",
  approved: "success",
  completed: "success",
  complete: "success",
  committed: "success",
  published: "success",
  resolved: "success",
  running: "info",
  queued: "info",
  preparing: "info",
  validating: "info",
  publishing: "info",
  in_progress: "info",
  target_created: "info",
  proposed: "warning",
  needs_review: "warning",
  draft: "warning",
  pending: "warning",
  paused: "warning",
  quality_failed: "warning",
  blocked: "warning",
  deferred: "neutral",
  backlog: "neutral",
  failed: "danger",
  rejected: "danger",
  stale_context: "danger",
  unresolved: "danger",
  cancelled: "neutral",
  archived: "neutral",
  superseded: "neutral",
};

const DOT: Partial<Record<BadgeTone, string>> = {
  success: "bg-emerald-500",
  info: "bg-sky-500",
  warning: "bg-amber-500",
  danger: "bg-red-500",
  neutral: "bg-slate-400",
};

const ACTIVE = new Set(["running", "preparing", "validating", "publishing", "in_progress"]);

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const tone = TONES[status] ?? "neutral";
  return (
    <Badge tone={tone} className={className} data-status={status}>
      <span aria-hidden="true" className={cn("size-1.5 rounded-full", DOT[tone], ACTIVE.has(status) && "animate-pulse")} />
      {statusLabel(status)}
    </Badge>
  );
}
