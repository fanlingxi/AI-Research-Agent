import { createNodeBorderProgram } from "@sigma/node-border";
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import FA2LayoutSupervisor from "graphology-layout-forceatlas2/worker";
import { Maximize2, Minus, Plus } from "lucide-react";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import Sigma from "sigma";
import { EdgeArrowProgram, EdgeLineProgram } from "sigma/rendering";
import type { EdgeDisplayData, NodeDisplayData } from "sigma/types";

import type { KnowledgeMapGraph } from "./api";
import { DIM_COLOR, EDGE_COLOR, EVIDENCE_EDGE_COLOR, PENDING_COLOR, relationLabel, typeColor } from "./palette";

export interface GraphCanvasHandle {
  focus: (nodeId: string) => void;
}

interface GraphCanvasProps {
  data: KnowledgeMapGraph;
  selectedId: string | null;
  onSelect: (nodeId: string | null) => void;
}

interface EdgeTip {
  x: number;
  y: number;
  type: string;
  label: string;
  pending: boolean;
  source: string;
  target: string;
}

// Hollow ring = not yet approved. Fill colour is per-node so the ring keeps the type hue.
const PendingNodeProgram = createNodeBorderProgram({
  borders: [
    { size: { value: 0.32 }, color: { attribute: "borderColor" } },
    { size: { fill: true }, color: { value: "#ffffff" } },
  ],
});

const SYNC_LAYOUT_LIMIT = 450;

function hashUnit(value: string, salt: number) {
  let hash = 2166136261 ^ salt;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return ((hash >>> 0) % 100000) / 100000;
}

function shortLabel(label: string) {
  return label.length > 16 ? `${label.slice(0, 15)}…` : label;
}

