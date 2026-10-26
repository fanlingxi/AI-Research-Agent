import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

import { api } from "../lib/api";
import { snapshotNotices } from "../lib/snapshotNotices";
import { Notice } from "./AsyncState";

/** Warnings about a frozen snapshot's evidence selection, for pages that only know its ID. */
export function SnapshotWarnings({ snapshotId, className }: { snapshotId?: string; className?: string }) {
  const snapshot = useQuery({
    queryKey: ["snapshot", snapshotId],
    queryFn: () => api.snapshot(snapshotId!),
    enabled: Boolean(snapshotId),
    staleTime: Infinity,
  });
  // Advisory only: a missing summary must never block reading the run or report.
  const warnings = snapshot.data ? snapshotNotices(snapshot.data).filter((notice) => notice.level === "warning") : [];
  if (!warnings.length) return null;
  return (
    <div className={className}>
      {warnings.map((notice) => (
        <Notice key={notice.key} tone="warning" icon={<AlertTriangle size={15} />} title={notice.title}>{notice.body}</Notice>
      ))}
    </div>
  );
}
