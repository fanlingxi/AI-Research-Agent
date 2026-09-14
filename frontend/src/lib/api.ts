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
  sources: string[];
  pdf_max_pages: number;
  status: string;
  document_count: number;
  candidate_count: number;
  published_count: number;
  queue_position: number | null;
  job_attempts: number;
  error: string | null;
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
  source_name?: string;
  target_name?: string;
  type: string;
}

export interface KnowledgeCandidate {
  kind: "entity" | "relation";
  candidate: EntityCandidate | RelationCandidate;
}

export interface KnowledgeCandidatePage {
  items: KnowledgeCandidate[];
  total: number;
  offset: number;
  limit: number;
  next_offset: number | null;
}

export interface BulkApprovalResult {
  ingestion: KnowledgeIngestion;
  published_entities: number;
  published_relations: number;
  skipped_conflicts: number;
  blocked_relations: number;
}

export interface BulkCandidateDecisionResult {
  ingestion: KnowledgeIngestion;
  decision: "approve" | "reject" | "defer";
  requested: number;
  applied: number;
  replayed: number;
  skipped: Array<{ candidate_id: string; reason: string }>;
}

export interface ConfidenceAutoApprovalResult extends BulkCandidateDecisionResult {
  min_confidence_exclusive: number;
}

export interface ReportEvidence {
  id: string;
  paper_id: string;
  chunk_id: string;
  title: string;
  text: string;
  page_start: number;
  page_end: number;
  score: number;
}