function buildGraph(data: KnowledgeMapGraph) {
  const graph = new Graph({ multi: true, type: "directed", allowSelfLoops: false });
  // Shrink nodes as the graph grows so thousands of nodes do not paint a solid blob.
  const scale = data.nodes.length > 1500 ? 0.45 : data.nodes.length > 400 ? 0.7 : 1;
  for (const node of data.nodes) {
    if (graph.hasNode(node.id)) continue;
    const pending = node.status === "pending";
    const base = Math.min(18, 3.5 + Math.sqrt(Math.max(node.score, 0)) * 2.1);
    const color = typeColor(node.type);
    graph.addNode(node.id, {
      x: 0,
      y: 0,
      size: Math.max(2, (node.type === "Paper" ? Math.max(base, 9) : base) * scale),
      label: pending ? `${shortLabel(node.label)}（待审核）` : shortLabel(node.label),
      fullLabel: node.label,
      color,
      borderColor: color,
      type: pending ? "pending" : "circle",
      nodeType: node.type,
      pending,
      matched: node.matched,
      forceLabel: node.matched,
    });
  }
  for (const edge of data.edges) {
    if (edge.source === edge.target || !graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
    if (graph.hasEdge(edge.id)) continue;
    const pending = edge.status === "pending";
    const evidence = edge.kind === "evidence";
    graph.addDirectedEdgeWithKey(edge.id, edge.source, edge.target, {
      type: evidence ? "line" : "arrow",
      color: pending ? `${PENDING_COLOR}99` : evidence ? EVIDENCE_EDGE_COLOR : EDGE_COLOR,
      size: evidence ? 0.8 : Math.min(3.2, 1.1 + Math.log2(edge.weight + 1) * 0.5),
      relation: edge.type,
      statement: edge.label,
      pending,
      evidence,
      weight: edge.weight,
    });
  }
  seedPositions(graph, data);
  return graph;
}

/**
 * Cheap pre-layout so ForceAtlas2 starts from readable clusters: every node is placed next to its
 * most important neighbour ("hub"), and hubs are spread on a sunflower spiral. Deterministic per id.
 */
function seedPositions(graph: Graph, data: KnowledgeMapGraph) {
  const score = new Map(data.nodes.map((node) => [node.id, node.score]));
  const anchor = new Map<string, string>();
  const children = new Map<string, string[]>();
  for (const node of data.nodes) {
    let best: string | null = null;
    for (const neighbour of graph.neighbors(node.id)) {
      if ((score.get(neighbour) ?? 0) > (score.get(best ?? "") ?? score.get(node.id) ?? 0)) best = neighbour;
    }
    if (best) {
      anchor.set(node.id, best);
      children.set(best, [...(children.get(best) ?? []), node.id]);
    }
  }
  const root = (id: string) => {
    let current = id;
    for (let depth = 0; depth < 6 && anchor.has(current); depth += 1) current = anchor.get(current)!;
    return current;
  };
  const hubs = data.nodes.filter((node) => !anchor.has(node.id)).sort((a, b) => b.score - a.score);
  const golden = Math.PI * (3 - Math.sqrt(5));
  const spacing = 40;
  hubs.forEach((hub, index) => {
    const radius = spacing * Math.sqrt(index + 0.5);
    graph.mergeNodeAttributes(hub.id, { x: Math.cos(index * golden) * radius, y: Math.sin(index * golden) * radius });
  });
  for (const node of data.nodes) {
    if (!anchor.has(node.id)) continue;
    const hub = root(node.id);
    const angle = hashUnit(node.id, 1) * Math.PI * 2;
    const spread = 6 + Math.sqrt(children.get(hub)?.length ?? 1) * 3;
    const radius = spread * (0.4 + 0.6 * hashUnit(node.id, 7));
    graph.mergeNodeAttributes(node.id, {
      x: (graph.getNodeAttribute(hub, "x") as number) + Math.cos(angle) * radius,
      y: (graph.getNodeAttribute(hub, "y") as number) + Math.sin(angle) * radius,
    });
  }
}

export const GraphCanvas = forwardRef<GraphCanvasHandle, GraphCanvasProps>(function GraphCanvas(
  { data, selectedId, onSelect },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const hoveredRef = useRef<string | null>(null);
  const selectedRef = useRef<string | null>(selectedId);
  const onSelectRef = useRef(onSelect);
  const refreshRef = useRef<(() => void) | null>(null);
  const [laying, setLaying] = useState(false);
  const [tip, setTip] = useState<EdgeTip | null>(null);

  onSelectRef.current = onSelect;

  useImperativeHandle(ref, () => ({
    focus(nodeId: string) {
      const sigma = sigmaRef.current;
      const display = sigma?.getNodeDisplayData(nodeId);
      if (!sigma || !display) return;
      sigma.getCamera().animate({ x: display.x, y: display.y, ratio: 0.45 }, { duration: 450 });
    },
  }));

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const graph = buildGraph(data);
    graphRef.current = graph;

    const focusSet = () => {
      const focus = hoveredRef.current ?? selectedRef.current;
      if (!focus || !graph.hasNode(focus)) return null;
      return { focus, neighbours: new Set<string>([focus, ...graph.neighbors(focus)]) };
    };
    let cachedFocus = focusSet();

    const sigma = new Sigma(graph, container, {
      allowInvalidContainer: true,
      renderEdgeLabels: true,
      enableEdgeEvents: true,
      zIndex: true,
      defaultEdgeType: "arrow",
      nodeProgramClasses: { pending: PendingNodeProgram },
      edgeProgramClasses: { arrow: EdgeArrowProgram, line: EdgeLineProgram },
      labelFont: 'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
      labelSize: 12,
      labelWeight: "500",
      labelColor: { color: "#18212f" },
      labelDensity: graph.order < 250 ? 0.8 : 0.5,
      labelGridCellSize: graph.order < 250 ? 100 : 120,
      labelRenderedSizeThreshold: container.clientWidth < 520 ? 9 : graph.order < 250 ? 5 : 8,
      stagePadding: 48,
      edgeLabelFont: 'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
      edgeLabelSize: 11,
      edgeLabelColor: { color: "#475569" },
      minCameraRatio: 0.05,
      maxCameraRatio: 4,
      nodeReducer: (node, attributes) => {
        const result: Partial<NodeDisplayData> & Record<string, unknown> = { ...attributes };
        if (cachedFocus) {
          if (!cachedFocus.neighbours.has(node)) {
            result.color = DIM_COLOR;
            result.borderColor = DIM_COLOR;
            result.label = "";
            result.zIndex = 0;
          } else {
            result.zIndex = 2;
            result.forceLabel = cachedFocus.neighbours.size <= 40 || node === cachedFocus.focus;
            if (node === cachedFocus.focus) {
              result.highlighted = true;
              result.label = `${attributes.fullLabel}${attributes.pending ? "（待审核）" : ""}`;
            }
          }
        } else if (attributes.matched) {
          result.highlighted = true;
          result.zIndex = 2;
        }
        return result;
      },
      edgeReducer: (edge, attributes) => {
        const result: Partial<EdgeDisplayData> & Record<string, unknown> = { ...attributes, label: "" };
        if (cachedFocus) {
          const [source, target] = graph.extremities(edge);
          if (source !== cachedFocus.focus && target !== cachedFocus.focus) {
            result.hidden = true;
          } else {
            result.color = attributes.pending ? PENDING_COLOR : attributes.evidence ? "#9aa7b6" : "#526173";
            result.size = (attributes.size as number) + 0.6;
            result.label = relationLabel(attributes.relation as string) + (attributes.pending ? " · 待审核" : "");
            result.zIndex = 2;
          }
        }
        return result;
      },
    });
    sigmaRef.current = sigma;

    const refresh = () => {
      cachedFocus = focusSet();
      sigma.refresh({ skipIndexation: true });
    };
    refreshRef.current = refresh;

    sigma.on("enterNode", ({ node }) => {
      hoveredRef.current = node;
      container.style.cursor = "pointer";
      refresh();
    });
    sigma.on("leaveNode", () => {
      hoveredRef.current = null;
      container.style.cursor = "";
      refresh();
    });
    sigma.on("clickNode", ({ node }) => onSelectRef.current(node));
    sigma.on("clickStage", () => onSelectRef.current(null));
    sigma.on("enterEdge", ({ edge, event }) => {
      const attributes = graph.getEdgeAttributes(edge);
      const [source, target] = graph.extremities(edge);
      setTip({
        x: event.x,
        y: event.y,
        type: attributes.relation as string,
        label: attributes.statement as string,
        pending: Boolean(attributes.pending),
        source: graph.getNodeAttribute(source, "fullLabel") as string,
        target: graph.getNodeAttribute(target, "fullLabel") as string,
      });
    });
    sigma.on("leaveEdge", () => setTip(null));
    sigma.getCamera().on("updated", () => setTip(null));

    // Layout: synchronous for small graphs, web-worker for large ones so the UI stays responsive.
    let supervisor: FA2LayoutSupervisor | null = null;
    let timer: number | undefined;
    if (graph.order > 1) {
      const settings = {
        ...forceAtlas2.inferSettings(graph),
        barnesHutOptimize: graph.order > 300,
        gravity: graph.order < 250 ? 0.4 : 1.2,
        scalingRatio: graph.order < 250 ? 40 : graph.order > 1000 ? 6 : 12,
        slowDown: 2 + Math.log(graph.order),
      };
      if (graph.order <= SYNC_LAYOUT_LIMIT) {
        forceAtlas2.assign(graph, { iterations: graph.order < 150 ? 500 : 250, settings });
      } else {
        supervisor = new FA2LayoutSupervisor(graph, { settings });
        supervisor.start();
        setLaying(true);
        timer = window.setTimeout(() => {
          supervisor?.stop();
          setLaying(false);
        }, Math.min(7000, 1800 + graph.order * 1.4));
      }
    }

    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => sigma.refresh());
    observer?.observe(container);

    return () => {
      window.clearTimeout(timer);
      supervisor?.kill();
      observer?.disconnect();
      sigma.kill();
      sigmaRef.current = null;
      graphRef.current = null;
      refreshRef.current = null;
      setLaying(false);
      setTip(null);
    };
  }, [data]);

  useEffect(() => {
    selectedRef.current = selectedId;
    refreshRef.current?.();
  }, [selectedId]);

  const camera = () => sigmaRef.current?.getCamera();

  return (
    <div className="relative h-full w-full">
      <div aria-label="知识关系图" className="absolute inset-0" ref={containerRef} role="img" />
      <div className="absolute right-3 top-3 flex flex-col overflow-hidden rounded-lg border bg-white/95 shadow-sm">
        <button aria-label="放大" className="grid size-8 place-items-center text-slate-600 hover:bg-slate-100" onClick={() => camera()?.animatedZoom({ duration: 250 })} type="button"><Plus size={15} /></button>
        <button aria-label="缩小" className="grid size-8 place-items-center border-t text-slate-600 hover:bg-slate-100" onClick={() => camera()?.animatedUnzoom({ duration: 250 })} type="button"><Minus size={15} /></button>
        <button aria-label="适应画布" className="grid size-8 place-items-center border-t text-slate-600 hover:bg-slate-100" onClick={() => camera()?.animatedReset({ duration: 300 })} type="button"><Maximize2 size={14} /></button>
      </div>
      {laying ? (
        <div className="absolute left-3 top-3 rounded-full border bg-white/95 px-3 py-1 text-xs text-muted-ink shadow-sm" role="status">
          正在计算布局…
        </div>
      ) : null}
      {tip ? (
        <div
          className="pointer-events-none absolute z-10 max-w-72 rounded-lg border bg-white px-3 py-2 text-xs shadow-lg"
          style={{ left: Math.min(tip.x + 12, (containerRef.current?.clientWidth ?? 400) - 290), top: tip.y + 12 }}
        >
          <p className="font-semibold text-ink">
            {relationLabel(tip.type)}
            {tip.pending ? <span className="ml-1.5 rounded bg-amber-100 px-1 py-px font-medium text-amber-900">待审核</span> : null}
          </p>
          <p className="mt-1 text-slate-600">{tip.source} → {tip.target}</p>
          {tip.label ? <p className="mt-1 leading-5 text-slate-500">{tip.label}</p> : null}
        </div>
      ) : null}
    </div>
  );
});

export default GraphCanvas;
