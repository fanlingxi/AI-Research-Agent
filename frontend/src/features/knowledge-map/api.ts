import { ApiError } from "../../lib/api";

export type MapStatus = "published" | "pending";

export interface MapNode {
  id: string;
  label: string;
  type: string;
  kind: "entity" | "source";
  status: MapStatus;
  degree: number;
  evidence_count: number;
  paper_count: number;
  score: number;
  matched: boolean;
}

export interface MapEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  kind: "relation" | "evidence";
  status: MapStatus;
  weight: number;
  label: string;
}

export interface KnowledgeMapGraph {
  nodes: MapNode[];
  edges: MapEdge[];
  stats: {
    total_nodes: number;
    total_edges: number;
    shown_nodes: number;
    shown_edges: number;
    pending_nodes: number;
    pending_edges: number;
    truncated: boolean;
    limit: number;
  };
  type_counts: Record<string, number>;
}

export interface MapEvidence {
  id: string;
  quote: string;
  document_id: string | null;
  paper_title: string | null;
  paper_node_id: string | null;
  page_start: number | null;
  page_end: number | null;
}

export interface MapNodeRef {
  id: string;
  label: string;
  type: string;
  status: MapStatus;
}

export interface MapNodeDetail {
  node: MapNodeRef & {
    kind: "entity" | "source";
    summary: string;
    aliases: string[];
    collections: string[];
  };
  claims: {
    id: string;
    statement: string;
    claim_type: string;
    confidence: number | null;
    status: MapStatus;
    evidence: MapEvidence[];
  }[];
  relations: {
    id: string;
    type: string;
    direction: "in" | "out";
    status: MapStatus;
    statement: string;
    other: MapNodeRef;
    evidence: MapEvidence[];
  }[];
  papers: {
    document_id: string;
    node_id: string;
    title: string;
    pages: number | null;
    url: string | null;
    evidence_count: number;
  }[];
  review: { candidate_id: string; ingestion_id: string } | null;
}

export interface MapCollection {
  slug: string;
  name: string;
  is_system: boolean;
}

export interface MapFilters {
  collection: string;
  types: string[];
  q: string;
  includePending: boolean;
  limit: number;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const body = await response.text();
    let detail: unknown = body;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      detail = parsed?.detail ?? parsed;
    } catch {
      // keep raw text
    }
    throw new ApiError(typeof detail === "string" && detail ? detail : "请求未能完成。", response.status, detail);
  }
  return (await response.json()) as T;
}

export function mapQueryString(filters: MapFilters) {
  const params = new URLSearchParams();
  if (filters.collection) params.set("collection_slug", filters.collection);
  for (const type of filters.types) params.append("entity_type", type);
  if (filters.q.trim()) params.set("q", filters.q.trim());
  if (filters.includePending) params.set("include_pending", "true");
  params.set("limit", String(filters.limit));
  return params.toString();
}

export const knowledgeMapApi = {
  graph: (filters: MapFilters) => getJson<KnowledgeMapGraph>(`/api/knowledge/map?${mapQueryString(filters)}`),
  node: (nodeId: string, includePending: boolean) =>
    getJson<MapNodeDetail>(
      `/api/knowledge/map/nodes/${encodeURIComponent(nodeId)}${includePending ? "?include_pending=true" : ""}`,
    ),
  collections: () => getJson<MapCollection[]>("/api/knowledge/collections"),
};
