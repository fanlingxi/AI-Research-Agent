/** Chinese display labels for backend enum values. Unknown values fall back to the raw value. */

export const STATUS_LABELS: Record<string, string> = {
  active: "进行中",
  ready: "就绪",
  accepted: "已受理",
  approved: "已批准",
  completed: "已完成",
  complete: "已完成",
  committed: "已提交",
  published: "已发布",
  running: "运行中",
  queued: "排队中",
  preparing: "准备中",
  validating: "校验中",
  publishing: "发布中",
  in_progress: "进行中",
  target_created: "已创建",
  created: "已创建",
  proposed: "待审核",
  needs_review: "待审核",
  draft: "待审核",
  deferred: "已搁置",
  pending: "待处理",
  paused: "已暂停",
  blocked: "受阻",
  backlog: "待办",
  failed: "失败",
  interrupted: "已中断",
  quality_failed: "未通过质检",
  rejected: "已驳回",
  cancelled: "已取消",
  canceled: "已取消",
  archived: "已归档",
  superseded: "已被取代",
  stale_context: "上下文已过期",
  resolved: "已解决",
  unresolved: "未解决",
  missing: "记录缺失",
  not_attempted: "未尝试",
  online: "在线",
  offline: "离线",
};

export function statusLabel(status: string) {
  return STATUS_LABELS[status] ?? status.replaceAll("_", " ");
}

export const PRIORITY_LABELS: Record<string, string> = {
  low: "低",
  normal: "普通",
  high: "高",
  urgent: "紧急",
};

export const ENTITY_TYPE_LABELS: Record<string, string> = {
  Concept: "概念",
  Dataset: "数据集",
  Finding: "发现",
  Method: "方法",
  Metric: "指标",
  Paper: "论文",
  Task: "任务",
  Tool: "工具",
  Model: "模型",
  Person: "人物",
  Organization: "机构",
};

export const RELATION_TYPE_LABELS: Record<string, string> = {
  ADDRESSES: "关注",
  APPLIES_TO: "适用于",
  COMPARES_WITH: "对比",
  EVALUATES: "评估",
  HAS_LIMITATION: "存在局限",
  IMPROVES: "改进",
  PRESENTS: "提出",
  SUPPORTS: "支持",
  USES: "使用",
  RELATED_TO: "相关",
  EXTENDS: "扩展",
  PART_OF: "属于",
};

export function entityTypeLabel(type: string) {
  return ENTITY_TYPE_LABELS[type] ?? type;
}

export function relationTypeLabel(type: string) {
  return RELATION_TYPE_LABELS[type] ?? type.replaceAll("_", " ").toLowerCase();
}

/** Research agent workflow nodes, in the order they normally run. */
export const NODE_LABELS: Record<string, string> = {
  load_context: "读取冻结的上下文",
  task_analysis: "分析研究任务",
  constrained_plan: "制定研究计划",
  snapshot_tools: "查阅快照中的证据",
  research_draft: "撰写报告草稿",
  citation_validation: "校验引用",
  repair: "修正引用问题",
  complete: "已完成",
  queued: "排队等待",
};

export function nodeLabel(node: string | null | undefined) {
  if (!node) return "排队等待";
  return NODE_LABELS[node] ?? node.replaceAll("_", " ");
}

export const EVENT_LABELS: Record<string, string> = {
  run_created: "创建运行",
  run_queued: "进入队列",
  review_origin: "由反馈修订发起",
  node_started: "开始",
  node_completed: "完成",
  node_failed: "失败",
  tool_completed: "读取证据",
  tool_failed: "读取失败",
  run_completed: "运行完成",
  run_failed: "运行失败",
  run_cancelled: "运行已取消",
};

export const TOOL_LABELS: Record<string, string> = {
  "context.research_input": "研究输入",
  "context.task_constraints": "任务约束与范围",
  "context.knowledge_bundles": "知识证据包",
};

export const DEPTH_LABELS: Record<string, string> = {
  brief: "简报",
  standard: "标准",
  deep: "深度",
};

export const COMMAND_STAGE_LABELS: Record<string, string> = {
  accepted: "已接收指令",
  preparing: "正在准备",
  task_ready: "任务已就绪",
  snapshot_ready: "上下文快照已冻结",
  target_created: "研究已创建，正在跳转",
  target_failed: "创建研究失败",
  completed: "已完成",
};