export interface ResearchReport {
  id: string;
  query: string;
  topic_slugs: string[];
  top_k: number;
  report_depth: "brief" | "standard" | "deep";
  status: "queued" | "running" | "completed" | "failed";
  content: string;
  evidence: ReportEvidence[];
  evaluation: {
    evidence_grounding: number;
    citation_coverage: number;
    citation_fidelity: number;
    structure_score: number;
    retrieval_relevance?: number;
    source_diversity?: number;
    selected_source_count?: number;
    available_relevant_source_count?: number;
    cited_evidence: string[];
    invalid_citations: string[];
    revision_applied: boolean;
    passed: boolean;
  } | null;
  run_metadata: JsonObject;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeHealth {
  status: string;
  services: Record<string, { available?: boolean; detail?: string }>;
  knowledge: {
    schema_version: number;
    core_shadow?: {
      backfill_ready: boolean;
      cutover_ready: boolean;
      legacy_paper_ids: string[];
      core_paper_ids: string[];
      authorized_core_paper_ids: string[];
      legacy_only: string[];
      core_only: string[];
    };
    live_llm_configured: boolean;
    llm_provider: string;
    jobs: Record<string, number>;
    projections: Record<string, number>;
  };
}

export interface RuntimeServiceState {
  available: boolean;
  configured: boolean;
  detail: string;
  endpoint: string | null;
}

export interface RuntimeExecutorState {
  id: string;
  role: string;
  version: string;
  online: boolean;
  started_at: string;
  last_heartbeat_at: string;
  current_job_id: string | null;
  metadata: JsonObject;
}

export interface RuntimeOverview {
  status: string;
  generated_at: string;
  services: Record<string, RuntimeServiceState>;
  executors: RuntimeExecutorState[];
  work_counts: Record<string, Record<string, number>>;
  projection_backlog: {
    queued: number;
    running: number;
    failed: number;
    completed: number;
    oldest_queued_at: string | null;
  };
}

export interface RuntimeWorkItem {
  id: string;
  kind: string;
  resource_id: string;
  title: string;
  detail_route: string;
  business_status: string;
  job_status: string;
  current_stage: string | null;
  attempt: number;
  priority: number;
  queue_position: number | null;
  lease_until: string | null;
  executor: string | null;
  created_at: string;
  updated_at: string;
  last_error: string | null;
  can_retry: boolean;
  can_cancel: boolean;
}

export interface RuntimeWorkPage {
  items: RuntimeWorkItem[];
  next_cursor: string | null;
}

export type ResearchCommandInput =
  | {
      mode: "quick_report";
      instruction: string;
      collection_slugs: string[];
      report_depth: "brief" | "standard" | "deep";
      top_k: number;
    }
  | {
      mode: "project_run";
      instruction: string;
      project_id: string;
      task:
        | { kind: "existing"; task_id: string }
        | { kind: "new"; title?: string; priority: "low" | "normal" | "high" | "urgent" };
      create_memory_proposal: boolean;
      max_steps: number;
      max_tool_calls: number;
      token_budget: number;
    };

export interface ResearchCommand {
  id: string;
  mode: "quick_report" | "project_run";
  status: "accepted" | "preparing" | "target_created" | "queued" | "completed" | "failed";
  instruction: string;
  orchestration_stage: string;
  project_id: string | null;
  task_id: string | null;
  snapshot_id: string | null;
  target_resource_type: string | null;
  target_resource_id: string | null;
  target_route: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeSearchResult {
  query: string;
  topic_slugs: string[];
  warnings?: string[];
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

async function readErrorDetail(response: Response): Promise<unknown> {
  // A Response body is a one-shot stream. Read it once, then decide whether it
  // is JSON; this also preserves useful Vite proxy and network error messages.
  const body = await response.text();
  if (!body) return undefined;

  try {
    const payload: unknown = JSON.parse(body);
    if (payload && typeof payload === "object" && "detail" in payload) {
      return payload.detail;
    }
    return payload;
  } catch {
    return body;
  }
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new ApiError(detailMessage(detail), response.status, detail);
  }
  return (await response.json()) as T;
}

export interface ExperimentVariant {
  strategy: string;
  path: string;
  status: string;
  metrics: Record<string, number | null>;
  span_recall: number | null;
  latency_ms: number | null;
  cost_cny: number | null;
  cost_upper_cny: number | null;
  model_calls: number | null;
  model: string;
  semantic_success: boolean | null;
  semantic_review: string;
  error: string;
  issues: string[];
  content: string | null;
  evidence: { rank: number; source_id: string; title: string; version: string; page: number;
    start: number; end: number; text: string }[];
}

export interface FeedbackInput {
  idempotency_key: string;
  category: "citation" | "unsupported" | "incomplete" | "scope" | "source_version" | "execution" | "other";
  note: string;
  reporter: string;
  finding_index: number | null;
  evidence_ids: string[];
}
export interface ReviewInput { expected_revision: number; decision: "accepted" | "rejected"; reviewer: string; note: string }
export interface RerunInput { idempotency_key: string; expected_revision: number; resolution_note: string; token_budget?: number; context_max_tokens?: number }
export interface RecheckInput { decision: "resolved" | "unresolved"; reviewer: string; note: string }
export interface RunFeedback {
  id: string; run_id: string; status: "pending" | "accepted" | "rejected" | "resolved";
  revision: number; request: FeedbackInput; decision: ReviewInput | null;
  anchor: { snapshot_id: string; snapshot_sha256: string; output_sha256: string | null;
    artifact_id: string | null; assertion: string | null; run_revision: number; run_status: string;
    evidence: { evidence_id: string; quote: string; source_version: string; location: JsonObject }[] };
  created_at: string; updated_at: string;
}
export interface RunRecheck {
  id: string; feedback_id: string; parent_run_id: string; child_run_id: string;
  child_status: string; child_snapshot_id: string; child_output_id: string | null; child_artifact_id: string | null;
  request: RerunInput; recheck: RecheckInput | null;
  recheck_anchor: { run_revision: number; status: string; snapshot_id: string; output_sha256: string | null } | null;
  created_at: string; checked_at: string | null;
}
export interface FeedbackList { feedback: RunFeedback[]; links: RunRecheck[] }
export interface SemanticObservationData {
  available: boolean; annotation_mode?: string; publication_gate_enabled?: boolean;
  summary: { planned_runs: number; total_findings: number; human_labeled: number; unlabeled: number;
    judge_unavailable_on_labeled: number; agreement_on_labeled: number | null; uncovered_categories: string[] } | null;
  records: { task_id: string; status: string; run_id: string; snapshot_sha256: string; output_sha256: string;
    findings: { id: string; assertion: string; verdict: string | null; reason: string; human_label: string | null; human_note: string;
      citations: { evidence_id: string; source_version: string; title: string; page_start: string; page_end: string; quote: string }[] }[] }[];
}

export interface ExperimentSummary {
  id: string;
  name: string;
  kind: "retrieval" | "generation";
  planned: number;
  recorded: number;
  task_count: number;
  status_counts: Record<string, number>;
  splits: string[];
  model: string;
  decision: string;
  limitations: string[];
}

export interface ExperimentTask {
  id: string;
  question: string;
  split: string;
  variants: ExperimentVariant[];
}

export interface ExperimentDetail {
  experiment: ExperimentSummary;
  tasks: ExperimentTask[];
}

export interface ExperimentTaskDetail {
  task_id: string;
  question: string;
  split: string;
  path: string;
  variants: ExperimentVariant[];
}

export type ProjectionTarget = "qdrant" | "neo4j" | "obsidian" | "all";
export type ProjectionRebuild = {
  id: string;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  last_error: string | null;
  payload: {
    target: ProjectionTarget;
    collection_slug: string | null;
    result?: { chunks: number; entities: number; relations: number; topics: number };
  };
};

export const api = {
  submitRebuild: (input: { target: ProjectionTarget; collection_slug: string | null; confirmed: true }, key: string) =>
    request<ProjectionRebuild>("/api/v1/runtime/rebuilds", {
      method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify(input),
    }),
  rebuild: (id: string) => request<ProjectionRebuild>(`/api/v1/runtime/rebuilds/${path(id)}`),
  retryRebuild: (id: string) => request<ProjectionRebuild>(`/api/v1/runtime/rebuilds/${path(id)}/retry`, { method: "POST" }),
  semanticObservation: () => request<SemanticObservationData>("/api/experiments/semantic-observation"),
  experiments: () => request<{ experiments: ExperimentSummary[]; unavailable: { id: string; reason: string }[] }>("/api/experiments"),
  experiment: (id: string) => request<ExperimentDetail>(`/api/experiments/${path(id)}`),
  experimentTask: (id: string, taskId: string, retrievalPath: string) =>
    request<ExperimentTaskDetail>(`/api/experiments/${path(id)}/tasks/${path(taskId)}${query({ path: retrievalPath })}`),
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
  updateTaskGoal: (taskId: string, expected_revision: number, goal: string) => request<WorkspaceTask>(`/api/workspace-tasks/${path(taskId)}`, { method: "PATCH", body: JSON.stringify({ expected_revision, goal }) }),
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
  submitIngestion: (input: { collection?: string; sources: string[]; pdf_max_pages: number }) =>
    request<KnowledgeIngestion>("/api/knowledge/ingestions", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  submitAndExecuteIngestion: (input: { collection?: string; sources: string[]; pdf_max_pages: number }) =>
    request<KnowledgeIngestion>("/api/knowledge/ingestions/execute", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  retryIngestion: (ingestionId: string) =>
    request<KnowledgeIngestion>(`/api/knowledge/ingestions/${path(ingestionId)}/retry`, {
      method: "POST",
    }),
  moveIngestionCollection: (ingestionId: string, collection: string) =>
    request<KnowledgeIngestion>(`/api/knowledge/ingestions/${path(ingestionId)}/collection`, {
      method: "PATCH",
      body: JSON.stringify({ collection }),
    }),
  candidates: (ingestionId: string, status = "draft") =>
    request<KnowledgeCandidate[]>(
      `/api/knowledge/ingestions/${path(ingestionId)}/candidates${query({ status })}`,
    ),
  candidatePage: (
    ingestionId: string,
    options: {
      status?: string;
      kind?: "entity" | "relation";
      paperId?: string;
      minConfidence?: number;
      offset?: number;
      limit?: number;
    } = {},
  ) =>
    request<KnowledgeCandidatePage>(
      `/api/knowledge/ingestions/${path(ingestionId)}/candidate-page${query({
        status: options.status ?? "draft",
        kind: options.kind,
        paper_id: options.paperId,
        min_confidence: options.minConfidence,
        offset: options.offset ?? 0,
        limit: options.limit ?? 25,
      })}`,
    ),
  decideCandidate: (candidateId: string, decision: "approve" | "reject" | "defer") =>
    request<unknown>(`/api/knowledge/candidates/${path(candidateId)}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  decideCandidatesBulk: (
    ingestionId: string,
    candidateIds: string[],
    decision: "approve" | "reject" | "defer",
  ) => request<BulkCandidateDecisionResult>(
    `/api/knowledge/ingestions/${path(ingestionId)}/candidates/bulk-decision`,
    { method: "POST", body: JSON.stringify({ candidate_ids: candidateIds, decision }) },
  ),
  autoApproveHighConfidence: (ingestionId: string) =>
    request<ConfidenceAutoApprovalResult>(
      `/api/knowledge/ingestions/${path(ingestionId)}/auto-approve-high-confidence`,
      { method: "POST" },
    ),
  approveReadyCandidates: (ingestionId: string) =>
    request<BulkApprovalResult>(`/api/knowledge/ingestions/${path(ingestionId)}/approve-ready`, {
      method: "POST",
    }),
  searchKnowledge: (searchQuery: string, collectionSlugs: string[]) => {
    const params = new URLSearchParams({ q: searchQuery, top_k: "8" });
    for (const slug of collectionSlugs) params.append("collection_slug", slug);
    return request<KnowledgeSearchResult>(`/api/knowledge/search?${params.toString()}`);
  },
  knowledgeHealth: () => request<KnowledgeHealth>("/api/knowledge/health"),
  runtimeOverview: () => request<RuntimeOverview>("/api/v1/runtime/overview"),
  runtimeWork: (options: { kind?: string; status?: string; cursor?: string; limit?: number } = {}) =>
    request<RuntimeWorkPage>(`/api/v1/runtime/work${query({
      kind: options.kind,
      status: options.status,
      cursor: options.cursor,
      limit: options.limit ?? 30,
    })}`),
  retryProjection: (eventId: string) =>
    request<RuntimeWorkItem>(`/api/v1/runtime/projections/${path(eventId)}/retry`, {
      method: "POST",
    }),
  submitResearchCommand: (input: ResearchCommandInput, idempotencyKey: string) =>
    request<ResearchCommand>("/api/v1/research-commands", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(input),
    }),
  researchCommand: (commandId: string) =>
    request<ResearchCommand>(`/api/v1/research-commands/${path(commandId)}`),
  retryResearchCommand: (commandId: string) =>
    request<ResearchCommand>(`/api/v1/research-commands/${path(commandId)}/retry`, {
      method: "POST",
    }),
  reports: () => request<ResearchReport[]>("/api/reports"),
  report: (reportId: string) => request<ResearchReport>(`/api/reports/${path(reportId)}`),
  submitReport: (input: {
    query: string;
    collection_slugs: string[];
    top_k: number;
    report_depth: "brief" | "standard" | "deep";
  }) => request<ResearchReport>("/api/reports", { method: "POST", body: JSON.stringify(input) }),
  executeReport: (reportId: string) =>
    request<ResearchReport>(`/api/reports/${path(reportId)}/execute`, { method: "POST" }),
  retryReport: (reportId: string) =>
    request<ResearchReport>(`/api/reports/${path(reportId)}/retry`, { method: "POST" }),
  submitAndExecuteReport: async (input: {
    query: string;
    collection_slugs: string[];
    top_k: number;
    report_depth: "brief" | "standard" | "deep";
  }) => {
    return request<ResearchReport>("/api/reports/execute", {
      method: "POST",
      body: JSON.stringify(input),
    });
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
  feedback: (runId: string) => request<FeedbackList>(`/api/agent-runs/${path(runId)}/feedback`),
  createFeedback: (runId: string, input: FeedbackInput) => request<RunFeedback>(`/api/agent-runs/${path(runId)}/feedback`, { method: "POST", body: JSON.stringify(input) }),
  reviewFeedback: (runId: string, id: string, input: ReviewInput) => request<RunFeedback>(`/api/agent-runs/${path(runId)}/feedback/${path(id)}/review`, { method: "POST", body: JSON.stringify(input) }),
  rerunFeedback: (runId: string, id: string, input: RerunInput) => request<AgentRun>(`/api/agent-runs/${path(runId)}/feedback/${path(id)}/rerun`, { method: "POST", body: JSON.stringify(input) }),
  recheckFeedback: (parentId: string, id: string, input: RecheckInput) => request<{ id: string; recheck: RecheckInput }>(`/api/agent-runs/${path(parentId)}/rechecks/${path(id)}`, { method: "POST", body: JSON.stringify(input) }),
  exportFeedback: (runId: string, id: string, version: string) => request<JsonObject>(`/api/agent-runs/${path(runId)}/feedback/${path(id)}/dev-candidate`, { method: "POST", body: JSON.stringify({ target_dev_version: version }) }),
  cancelRun: (runId: string) => request<AgentRun>(`/api/agent-runs/${path(runId)}/cancel`, { method: "POST" }),
  resumeRun: (runId: string) => request<AgentRun>(`/api/agent-runs/${path(runId)}/resume`, { method: "POST" }),
  reviewRun: (runId: string, action: "rerun" | "close") =>
    request<AgentRun>(`/api/agent-runs/${path(runId)}/review`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
};
