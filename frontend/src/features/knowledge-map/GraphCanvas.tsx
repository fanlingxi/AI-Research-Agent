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
import { mapPalette, relationLabel, typeColor } from "./palette";

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
    { size: { fill: true }, color: { attribute: "fillColor" } },
  ],
});

const SYNC_LAYOUT_LIMIT = 450;
const DETAIL_LABEL_RATIO = 0.55;

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
  const palette = mapPalette();
  const graph = new Graph({ multi: true, type: "directed", allowSelfLoops: false });
  // Only a small, deterministic set competes for label space in the overview.
  const importantIds = new Set([...data.nodes]
    .sort((a, b) => b.score - a.score || b.degree - a.degree || a.id.localeCompare(b.id))
    .slice(0, Math.min(20, Math.max(5, Math.ceil(Math.sqrt(data.nodes.length)))))
    .map((node) => node.id));
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
      fillColor: palette.panel,
      type: pending ? "pending" : "circle",
      nodeType: node.type,
      pending,
      matched: node.matched,
      important: importantIds.has(node.id),
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
      color: pending ? palette.pending : evidence ? palette.evidence : palette.edge,
      size: pending ? 0.6 : evidence ? 0.8 : Math.min(3.2, 1.1 + Math.log2(edge.weight + 1) * 0.5),
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
    let palette = mapPalette();
    const graph = buildGraph(data);
    graphRef.current = graph;

    const focusSet = () => {
      const focus = hoveredRef.current ?? selectedRef.current;
      if (!focus || !graph.hasNode(focus)) return null;
      return { focus, neighbours: new Set<string>([focus, ...graph.neighbors(focus)]) };
    };
    let cachedFocus = focusSet();
    let showDetailLabels = false;

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
      defaultDrawNodeHover: (context, node, settings) => {
        if (!node.label) return;
        context.font = `${settings.labelWeight} ${settings.labelSize}px ${settings.labelFont}`;
        const width = context.measureText(node.label).width;
        context.fillStyle = palette.panel;
        context.fillRect(node.x + node.size + 1, node.y - 12, width + 12, 24);
        context.fillStyle = palette.label;
        context.fillText(node.label, node.x + node.size + 7, node.y + 4);
      },
      labelWeight: "500",
      labelColor: { color: palette.label },
      labelDensity: 0.5,
      labelGridCellSize: 180,
      labelRenderedSizeThreshold: 0,
      stagePadding: 48,
      edgeLabelFont: 'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
      edgeLabelSize: 11,
      edgeLabelColor: { color: palette.edgeLabel },
      minCameraRatio: 0.05,
      maxCameraRatio: 4,
      nodeReducer: (node, attributes) => {
        const result: Partial<NodeDisplayData> & Record<string, unknown> = { ...attributes };
        const selected = selectedRef.current;
        const selectedNeighbour = selected && graph.hasNode(selected) && graph.areNeighbors(node, selected);
        const explicit = Boolean(attributes.matched || node === selected || node === hoveredRef.current);
        result.forceLabel = explicit;
        if (!showDetailLabels && !attributes.important && !explicit && !selectedNeighbour && !cachedFocus?.neighbours.has(node)) {
          result.label = "";
        }
        if (cachedFocus) {
          if (!cachedFocus.neighbours.has(node)) {
            result.color = palette.dim;
            result.borderColor = palette.dim;
            if (!explicit && !selectedNeighbour) result.label = "";
            result.zIndex = 0;
          } else {
            result.zIndex = 2;
            // Neighbours still use Sigma's collision grid; forcing all of them overlaps labels.
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
          if (source !== cachedFocus.focus && target !== cachedFocus.focus && source !== selectedRef.current && target !== selectedRef.current) {
            result.hidden = true;
          } else {
            result.color = attributes.pending ? palette.pending : palette.focus;
            result.size = attributes.pending ? 1.8 : (attributes.size as number) + 0.6;
            result.label = relationLabel(attributes.relation as string) + (attributes.pending ? " · 待审核" : "");
            result.zIndex = 2;
          }
        }
        return result;
      },
    });
    sigmaRef.current = sigma;
    const updateColors = () => {
      palette = mapPalette();
      graph.forEachNode((node, attrs) => graph.mergeNodeAttributes(node, {
        color: typeColor(attrs.nodeType as string),
        borderColor: typeColor(attrs.nodeType as string),
        fillColor: palette.panel,
      }));
      graph.forEachEdge((edge, attrs) => graph.setEdgeAttribute(edge, "color",
        attrs.pending ? palette.pending : attrs.evidence ? palette.evidence : palette.edge));
      sigma.setSetting("labelColor", { color: palette.label });
      sigma.setSetting("edgeLabelColor", { color: palette.edgeLabel });
      sigma.refresh();
    };
    updateColors();
    const themeObserver = new MutationObserver(updateColors);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    const refresh = () => {
      cachedFocus = focusSet();
      sigma.refresh();
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
    sigma.getCamera().on("updated", () => {
      setTip(null);
      const next = sigma.getCamera().getState().ratio < DETAIL_LABEL_RATIO;
      if (next !== showDetailLabels) {
        showDetailLabels = next;
        // Rebuild the label grid when the set of eligible labels changes.
        sigma.refresh();
      }
    });

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
      themeObserver.disconnect();
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
      <div className="absolute right-3 top-3 flex flex-col overflow-hidden rounded-lg border bg-panel/95 shadow-sm">
        <button aria-label="放大" className="grid size-8 place-items-center text-ink-soft hover:bg-subtle" onClick={() => camera()?.animatedZoom({ duration: 250 })} type="button"><Plus size={15} /></button>
        <button aria-label="缩小" className="grid size-8 place-items-center border-t text-ink-soft hover:bg-subtle" onClick={() => camera()?.animatedUnzoom({ duration: 250 })} type="button"><Minus size={15} /></button>
        <button aria-label="适应画布" className="grid size-8 place-items-center border-t text-ink-soft hover:bg-subtle" onClick={() => camera()?.animatedReset({ duration: 300 })} type="button"><Maximize2 size={14} /></button>
      </div>
      {laying ? (
        <div className="absolute left-3 top-3 rounded-full border bg-panel/95 px-3 py-1 text-xs text-muted-ink shadow-sm" role="status">
          正在计算布局…
        </div>
      ) : null}
      {tip ? (
        <div
          className="pointer-events-none absolute z-10 max-w-72 rounded-lg border bg-panel px-3 py-2 text-xs shadow-lg"
          style={{ left: Math.min(tip.x + 12, (containerRef.current?.clientWidth ?? 400) - 290), top: tip.y + 12 }}
        >
          <p className="font-semibold text-ink">
            {relationLabel(tip.type)}
            {tip.pending ? <span className="ml-1.5 rounded bg-amber-100 px-1 py-px font-medium text-amber-900">待审核</span> : null}
          </p>
          <p className="mt-1 text-ink-soft">{tip.source} → {tip.target}</p>
          {tip.label ? <p className="mt-1 leading-5 text-muted-ink">{tip.label}</p> : null}
        </div>
      ) : null}
    </div>
  );
});

export default GraphCanvas;
