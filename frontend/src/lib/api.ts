export type JsonObject = Record<string, unknown>;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface Project {
  id: string;
  name: string;
  goal: string;
  domain: string;
  status: string;
  metadata: JsonObject;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceTask {
  id: string;
  project_id: string;
  title: string;
  goal: string;
  status: string;
  priority: string;
  metadata: JsonObject;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface Decision {
  id: string;
  project_id: string;
  task_id: string | null;
  summary: string;
  rationale: string;
  impact: string;
  status: string;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface Artifact {
  id: string;
  project_id: string;
  task_id: string | null;
  type: string;
  reference: string;
  version: number;
  status: string;
  supersedes_artifact_id: string | null;
  metadata: JsonObject;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface MemoryProposal {
  id: string;
  project_id: string;
  task_id: string | null;
  proposal_type: string;
  payload: JsonObject;
  rationale: string;
  status: string;
  review_note: string | null;
  committed_record_type: string | null;
  committed_record_id: string | null;
  revision: number;
  created_at: string;
  reviewed_at: string | null;
  committed_at: string | null;
  updated_at: string;
}

export interface AgentRun {
  id: string;
  project_id: string;
  task_id: string;
  context_snapshot_id: string;
  context_sha256: string;
  workflow_name: string;
  workflow_version: string;
  status: string;
  model_provider: string;
  model_name: string;
  max_steps: number;
  max_tool_calls: number;
  token_budget: number;
  tool_call_count: number;
  repair_count: number;
  current_node: string | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  revision: number;
}

export interface ContextSnapshotSummary {
  id: string | null;
  persisted: boolean;
  project_id: string;
  task_id: string;
  project_revision: number;
  task_revision: number;
  builder_version: string;
  package_schema_version: string;
  package_sha256: string;
  token_budget: number;
  used_tokens: number;
  collection_scopes: string[];
  item_counts: Record<string, number>;
  diagnostics: {
    knowledge_coverage: string;
    selected_claim_bundles: number;
    notices: string[];
  };
  created_at: string;
}

export interface ContextSnapshotItem {
  section: string;
  item_type: string;
  item_id: string;
  parent_item_id: string | null;
  rank: number | null;
  score: number | null;
  selected_reason: string;
  provenance: JsonObject;
  estimated_tokens: number;
}

export interface EvidenceReference {
  snapshot_id: string;
  claim: { id: string; statement: string; selected_reason: string; provenance: JsonObject };
  evidence: {
    id: string;
    quote: string;
    quote_sha256: string;
    location: JsonObject;
    selected_reason: string;
    provenance: JsonObject;
  };
  chunk: { id: string; page_start: number; page_end: number; location: JsonObject };
  document: { id: string; title: string; pages: number; parser_version: string };
  source: { id: string; title: string; uri: string; canonical_uri: string; version: string };
}

export interface AgentRunEvent {
  id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  node_name: string | null;
  status: string;
  input_summary: JsonObject;
  output_summary: JsonObject;
  token_usage: Record<string, number>;
  latency_ms: number | null;
  error: JsonObject;
  created_at: string;
}

export interface AgentToolCall {
  id: string;
  run_id: string;
  event_id: string;
  sequence: number;
  tool_name: string;
  permission: string;
  result_summary: JsonObject;
  result_hash: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  error: JsonObject;
}

export interface AgentTrace {
  run: AgentRun;
  events: AgentRunEvent[];
  tool_calls: AgentToolCall[];
  next_event_sequence: number | null;
  token_usage: Record<string, number>;
  total_latency_ms: number;
}

export interface AgentOutput {
  id: string;
  run_id: string;
  output_type: string;
  structured: JsonObject;
  rendered_text: string;
  output_sha256: string;
  validation: JsonObject;
  created_at: string;
}

export interface ArtifactContent {
  artifact_id: string;
  reference: string;
  content_type: "text/markdown";
  rendered_markdown: string;
  output_id: string;
  output_sha256: string;
  run_id: string;
  context_snapshot_id: string;
  context_sha256: string;
  validation: JsonObject;
}

export interface Dashboard {
  active_projects: Project[];
  recent_workspace_tasks: WorkspaceTask[];
  recent_agent_runs: AgentRun[];
  pending_memory_proposals: MemoryProposal[];
  recent_artifacts: Artifact[];
}

export interface KnowledgeCollection {
  slug: string;
  name: string;
  is_system: boolean;
  ingestion_count: number;
  updated_at: string | null;
}

export interface ProjectKnowledgeScope {
  project_id: string;
  collection_slug: string;
  created_at: string;
}

export interface KnowledgeIngestion {
  id: string;
  topic: string;
  topic_slug: string;
  collection: string;
  collection_slug: string;
  status: string;
  candidate_count: number;
  published_count: number;
  created_at: string;
  updated_at: string;
}

interface CandidateEvidence {
  paper_id: string;
  chunk_id: string;
  page_start: number;
  page_end: number;
  quote: string;
}

interface CandidateBase {
  id: string;
  ingestion_id: string;
  topic_slug: string;
  summary: string;
  confidence: number;
  evidence: CandidateEvidence;
  status: string;
}

export interface EntityCandidate extends CandidateBase {
  name: string;
  type: string;
}

export interface RelationCandidate extends CandidateBase {
  source_candidate_id: string;
  target_candidate_id: string;
  type: string;
}

export interface KnowledgeCandidate {
  kind: "entity" | "relation";
  candidate: EntityCandidate | RelationCandidate;
}

export interface KnowledgeSearchResult {
  query: string;
  topic_slugs: string[];
  evidence: Array<{
    id: string;
    paper_id: string;
    chunk_id: string;
    title: string;
    text: string;
    page_start: number;
    page_end: number;
    score: number;
  }>;
  graph: Array<{
    source_id?: string;
    source_name: string;
    edge_id?: string;
    relation_type: string;
    target_id?: string;
    target_name: string;
  }>;
}

interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

function query(values: Record<string, string | number | boolean | undefined>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined) params.set(key, String(value));
  }
  const rendered = params.toString();
  return rendered ? `?${rendered}` : "";
}

function path(value: string) {
  return encodeURIComponent(value);
}

function detailMessage(detail: unknown) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => String(item.msg ?? item)).join("；");
  if (detail && typeof detail === "object" && "message" in detail) return String(detail.message);
  return "请求未能完成。";
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = await response.text();
    }
    throw new ApiError(detailMessage(detail), response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  dashboard: () => request<Dashboard>("/api/v1/workspace/dashboard?limit=12"),
  projects: () => request<Project[]>("/api/projects"),
  project: (projectId: string) => request<Project>(`/api/projects/${path(projectId)}`),
  createProject: (input: Pick<Project, "name" | "goal" | "domain">) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify(input) }),
  tasks: (projectId: string, includeClosed = false) =>
    request<WorkspaceTask[]>(
      `/api/projects/${path(projectId)}/workspace-tasks${query({ include_closed: includeClosed })}`,
    ),
  task: (taskId: string) => request<WorkspaceTask>(`/api/workspace-tasks/${path(taskId)}`),
  createTask: (
    projectId: string,
    input: Pick<WorkspaceTask, "title" | "goal" | "priority">,
  ) =>
    request<WorkspaceTask>(`/api/projects/${path(projectId)}/workspace-tasks`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  scopes: (projectId: string) =>
    request<ProjectKnowledgeScope[]>(
      `/api/projects/${path(projectId)}/knowledge-scopes`,
    ),
  collections: () => request<KnowledgeCollection[]>("/api/knowledge/collections"),
  replaceScopes: (projectId: string, expectedProjectRevision: number, collectionSlugs: string[]) =>
    request<ProjectKnowledgeScope[]>(`/api/projects/${path(projectId)}/knowledge-scopes`, {
      method: "PUT",
      body: JSON.stringify({
        expected_project_revision: expectedProjectRevision,
        collection_slugs: collectionSlugs,
      }),
    }),
  ingestions: () => request<KnowledgeIngestion[]>("/api/knowledge/ingestions"),
  candidates: (ingestionId: string, status = "draft") =>
    request<KnowledgeCandidate[]>(
      `/api/knowledge/ingestions/${path(ingestionId)}/candidates${query({ status })}`,
    ),
  decideCandidate: (candidateId: string, decision: "approve" | "reject" | "defer") =>
    request<unknown>(`/api/knowledge/candidates/${path(candidateId)}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  searchKnowledge: (searchQuery: string, collectionSlugs: string[]) => {
    const params = new URLSearchParams({ q: searchQuery, top_k: "8" });
    for (const slug of collectionSlugs) params.append("collection_slug", slug);
    return request<KnowledgeSearchResult>(`/api/knowledge/search?${params.toString()}`);
  },
  decisions: (projectId: string) =>
    request<Decision[]>(`/api/projects/${path(projectId)}/decisions?include_inactive=true`),
  artifacts: (projectId: string) =>
    request<Artifact[]>(`/api/projects/${path(projectId)}/artifacts?include_inactive=true`),
  artifact: (artifactId: string) => request<Artifact>(`/api/artifacts/${path(artifactId)}`),
  artifactContent: (artifactId: string) =>
    request<ArtifactContent>(`/api/v1/artifacts/${path(artifactId)}/content`),
  proposals: (projectId: string) =>
    request<MemoryProposal[]>(`/api/projects/${path(projectId)}/memory-proposals?include_closed=true`),
  proposal: (proposalId: string) => request<MemoryProposal>(`/api/memory-proposals/${path(proposalId)}`),
  reviewProposal: (proposalId: string, revision: number, status: "approved" | "rejected") =>
    request<MemoryProposal>(`/api/memory-proposals/${path(proposalId)}/review`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: revision, status }),
    }),
  commitProposal: (proposalId: string) =>
    request<{ proposal: MemoryProposal }>(`/api/memory-proposals/${path(proposalId)}/commit`, {
      method: "POST",
    }),
  previewContext: (projectId: string, taskId: string, maxTokens = 6000) =>
    request<{ snapshot: ContextSnapshotSummary }>(
      `/api/v1/projects/${path(projectId)}/workspace-tasks/${path(taskId)}/context-snapshots/preview`,
      { method: "POST", body: JSON.stringify({ max_tokens: maxTokens }) },
    ),
  createSnapshot: (projectId: string, taskId: string, maxTokens = 6000) =>
    request<ContextSnapshotSummary>(
      `/api/v1/projects/${path(projectId)}/workspace-tasks/${path(taskId)}/context-snapshots`,
      { method: "POST", body: JSON.stringify({ max_tokens: maxTokens }) },
    ),
  snapshot: (snapshotId: string) => request<ContextSnapshotSummary>(`/api/v1/context-snapshots/${path(snapshotId)}`),
  snapshotItems: (snapshotId: string, offset = 0, limit = 50) =>
    request<{ snapshot_id: string; items: ContextSnapshotItem[]; next_offset: number | null }>(
      `/api/v1/context-snapshots/${path(snapshotId)}/items${query({ offset, limit })}`,
    ),
  evidence: (snapshotId: string, evidenceId: string) =>
    request<EvidenceReference>(
      `/api/v1/context-snapshots/${path(snapshotId)}/evidence/${path(evidenceId)}`,
    ),
  startRun: (projectId: string, taskId: string, snapshotId: string, createProposal: boolean) =>
    request<AgentRun>(`/api/projects/${path(projectId)}/workspace-tasks/${path(taskId)}/agent-runs`, {
      method: "POST",
      body: JSON.stringify({
        context_snapshot_id: snapshotId,
        workflow: "research",
        create_memory_proposal: createProposal,
        max_steps: 10,
        // All available snapshot tools are local, read-only context views.
        // Allow the bounded research planner to use the full three-tool set.
        max_tool_calls: 3,
        token_budget: 6000,
      }),
    }),
  projectRuns: (projectId: string) =>
    request<CursorPage<AgentRun>>(`/api/v1/projects/${path(projectId)}/agent-runs?limit=20`),
  taskRuns: (projectId: string, taskId: string) =>
    request<CursorPage<AgentRun>>(
      `/api/v1/projects/${path(projectId)}/workspace-tasks/${path(taskId)}/agent-runs?limit=20`,
    ),
  run: (runId: string) => request<AgentRun>(`/api/agent-runs/${path(runId)}`),
  trace: (runId: string, afterSequence = 0) =>
    request<AgentTrace>(`/api/v1/agent-runs/${path(runId)}/trace${query({ after_sequence: afterSequence, limit: 50 })}`),
  output: (runId: string) => request<AgentOutput>(`/api/agent-runs/${path(runId)}/output`),
  cancelRun: (runId: string) => request<AgentRun>(`/api/agent-runs/${path(runId)}/cancel`, { method: "POST" }),
  resumeRun: (runId: string) => request<AgentRun>(`/api/agent-runs/${path(runId)}/resume`, { method: "POST" }),
};
