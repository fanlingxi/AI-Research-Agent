// Sigma needs resolved colours, not CSS variable expressions.

export const TYPE_ORDER = ["Paper", "Method", "Concept", "Task", "Dataset", "Metric", "Finding", "Topic"];

const TYPE_COLORS: Record<string, string> = {
  Paper: "#195f63",
  Method: "#2f72b8",
  Concept: "#7c5cc4",
  Task: "#c77d1e",
  Dataset: "#2f8a5b",
  Metric: "#c0506f",
  Finding: "#7d8a24",
  Topic: "#4f6177",
};

const FALLBACK_COLORS = ["#5b7c99", "#996b5b", "#6b8f71", "#8a6b99"];

export const TYPE_LABELS: Record<string, string> = {
  Paper: "论文",
  Method: "方法",
  Concept: "概念",
  Task: "任务",
  Dataset: "数据集",
  Metric: "指标",
  Finding: "发现",
  Topic: "主题",
  Formula: "公式",
};

export const RELATION_LABELS: Record<string, string> = {
  PRESENTS: "提出",
  ADDRESSES: "针对",
  USES: "使用",
  EVALUATES: "评估",
  IMPROVES: "改进",
  COMPARES_WITH: "对比",
  APPLIES_TO: "应用于",
  HAS_LIMITATION: "存在局限",
  SUPPORTS: "支持",
  EVIDENCE: "证据支撑",
};

export function mapColor(token: string, fallback: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--color-${token}`).trim() || fallback;
}
export function mapPalette() {
  return {
    panel: mapColor("panel", "#ffffff"),
    label: mapColor("ink", "#18212f"),
    edgeLabel: mapColor("ink-soft", "#475569"),
    edge: mapColor("map-edge", EDGE_COLOR),
    evidence: mapColor("map-evidence", EVIDENCE_EDGE_COLOR),
    dim: mapColor("map-dim", DIM_COLOR),
    focus: mapColor("map-focus", "#526173"),
    pending: mapColor("map-pending", PENDING_COLOR),
  };
}
export const PENDING_COLOR = "#d97706";
export const PENDING_SWATCH = `var(--color-map-pending, ${PENDING_COLOR})`;
export const EDGE_COLOR = "#b8c2cf";
export const EVIDENCE_EDGE_COLOR = "#dfe4ea";
export const DIM_COLOR = "#eceef1";

export function typeSwatch(type: string) {
  return TYPE_COLORS[type] ? `var(--color-map-${type.toLowerCase()}, ${TYPE_COLORS[type]})` : typeColor(type);
}

export function typeColor(type: string) {
  if (TYPE_COLORS[type]) return mapColor(`map-${type.toLowerCase()}`, TYPE_COLORS[type]);
  let hash = 0;
  for (const char of type) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return FALLBACK_COLORS[hash % FALLBACK_COLORS.length];
}

export function typeLabel(type: string) {
  return TYPE_LABELS[type] ?? type;
}

export function relationLabel(type: string) {
  return RELATION_LABELS[type] ?? type;
}

export function sortTypes(types: string[]) {
  return [...types].sort((a, b) => {
    const ia = TYPE_ORDER.indexOf(a);
    const ib = TYPE_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
  });
}
