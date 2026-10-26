import type { ContextSnapshotSummary } from "./api";

export interface SnapshotNotice {
  key: string;
  level: "warning" | "info";
  title: string;
  body: string;
}

const RETRY_HINT = "如回答不全，可重新冻结快照后再运行。";

const SELECTION_FALLBACK: Record<string, Omit<SnapshotNotice, "key">> = {
  transient_failure: {
    level: "warning",
    title: "证据选材未完成",
    body: "选材模型连接失败，本次按相似度顺序选取证据，涉及多篇资料的问题可能覆盖不全。",
  },
  invalid_output: {
    level: "warning",
    title: "证据选材未完成",
    body: "选材模型返回的结果不符合格式要求，本次按相似度顺序选取证据，回答可能覆盖不全。",
  },
  inputs_changed: {
    level: "warning",
    title: "证据选材结果已作废",
    body: "冻结快照期间知识库发生了变化，选材结果不再适用，本次按相似度顺序选取证据。",
  },
  no_anchored_support: {
    level: "info",
    title: "未找到直接对应问题的段落",
    body: "选材没有在候选资料中找到直接回答问题的原文，已按相似度顺序选取证据。如果问题超出了项目资料范围，这是正常结果。",
  },
  unavailable: {
    level: "info",
    title: "未进行证据选材",
    body: "没有候选证据或未配置选材模型，本次按相似度顺序选取证据。",
  },
  other: {
    level: "warning",
    title: "证据选材未完成",
    body: "证据选材没有成功，本次按相似度顺序选取证据，回答可能覆盖不全。",
  },
};

const PROFILE_FALLBACK: Record<string, string> = {
  no_explicit_knowledge_scope: "项目尚未设定知识范围，标准配置需要明确的资料范围。",
  vector_unavailable: "向量检索服务不可用，已自动改用经典配置。",
  legacy_snapshot: "快照为旧格式，只能使用经典配置。",
};

const BUDGET_NOTICES = [
  "Lower-ranked Claim bundles were omitted to respect the budget.",
  "Claim bundles that did not fit were omitted to respect the budget.",
];

/** Plain-language notices about how this snapshot's evidence was chosen; raw codes stay in details. */
export function snapshotNotices(snapshot: ContextSnapshotSummary): SnapshotNotice[] {
  const notices: SnapshotNotice[] = [];
  const selection = snapshot.evidence_selection;
  if (selection?.status === "fallback") {
    const category = selection.reason_category ?? "other";
    const template = SELECTION_FALLBACK[category] ?? SELECTION_FALLBACK.other;
    const body = category === "transient_failure" && selection.retried
      ? "选材模型连接失败（已自动重试 1 次），本次按相似度顺序选取证据，涉及多篇资料的问题可能覆盖不全。"
      : template.body;
    notices.push({ key: "selection", ...template, body: template.level === "warning" ? body + RETRY_HINT : body });
  }
  const raw = snapshot.diagnostics.notices ?? [];
  const profileReason = raw.find((notice) => notice.startsWith("profile_fallback_reason:"))?.split(":")[1];
  if (profileReason) {
    notices.push({
      key: "profile",
      level: profileReason === "no_explicit_knowledge_scope" ? "info" : "warning",
      title: "本次使用经典研究配置",
      body: PROFILE_FALLBACK[profileReason] ?? "标准配置不可用，已自动改用经典配置。",
    });
  }
  if (raw.some((notice) => BUDGET_NOTICES.includes(notice))) {
    const dropped = snapshot.diagnostics.dropped_for_budget;
    notices.push({
      key: "budget",
      level: "info",
      title: "部分证据未纳入",
      body: typeof dropped === "number" && dropped > 0
        ? `受上下文预算限制，另有 ${dropped} 个排名较低的证据包未纳入快照。`
        : "受上下文预算限制，部分排名较低的证据包未纳入快照。",
    });
  }
  return notices;
}
