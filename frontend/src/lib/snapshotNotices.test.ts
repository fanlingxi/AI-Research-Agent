import { describe, expect, it } from "vitest";

import type { ContextSnapshotSummary, EvidenceSelection } from "./api";
import { snapshotNotices } from "./snapshotNotices";

function summary(selection: EvidenceSelection | null, notices: string[] = [], dropped?: number): ContextSnapshotSummary {
  return {
    id: "snapshot-1", persisted: true, project_id: "p", task_id: "t", project_revision: 1, task_revision: 1,
    builder_version: "b", package_schema_version: "1.2", package_sha256: "x", token_budget: 16000, used_tokens: 14000,
    collection_scopes: ["papers"], item_counts: { knowledge_claim_bundles: 4, knowledge_sources: 1 },
    diagnostics: { knowledge_coverage: "available", selected_claim_bundles: 4, dropped_for_budget: dropped, notices },
    evidence_selection: selection, created_at: "2026-10-25T00:00:00Z",
  };
}

const fallback = (reason_category: EvidenceSelection["reason_category"], retried = false): EvidenceSelection => ({
  strategy: "coverage-v2", status: "fallback", reason_category, model_calls: retried ? 2 : 1, retried,
});

describe("snapshotNotices", () => {
  it("is silent when selection succeeded or was not used", () => {
    expect(snapshotNotices(summary({ ...fallback(null), status: "ranked" }))).toEqual([]);
    expect(snapshotNotices(summary(null))).toEqual([]);
  });

  it("warns about a transient selection failure and mentions the retry", () => {
    const [notice] = snapshotNotices(summary(fallback("transient_failure", true)));
    expect(notice.level).toBe("warning");
    expect(notice.body).toContain("已自动重试 1 次");
    expect(notice.body).toContain("重新冻结快照");
  });

  it("treats missing support as expected information, not a warning", () => {
    const [notice] = snapshotNotices(summary(fallback("no_anchored_support")));
    expect(notice.level).toBe("info");
    expect(notice.body).not.toContain("重新冻结快照");
  });

  it("explains profile fallback and budget trimming in Chinese", () => {
    const notices = snapshotNotices(summary(null, [
      "vector_unavailable",
      "research_profile:classic",
      "profile_fallback_reason:vector_unavailable",
      "Lower-ranked Claim bundles were omitted to respect the budget.",
    ], 36));
    expect(notices.map((notice) => notice.key)).toEqual(["profile", "budget"]);
    expect(notices[0]).toMatchObject({ level: "warning", body: expect.stringContaining("向量检索服务不可用") });
    expect(notices[1].body).toContain("36 个");
    const packed = snapshotNotices(summary(null, ["Claim bundles that did not fit were omitted to respect the budget."], 5));
    expect(packed).toEqual([expect.objectContaining({ key: "budget", body: expect.stringContaining("5 个") })]);
  });
});
