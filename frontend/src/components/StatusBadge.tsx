import { Badge } from "./ui/badge";

const TONES: Record<string, "neutral" | "success" | "info" | "warning" | "danger" | "brand"> = {
  active: "success",
  ready: "success",
  accepted: "success",
  approved: "success",
  completed: "success",
  committed: "success",
  running: "info",
  queued: "info",
  preparing: "info",
  validating: "info",
  in_progress: "info",
  proposed: "warning",
  needs_review: "warning",
  paused: "warning",
  blocked: "warning",
  draft: "neutral",
  backlog: "neutral",
  failed: "danger",
  rejected: "danger",
  cancelled: "neutral",
  archived: "neutral",
  superseded: "neutral",
  stale_context: "danger",
};

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={TONES[status] ?? "neutral"}>{status.replaceAll("_", " ")}</Badge>;
}
