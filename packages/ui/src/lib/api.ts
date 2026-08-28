const API_BASE = "/api/backend";

export type CommitteePhase = "drafting" | "reviewing" | "finalizing";

export type CodexAuthState =
  | "unavailable"
  | "disconnected"
  | "pending"
  | "connected"
  | "error";

export interface CodexAuthStatus {
  state: CodexAuthState;
  auth_mode?: string | null;
  email?: string | null;
  plan_type?: string | null;
  login_id?: string | null;
  verification_url?: string | null;
  user_code?: string | null;
  error?: string | null;
}

export interface CodexDeviceLogin {
  login_id: string;
  verification_url: string;
  user_code: string;
}

export class CodexAuthRequestError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "CodexAuthRequestError";
  }
}

async function codexAuthJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}/codex/auth${path}`, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Keep the HTTP status text when the response has no JSON body.
    }
    throw new CodexAuthRequestError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

export function getCodexAuthStatus(): Promise<CodexAuthStatus> {
  return codexAuthJson<CodexAuthStatus>("/status", { cache: "no-store" });
}

export function startCodexDeviceLogin(): Promise<CodexDeviceLogin> {
  return codexAuthJson<CodexDeviceLogin>("/device/start", { method: "POST" });
}

export function cancelCodexDeviceLogin(): Promise<{ status: string }> {
  return codexAuthJson<{ status: string }>("/device/cancel", { method: "POST" });
}

// Inline action chip surfaced when the Executive fires a side-effecting
// tool (DM, schedule, workflow, roster mutation, alert, etc). Read-only
// tools never produce one. See backend `action_chips.SIDE_EFFECTING_TOOLS`
// for the canonical instrumented set; chip metadata is freezing once the
// `done` event arrives. Not persisted across sessions yet — see the
// `chat:` block in architecture-facts.yaml for the deferred persistence
// limitation.
export interface ActionTaken {
  type: "action_taken";
  tool: string;
  summary: string;
  target?: string | null;
  link?: string | null;
  iteration?: number;
}

export interface ChatChunk {
  type:
    | "chunk"
    | "done"
    | "error"
    | "thinking"
    | "phase"
    | "committee_critique";
  content?: string;
  session_id?: string;
  message?: string;
  // committee fields (when type === "phase" or "committee_critique")
  phase?: CommitteePhase;
  reviewer?: string;
  severity?: "low" | "medium" | "high";
}

export type DebugEventKind =
  | "knowledge_retrieved"
  | "routing_decision"
  | "specialist_start"
  | "specialist_done"
  | "synthesis_start"
  | "synthesis_done"
  | "skill_invocation"
  | "turn_complete"
  | "turn_error"
  | "committee_review_start"
  | "committee_review_done"
  | "committee_revision_start";

export interface DebugEvent {
  type: "debug_event";
  kind: DebugEventKind;
  ts: number;
  data: Record<string, unknown>;
  turn_id?: string | null;
}

// ---- Ask OE page context (panel-only) ----
// Sent with panel turns so the Executive knows what page/form the user is
// looking at. Mirrors PageContext / PageFormDescriptor in the backend's
// api/models.py. The backend renders it into the USER turn (cache-safe).

export interface PageFormField {
  name: string;
  label: string;
  type: "text" | "textarea" | "number" | "boolean" | "select" | "json";
  description?: string;
  options?: string[];
  value?: unknown;
  required?: boolean;
}

export interface PageFormDescriptor {
  form_id: string;
  title: string;
  description?: string;
  fields: PageFormField[];
}

export interface PageContext {
  route: string;
  title: string;
  guide_section_id?: string | null;
  summary?: string;
  form?: PageFormDescriptor | null;
}

// Emitted when the Executive calls propose_form_values: suggested values
// for the form named in the page context. The panel applies them into the
// registered form as highlighted suggestions — never auto-submitted.
export interface FormPatch {
  type: "form_patch";
  form_id: string;
  fields: Record<string, unknown>;
  rationale?: string;
  iteration?: number;
}

export type StreamItem = ChatChunk | DebugEvent | ActionTaken | FormPatch;

export interface StreamChatOptions {
  committeeReview?: boolean;
  // Files to attach to this turn. When non-empty, the request switches to
  // the multipart /chat/upload route; documents are auto-indexed into
  // Company Resources and images are forwarded as vision blocks.
  files?: File[];
  // Ask OE panel only — what page/form the user is looking at. Ignored on
  // the multipart route (the panel doesn't support attachments).
  pageContext?: PageContext;
}

export async function* streamChat(
  message: string,
  sessionId?: string,
  opts?: StreamChatOptions
): AsyncGenerator<StreamItem> {
  const files = opts?.files ?? [];
  const committeeReview = opts?.committeeReview ?? false;

  let response: Response;
  if (files.length > 0) {
    const form = new FormData();
    form.append("message", message);
    if (sessionId) form.append("session_id", sessionId);
    form.append("committee_review", String(committeeReview));
    for (const file of files) form.append("files", file, file.name);
    response = await fetch(`${API_BASE}/chat/upload`, {
      method: "POST",
      body: form,
    });
  } else {
    response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        session_id: sessionId,
        committee_review: committeeReview,
        page_context: opts?.pageContext ?? undefined,
      }),
    });
  }

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      // body wasn't JSON — fall through with statusText
    }
    throw new Error(`Chat request failed: ${detail}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          const data: StreamItem = JSON.parse(line.slice(6));
          yield data;
        } catch {
          // skip malformed lines
        }
      }
    }
  }
}

export interface OnboardStatus {
  session_id: string;
  current_step: number;
  total_steps: number;
  current_question: string | null;
  current_step_required: boolean;
  progress_percent: number;
  completed: boolean;
}

export async function startOnboarding(): Promise<OnboardStatus> {
  const res = await fetch(`${API_BASE}/onboard/start`);
  if (!res.ok) throw new Error("Failed to start onboarding");
  return res.json();
}

export async function submitOnboardAnswer(
  sessionId: string,
  answer: string
): Promise<OnboardStatus> {
  const res = await fetch(`${API_BASE}/onboard/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, answer }),
  });
  if (!res.ok) throw new Error("Failed to submit answer");
  return res.json();
}

export interface CompanyProfile {
  name: string;
  industry: string;
  stage: string;
  founding_year: number | null;
  headcount: number | null;
  annual_revenue_arr: number | null;
  mission: string;
  vision: string;
  target_customer: { profile: string; pain_points: string[] };
  competitive_landscape: { primary_competitors: string[]; competitive_advantages: string[] };
  org_structure: { departments: string[]; leadership_team: string[] };
  strategic_priorities: { current_year: string[]; north_star_metric: string };
  culture: { values: string[]; operating_principles: string[] };
  financials: { burn_rate_monthly: number | null; runway_months: number | null; key_metrics: Record<string, unknown> };
}

export async function getCompanyProfile(): Promise<CompanyProfile> {
  const res = await fetch(`${API_BASE}/company-profile`);
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

export async function updateCompanyProfile(patch: Partial<CompanyProfile>): Promise<CompanyProfile> {
  const res = await fetch(`${API_BASE}/company-profile`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error("Failed to update profile");
  return res.json();
}

export async function uploadDocument(
  file: File,
  domain = "general"
): Promise<{ filename: string; chunks_indexed: number; domain: string }> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("domain", domain);

  const res = await fetch(`${API_BASE}/documents`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) throw new Error("Failed to upload document");
  return res.json();
}

export interface CompanyDoc {
  filename: string;
  size_bytes: number;
  modified_at: number;
}

export async function listDocuments(): Promise<CompanyDoc[]> {
  const res = await fetch(`${API_BASE}/documents`);
  if (!res.ok) throw new Error("Failed to list documents");
  const data = await res.json();
  return data.documents;
}

export async function deleteDocument(filename: string): Promise<void> {
  const res = await fetch(`${API_BASE}/documents/${encodeURIComponent(filename)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete document");
}

export interface CompanyDocContent {
  filename: string;
  content: string;
}

export async function getDocument(filename: string): Promise<CompanyDocContent> {
  const res = await fetch(`${API_BASE}/documents/${encodeURIComponent(filename)}`);
  if (!res.ok) throw new Error("Failed to fetch document content");
  return res.json();
}

export interface BuiltinFileMeta {
  domain: string;
  filename: string;
  size_bytes: number;
}

export interface BuiltinFileContent {
  domain: string;
  filename: string;
  content: string;
}

export async function listBuiltinFiles(): Promise<BuiltinFileMeta[]> {
  const res = await fetch(`${API_BASE}/knowledge/builtin`);
  if (!res.ok) throw new Error("Failed to fetch built-in files");
  const data = await res.json();
  return data.files;
}

export async function getBuiltinFile(
  domain: string,
  filename: string
): Promise<BuiltinFileContent> {
  const res = await fetch(`${API_BASE}/knowledge/builtin/${domain}/${filename}`);
  if (!res.ok) throw new Error("Failed to fetch file content");
  return res.json();
}

export async function createBuiltinFile(
  domain: string,
  filename: string,
  content: string
): Promise<{ chunks_indexed: number }> {
  const res = await fetch(`${API_BASE}/knowledge/builtin`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domain, filename, content }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Failed to create file");
  }
  return res.json();
}

export async function updateBuiltinFile(
  domain: string,
  filename: string,
  content: string
): Promise<{ chunks_indexed: number }> {
  const res = await fetch(`${API_BASE}/knowledge/builtin/${domain}/${filename}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domain, filename, content }),
  });
  if (!res.ok) throw new Error("Failed to save file");
  return res.json();
}

export async function deleteBuiltinFile(domain: string, filename: string): Promise<void> {
  const res = await fetch(`${API_BASE}/knowledge/builtin/${domain}/${filename}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete file");
}

// ---------------------------------------------------------------------------
// Failures (negative learnings) — same shape as builtin, separate routes.
// ---------------------------------------------------------------------------

export async function listFailureFiles(): Promise<BuiltinFileMeta[]> {
  const res = await fetch(`${API_BASE}/knowledge/failures`);
  if (!res.ok) throw new Error("Failed to fetch failure files");
  const data = await res.json();
  return data.files;
}

export async function getFailureFile(
  domain: string,
  filename: string
): Promise<BuiltinFileContent> {
  const res = await fetch(`${API_BASE}/knowledge/failures/${domain}/${filename}`);
  if (!res.ok) throw new Error("Failed to fetch failure content");
  return res.json();
}

export async function createFailureFile(
  domain: string,
  filename: string,
  content: string
): Promise<{ chunks_indexed: number }> {
  const res = await fetch(`${API_BASE}/knowledge/failures`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domain, filename, content }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Failed to create failure file");
  }
  return res.json();
}

export async function updateFailureFile(
  domain: string,
  filename: string,
  content: string
): Promise<{ chunks_indexed: number }> {
  const res = await fetch(`${API_BASE}/knowledge/failures/${domain}/${filename}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domain, filename, content }),
  });
  if (!res.ok) throw new Error("Failed to save failure file");
  return res.json();
}

export async function deleteFailureFile(domain: string, filename: string): Promise<void> {
  const res = await fetch(`${API_BASE}/knowledge/failures/${domain}/${filename}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete failure file");
}

// ---------------------------------------------------------------------------
// Knowledge search — "what would RAG retrieve" diagnostic
// ---------------------------------------------------------------------------

export type KnowledgeSourceType = "builtin" | "company" | "failures" | "external";

export interface KnowledgeSearchRequest {
  query: string;
  domain_filter?: string[];
  specialist?: string;
  n_builtin?: number;
  n_company?: number;
  n_failures?: number;
  n_external?: number;
  include?: KnowledgeSourceType[];
}

export interface KnowledgeSearchHit {
  filename: string;
  domain: string;
  source: string | null;
  source_id: string | null;
  source_url: string | null;
  license: string | null;
  publisher: string | null;
  chunk_index: number | null;
  distance: number;
  text: string;
}

export interface KnowledgeSearchResponse {
  query: string;
  effective_domains: string[] | null;
  specialists_that_would_see_this: string[];
  builtin: KnowledgeSearchHit[];
  company: KnowledgeSearchHit[];
  failures: KnowledgeSearchHit[];
  external: KnowledgeSearchHit[];
}

export async function searchKnowledge(
  body: KnowledgeSearchRequest
): Promise<KnowledgeSearchResponse> {
  const res = await fetch(`${API_BASE}/knowledge/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Knowledge search failed");
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// External / OER reference library (read-only)
// ---------------------------------------------------------------------------

export interface ExternalSourceInfo {
  id: string;
  title: string;
  publisher: string;
  license: string;
  phase: number;
  domains: string[];
  type: string;
  url: string;
  slug: string | null;
  chunks: number;
  files: number;
  is_ingested: boolean;
  last_fetched_at: number | null;
}

export interface ExternalSourcesResponse {
  sources: ExternalSourceInfo[];
  total_chunks: number;
}

export interface ExternalPeekChunk {
  domain: string;
  filename: string;
  chunk_index: number;
  text: string;
}

export interface ExternalPeekResponse {
  source_id: string;
  chunks: ExternalPeekChunk[];
}

export async function listExternalSources(): Promise<ExternalSourcesResponse> {
  const res = await fetch(`${API_BASE}/knowledge/external`);
  if (!res.ok) throw new Error("Failed to list reference sources");
  return res.json();
}

export async function peekExternalSource(
  sourceId: string,
  limit = 5
): Promise<ExternalPeekResponse> {
  const res = await fetch(
    `${API_BASE}/knowledge/external/${encodeURIComponent(sourceId)}/peek?limit=${limit}`
  );
  if (!res.ok) throw new Error("Failed to peek source");
  return res.json();
}

export interface SkillMeta {
  name: string;
  category: string;
  description: string;
  when_to_use: string;
  source: "builtin" | "company";
  filename: string;
}

export interface SkillDetail extends SkillMeta {
  body: string;
}

export interface SkillSearchHit {
  name: string;
  category: string;
  description: string;
  when_to_use: string;
  source: "builtin" | "company";
  score: number;
}

export async function listSkills(): Promise<SkillMeta[]> {
  const res = await fetch(`${API_BASE}/skills`);
  if (!res.ok) throw new Error("Failed to list skills");
  const data = await res.json();
  return data.skills;
}

export async function getSkill(name: string): Promise<SkillDetail> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error("Failed to load skill");
  return res.json();
}

export async function searchSkills(
  q: string,
  n = 5
): Promise<SkillSearchHit[]> {
  const params = new URLSearchParams({ q, n: String(n) });
  const res = await fetch(`${API_BASE}/skills/search?${params.toString()}`);
  if (!res.ok) throw new Error("Failed to search skills");
  const data = await res.json();
  return data.results;
}

export async function deleteSkill(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Failed to delete skill");
  }
}

export interface SessionSummary {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export async function listSessions(): Promise<SessionSummary[]> {
  const res = await fetch(`${API_BASE}/sessions`);
  if (!res.ok) throw new Error("Failed to list sessions");
  return res.json();
}

export async function getSessionMessages(sessionId: string): Promise<ChatMessage[]> {
  const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/messages`);
  if (!res.ok) throw new Error("Failed to load session messages");
  return res.json();
}

export async function deleteSession(sessionId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete session");
}

export interface SuggestedPromptsResponse {
  prompts: string[];
  subtitle: string;
  context_quality: "rich" | "thin" | "empty";
}

export async function getSuggestedPrompts(
  signal?: AbortSignal,
): Promise<SuggestedPromptsResponse> {
  const res = await fetch(`${API_BASE}/chat/suggested-prompts`, { signal });
  if (!res.ok) throw new Error("Failed to load suggested prompts");
  return res.json();
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  // Inline action chips for assistant messages — populated from
  // `action_taken` SSE events during the live turn, and persisted to
  // `chat_messages.action_chips` so reopening a saved session restores
  // them (the backend re-attaches them here via load_messages).
  actions?: ActionTaken[];
}

export interface Decision {
  id: number;
  timestamp: string;
  domain: string;
  summary: string;
  rationale: string;
  outcome: string;
  tags: string;
}

export interface Initiative {
  id: number;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
  summary: string;
}

export interface Advice {
  id: number;
  timestamp: string;
  domain: string;
  query_summary: string;
  advice_summary: string;
}

export async function listDecisions(): Promise<Decision[]> {
  const res = await fetch(`${API_BASE}/memories/decisions`);
  if (!res.ok) throw new Error("Failed to list decisions");
  return res.json();
}

export async function updateDecision(id: number, patch: Partial<Omit<Decision, "id" | "timestamp">>): Promise<Decision> {
  const res = await fetch(`${API_BASE}/memories/decisions/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error("Failed to update decision");
  return res.json();
}

export async function deleteDecision(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/memories/decisions/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to delete decision");
}

export async function listInitiatives(): Promise<Initiative[]> {
  const res = await fetch(`${API_BASE}/memories/initiatives`);
  if (!res.ok) throw new Error("Failed to list initiatives");
  return res.json();
}

export async function updateInitiative(id: number, patch: Partial<Omit<Initiative, "id" | "created_at" | "updated_at">>): Promise<Initiative> {
  const res = await fetch(`${API_BASE}/memories/initiatives/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error("Failed to update initiative");
  return res.json();
}

export async function deleteInitiative(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/memories/initiatives/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to delete initiative");
}

export async function listAdvice(): Promise<Advice[]> {
  const res = await fetch(`${API_BASE}/memories/advice`);
  if (!res.ok) throw new Error("Failed to list advice");
  return res.json();
}

export async function updateAdvice(id: number, patch: Partial<Omit<Advice, "id" | "timestamp">>): Promise<Advice> {
  const res = await fetch(`${API_BASE}/memories/advice/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error("Failed to update advice");
  return res.json();
}

export async function deleteAdvice(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/memories/advice/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to delete advice");
}

export interface ScheduledAction {
  id: number;
  created_at: string;
  run_at: string;
  channel: string;
  channel_ref: string;
  intent_text: string;
  originating_session_id: string | null;
  status: string;
  attempts: number;
  last_error: string;
  // `kind` distinguishes recurring rhythms (principal_brief_morning,
  // executive_reflection, dept_cadence, …) from one-off follow-ups (ad_hoc)
  // and internal heartbeats (nudge_scan, external_monitor_scan). Always on
  // the wire from GET /scheduled — the Pulse page groups rows by it.
  kind: string;
  department: string;
  assigned_to_person_id: number | null;
  awaiting_response_since: string | null;
  scope_key: string | null;
}

export async function listScheduledActions(
  status: string = "pending",
  limit: number = 100,
  signal?: AbortSignal,
  order: "asc" | "desc" = "asc",
): Promise<ScheduledAction[]> {
  const params = new URLSearchParams({ status, limit: String(limit), order });
  const res = await fetch(`${API_BASE}/scheduled?${params.toString()}`, { signal });
  if (!res.ok) throw new Error("Failed to list scheduled actions");
  return res.json();
}

export async function cancelScheduledAction(id: number): Promise<ScheduledAction> {
  const res = await fetch(`${API_BASE}/scheduled/${id}`, { method: "DELETE" });
  if (res.status === 409) {
    throw new Error("Action is no longer pending — can't cancel.");
  }
  if (res.status === 401 || res.status === 503) {
    throw new Error(
      "Cancel is gated by SCHEDULED_ADMIN_TOKEN. The UI doesn't forward this header yet — cancel from a loopback client or unset the token.",
    );
  }
  if (!res.ok) throw new Error("Failed to cancel scheduled action");
  return res.json();
}

// ----------------------------------------------------------------------------
// Workflows
// ----------------------------------------------------------------------------

export interface WorkflowStepDef {
  id: string;
  title: string;
  description: string;
}

export interface WorkflowInputFieldSchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  examples?: unknown[];
  minLength?: number;
  enum?: string[];
}

export interface WorkflowJsonSchema {
  title?: string;
  type?: string;
  properties?: Record<string, WorkflowInputFieldSchema>;
  required?: string[];
}

export type WorkflowSection =
  | "Board"
  | "Capital & Investors"
  | "Growth & GTM"
  | "Product"
  | "People"
  | "Risk, Legal & Crisis"
  | "Operating Cadence";

export interface WorkflowMeta {
  name: string;
  title: string;
  description: string;
  section: WorkflowSection;
  estimated_minutes: number;
  input_schema: WorkflowJsonSchema;
  steps: WorkflowStepDef[];
  is_custom?: boolean;
}

// ---- User-created (dynamic) workflows ----

export interface DynamicInputField {
  name: string;
  label: string;
  description?: string;
  required: boolean;
  multiline: boolean;
}

export type DynamicStep =
  | {
      kind: "specialist";
      id: string;
      title: string;
      description?: string;
      specialist: string;
      goal: string;
      rag_query?: string;
    }
  | {
      kind: "approval_gate";
      id: string;
      title: string;
      description?: string;
      person_id: number;
      question: string;
      timeout_hours?: number;
      on_timeout?: "escalate" | "auto_proceed" | "fail";
    }
  | {
      kind: "synthesis";
      id: string;
      title: string;
      description?: string;
      instructions?: string;
      specialist?: string;
    };

export interface DynamicWorkflowDef {
  name: string;
  title: string;
  description?: string;
  section: WorkflowSection;
  estimated_minutes: number;
  input_fields: DynamicInputField[];
  steps: DynamicStep[];
  cadence?: string | null;
  cadence_person_id?: number | null;
  is_active?: boolean;
  created_at?: string;
  updated_at?: string;
}

// The 8 specialists a dynamic step may consult (matches SPECIALIST_REGISTRY,
// excluding the internal `triage` router).
export const DYNAMIC_SPECIALISTS = [
  "cso",
  "cfo",
  "chro",
  "gc",
  "coo",
  "cmo",
  "cpo",
  "board_comms",
] as const;

export async function listCustomWorkflows(): Promise<DynamicWorkflowDef[]> {
  const res = await fetch(`${API_BASE}/workflows/custom`);
  if (!res.ok) throw new Error("Failed to list custom workflows");
  const data = await res.json();
  return data.definitions;
}

export async function getCustomWorkflow(name: string): Promise<DynamicWorkflowDef> {
  const res = await fetch(`${API_BASE}/workflows/custom/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error("Failed to load custom workflow");
  return res.json();
}

/** Returns the server's validation errors (array) when the response is 422. */
async function _writeCustom(
  url: string,
  method: "POST" | "PUT",
  def: DynamicWorkflowDef
): Promise<DynamicWorkflowDef> {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(def),
  });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = (await res.json()).detail;
    } catch {
      /* keep statusText */
    }
    const msg = Array.isArray(detail) ? detail.join("; ") : String(detail);
    throw new Error(msg);
  }
  return res.json();
}

export function createCustomWorkflow(def: DynamicWorkflowDef): Promise<DynamicWorkflowDef> {
  return _writeCustom(`${API_BASE}/workflows/custom`, "POST", def);
}

export function updateCustomWorkflow(
  name: string,
  def: DynamicWorkflowDef
): Promise<DynamicWorkflowDef> {
  return _writeCustom(`${API_BASE}/workflows/custom/${encodeURIComponent(name)}`, "PUT", def);
}

export async function deleteCustomWorkflow(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/workflows/custom/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete custom workflow");
}

export interface WorkflowSample {
  workflow: string;
  inputs: Record<string, unknown>;
}

export interface WorkflowRunSummary {
  run_id: string;
  workflow_name: string;
  title: string;
  status: "running" | "done" | "error";
  created_at: string;
  updated_at: string;
}

export interface WorkflowRunDetail extends WorkflowRunSummary {
  inputs: Record<string, unknown>;
  artifact: string | null;
  error: string | null;
}

export interface WorkflowEvent {
  type: "run_created" | "step_start" | "step_done" | "artifact" | "done" | "error";
  run_id?: string;
  title?: string;
  workflow?: string;
  step_id?: string;
  step_title?: string;
  summary?: string;
  content?: string;
  sources?: string[];
  message?: string;
  steps?: WorkflowStepDef[];
}

export async function listWorkflows(): Promise<WorkflowMeta[]> {
  const res = await fetch(`${API_BASE}/workflows`);
  if (!res.ok) throw new Error("Failed to list workflows");
  const data = await res.json();
  return data.workflows;
}

export async function getWorkflow(name: string): Promise<WorkflowMeta> {
  const res = await fetch(`${API_BASE}/workflows/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error("Failed to load workflow");
  return res.json();
}

export async function getWorkflowSample(name: string): Promise<WorkflowSample> {
  const res = await fetch(`${API_BASE}/workflows/${encodeURIComponent(name)}/sample`);
  if (!res.ok) throw new Error("Failed to load workflow sample");
  return res.json();
}

export async function listWorkflowRuns(
  workflowName?: string
): Promise<WorkflowRunSummary[]> {
  const params = new URLSearchParams();
  if (workflowName) params.set("workflow", workflowName);
  const res = await fetch(`${API_BASE}/workflows/runs?${params.toString()}`);
  if (!res.ok) throw new Error("Failed to list workflow runs");
  const data = await res.json();
  return data.runs;
}

export async function getWorkflowRun(runId: string): Promise<WorkflowRunDetail> {
  const res = await fetch(`${API_BASE}/workflows/runs/${encodeURIComponent(runId)}`);
  if (!res.ok) throw new Error("Failed to load workflow run");
  return res.json();
}

export async function deleteWorkflowRun(runId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/workflows/runs/${encodeURIComponent(runId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete workflow run");
}

// ---------------------------------------------------------------------------
// Executive Artifacts — unified read-only view over drafted artifacts
// (alerts, source='artifact') + completed workflow run outputs.
// ---------------------------------------------------------------------------

// Keep in sync with the Pydantic models in
// packages/core/openexecutive/api/routes/artifacts.py (ArtifactSummary / ArtifactDetail).
export interface ArtifactSummary {
  id: string; // composite "alert:<id>" | "run:<hex>"
  kind: "draft" | "workflow";
  title: string;
  source_label: string;
  created_at: string;
  preview: string | null;
  status: string;
  severity: string | null;
  archived_at: string | null; // ISO ts when archived; null = active
}

export interface ArtifactDetail extends ArtifactSummary {
  body: string;
  rationale: string | null;
}

export async function listArtifacts(
  opts?: { archived?: boolean }
): Promise<ArtifactSummary[]> {
  const qs = opts?.archived ? "?archived=true" : "";
  const res = await fetch(`${API_BASE}/artifacts${qs}`);
  if (!res.ok) throw new Error("Failed to list artifacts");
  const data = await res.json();
  return data.artifacts;
}

export async function getArtifact(id: string): Promise<ArtifactDetail> {
  const res = await fetch(`${API_BASE}/artifacts/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error("Failed to load artifact");
  return res.json();
}

export async function archiveArtifact(id: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/artifacts/${encodeURIComponent(id)}/archive`,
    { method: "POST" }
  );
  if (!res.ok) throw new Error("Failed to archive artifact");
}

export async function restoreArtifact(id: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/artifacts/${encodeURIComponent(id)}/restore`,
    { method: "POST" }
  );
  if (!res.ok) throw new Error("Failed to restore artifact");
}

export async function deleteArtifact(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/artifacts/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete artifact");
}

// ---------------------------------------------------------------------------
// SME Knowledge Review
// ---------------------------------------------------------------------------

export type ReviewStatus = "pending" | "approved" | "rejected" | "needs_revision";
export type ReviewContentType = "builtin" | "external";
export type ReviewPriority = "low" | "normal" | "high";

export interface ReviewItem {
  item_id: string;
  content_type: ReviewContentType;
  domain: string;
  filename: string;
  status: ReviewStatus;
  priority: ReviewPriority;
  reviewer_notes: string;
  reviewed_at: string | null;
  registered_at: string;
  last_modified_at: string;
}

export interface ReviewAnnotation {
  annotation_id: string;
  item_id: string;
  domain: string;
  correction: string;
  is_active: boolean;
  created_at: string;
}

export interface ReviewItemDetail {
  item: ReviewItem;
  annotations: ReviewAnnotation[];
}

export interface ReviewStats {
  pending: number;
  approved: number;
  rejected: number;
  needs_revision: number;
  total: number;
}

export interface ReviewItemPatch {
  status?: ReviewStatus;
  priority?: ReviewPriority;
  reviewer_notes?: string;
}

export async function listReviewItems(params?: {
  status?: ReviewStatus;
  domain?: string;
  content_type?: ReviewContentType;
  limit?: number;
  offset?: number;
}): Promise<ReviewItem[]> {
  const p = new URLSearchParams();
  if (params?.status) p.set("status", params.status);
  if (params?.domain) p.set("domain", params.domain);
  if (params?.content_type) p.set("content_type", params.content_type);
  if (params?.limit != null) p.set("limit", String(params.limit));
  if (params?.offset != null) p.set("offset", String(params.offset));
  const qs = p.toString();
  const res = await fetch(`${API_BASE}/review/items${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error("Failed to list review items");
  return res.json();
}

export async function getReviewItem(itemId: string): Promise<ReviewItemDetail> {
  const res = await fetch(`${API_BASE}/review/items/${encodeURIComponent(itemId)}`);
  if (!res.ok) throw new Error("Failed to get review item");
  return res.json();
}

export async function getReviewStats(): Promise<ReviewStats> {
  const res = await fetch(`${API_BASE}/review/stats`);
  if (!res.ok) throw new Error("Failed to get review stats");
  return res.json();
}

export async function patchReviewItem(itemId: string, patch: ReviewItemPatch): Promise<ReviewItem> {
  const res = await fetch(`${API_BASE}/review/items/${encodeURIComponent(itemId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error("Failed to update review item");
  return res.json();
}

export async function bulkApproveReviewItems(domain?: string): Promise<{ approved_count: number }> {
  const res = await fetch(`${API_BASE}/review/bulk-approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domain }),
  });
  if (!res.ok) throw new Error("Failed to bulk approve");
  return res.json();
}

export async function listAllAnnotations(activeOnly = true): Promise<ReviewAnnotation[]> {
  const res = await fetch(`${API_BASE}/review/annotations?active_only=${activeOnly}`);
  if (!res.ok) throw new Error("Failed to list annotations");
  return res.json();
}

export async function listItemAnnotations(itemId: string): Promise<ReviewAnnotation[]> {
  const res = await fetch(`${API_BASE}/review/items/${encodeURIComponent(itemId)}/annotations`);
  if (!res.ok) throw new Error("Failed to list annotations");
  return res.json();
}

export async function addAnnotation(itemId: string, correction: string): Promise<ReviewAnnotation> {
  const res = await fetch(`${API_BASE}/review/items/${encodeURIComponent(itemId)}/annotations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ correction }),
  });
  if (!res.ok) throw new Error("Failed to add annotation");
  return res.json();
}

export async function patchAnnotation(
  annotationId: string,
  patch: { correction?: string; is_active?: boolean }
): Promise<void> {
  const res = await fetch(`${API_BASE}/review/annotations/${encodeURIComponent(annotationId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error("Failed to update annotation");
}

export async function deleteAnnotation(annotationId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/review/annotations/${encodeURIComponent(annotationId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete annotation");
}

export async function* runWorkflow(
  name: string,
  inputs: Record<string, unknown>
): AsyncGenerator<WorkflowEvent> {
  const response = await fetch(
    `${API_BASE}/workflows/${encodeURIComponent(name)}/runs`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(inputs),
    }
  );

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = JSON.stringify(body.detail ?? body);
    } catch {
      // body wasn't JSON
    }
    throw new Error(`Workflow request failed: ${detail}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          const data: WorkflowEvent = JSON.parse(line.slice(6));
          yield data;
        } catch {
          // skip malformed lines
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Agent Council
// ---------------------------------------------------------------------------

export interface AgentMeta {
  name: string;
  role: string;
  model: string;
  deep_reasoning: boolean;
  domains: string[];
  has_override: boolean;
}

export interface AgentDetail {
  name: string;
  role: string;
  role_default: string;
  model: string;
  model_default: string;
  deep_reasoning: boolean;
  deep_reasoning_default: boolean;
  prompt: string;
  prompt_default: string;
  domains: string[];
  has_override: boolean;
  overridden_fields: string[];
  updated_at: string | null;
  voice_persona_slug: string | null;
  research_focus: string | null;
  research_focus_default: string | null;
}

export interface AgentHistoryEntry {
  id: number;
  agent_id: string;
  prompt: string | null;
  model: string | null;
  use_deep_reasoning: boolean | null;
  role: string | null;
  voice_persona_slug: string | null;
  research_focus: string | null;
  created_at: string;
}

export interface AgentPatch {
  prompt?: string | null;
  model?: string | null;
  use_deep_reasoning?: boolean | null;
  role?: string | null;
  voice_persona_slug?: string | null;
  research_focus?: string | null;
}

// ---- Voice Personas --------------------------------------------------------

export interface PersonaMeta {
  slug: string;
  display_name: string;
  is_builtin: boolean;
  is_customized: boolean;
}

export interface Persona {
  slug: string;
  display_name: string;
  body: string;
  is_builtin: boolean;
  is_customized: boolean;
  source_notes: string;
}

export async function listPersonas(): Promise<PersonaMeta[]> {
  const res = await fetch(`${API_BASE}/personas`);
  if (!res.ok) throw new Error("Failed to list personas");
  return res.json();
}

export async function getPersona(slug: string): Promise<Persona> {
  const res = await fetch(`${API_BASE}/personas/${encodeURIComponent(slug)}`);
  if (!res.ok) throw new Error("Failed to load persona");
  return res.json();
}

export async function savePersona(slug: string, displayName: string, body: string): Promise<Persona> {
  const res = await fetch(`${API_BASE}/personas/${encodeURIComponent(slug)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName, body }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Failed to save persona");
  }
  return res.json();
}

export async function createPersona(displayName: string, body: string): Promise<Persona> {
  const res = await fetch(`${API_BASE}/personas`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName, body }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Failed to create persona");
  }
  return res.json();
}

export async function resetPersona(slug: string): Promise<Persona> {
  const res = await fetch(`${API_BASE}/personas/${encodeURIComponent(slug)}/reset`, {
    method: "POST",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Failed to reset persona");
  }
  return res.json();
}

export async function deletePersona(slug: string): Promise<void> {
  const res = await fetch(`${API_BASE}/personas/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Failed to delete persona");
  }
}

export async function listAgents(): Promise<AgentMeta[]> {
  const res = await fetch(`${API_BASE}/agents`);
  if (!res.ok) throw new Error("Failed to list agents");
  return res.json();
}

export async function getAgentDetail(agentId: string): Promise<AgentDetail> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}`);
  if (!res.ok) throw new Error("Failed to load agent detail");
  return res.json();
}

export async function patchAgent(agentId: string, patch: AgentPatch): Promise<AgentDetail> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Failed to update agent");
  }
  return res.json();
}

export async function resetAgent(agentId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/override`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to reset agent");
}

export async function listAgentHistory(agentId: string): Promise<AgentHistoryEntry[]> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/history`);
  if (!res.ok) throw new Error("Failed to list history");
  return res.json();
}

export async function rollbackAgent(
  agentId: string,
  historyId: number
): Promise<AgentDetail> {
  const res = await fetch(
    `${API_BASE}/agents/${encodeURIComponent(agentId)}/rollback/${historyId}`,
    { method: "POST" }
  );
  if (!res.ok) throw new Error("Failed to roll back");
  return res.json();
}

export async function testAgent(
  agentId: string,
  body: { query: string; prompt?: string | null; model?: string | null; use_deep_reasoning?: boolean | null }
): Promise<{ response: string }> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Test call failed");
  }
  return res.json();
}

export async function listAgentModels(agentId?: string): Promise<string[]> {
  const qs = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  const res = await fetch(`${API_BASE}/agents/models${qs}`);
  if (!res.ok) throw new Error("Failed to list models");
  return res.json();
}

// ---------------------------------------------------------------------------
// Audit log
// ---------------------------------------------------------------------------

export interface AuditEvent {
  id: number;
  ts: string;
  event_type: string;
  session_id: string | null;
  turn_id: string | null;
  actor: string | null;
  summary: string;
  details: Record<string, unknown>;
}

export interface AuditListResponse {
  items: AuditEvent[];
  total: number;
  limit: number;
  offset: number;
  event_types: string[];
}

export interface AuditQuery {
  event_type?: string;
  session_id?: string;
  actor?: string;
  q?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

export async function listAuditLogs(params: AuditQuery = {}): Promise<AuditListResponse> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const res = await fetch(`${API_BASE}/audit/logs?${qs.toString()}`);
  if (!res.ok) throw new Error("Failed to list audit logs");
  return res.json();
}

export interface AuditEventDetail extends AuditEvent {
  // Un-truncated drill-down payload. Null for rows that pre-date the
  // column or for which no extra payload was captured.
  full: Record<string, unknown> | null;
}

export async function getAuditLog(id: number): Promise<AuditEventDetail> {
  const res = await fetch(`${API_BASE}/audit/logs/${id}`);
  if (!res.ok) throw new Error("Failed to load audit event");
  return res.json();
}

// Session-scoped read used by /audit/session/[id] and the drill-down preview
// in /audit. Returns the full ordered timeline plus a server-derived graph
// (nodes + edges) so the UI doesn't re-derive causality client-side.

export interface AuditGraphNode {
  id: string;
  event_id: number;
  event_type: string;
  kind: string; // "inbound" | "memory" | "knowledge" | "specialist" | "tool" | "cache" | "committee" | "response" | "alert" | "scheduled" | fallback to event_type
  label: string;
  actor: string | null;
  turn_id: string | null;
  ts: string;
}

export interface AuditGraphEdge {
  source: string;
  target: string;
  relation: "order" | "cause" | string;
}

export interface AuditGraph {
  nodes: AuditGraphNode[];
  edges: AuditGraphEdge[];
}

export interface TurnCost {
  turn_id: string | null;
  calls: number;
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
}

export interface CostSummary {
  calls: number;
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  per_turn: TurnCost[];
}

export interface Degradation {
  kind: string;
  reason: string;
  count: number;
  turn_ids: string[];
  detail: string | null;
}

export interface AuditSessionResponse {
  session_id: string;
  events: AuditEvent[];
  graph: AuditGraph;
  channel: string | null;
  cost_summary: CostSummary | null;
  degradations: Degradation[];
}

export async function getAuditSession(
  sessionId: string,
): Promise<AuditSessionResponse> {
  const res = await fetch(
    `${API_BASE}/audit/sessions/${encodeURIComponent(sessionId)}`,
  );
  if (!res.ok) {
    if (res.status === 404) {
      throw new Error(`No events found for session ${sessionId}`);
    }
    throw new Error("Failed to load audit session");
  }
  return res.json();
}

// Cross-session token-usage aggregate used by /audit/usage. Totals plus by-day
// and by-model breakdowns, summed from cache_event rows. `cost_usd` is the
// actual OpenRouter charge captured per call (0 for rows that predate capture).

export interface UsageTotals {
  calls: number;
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface UsageByDay extends UsageTotals {
  day: string; // YYYY-MM-DD (UTC)
}

export interface UsageByModel extends UsageTotals {
  model: string;
}

export interface UsageSummary {
  since: string | null;
  until: string | null;
  totals: UsageTotals;
  by_day: UsageByDay[];
  by_model: UsageByModel[];
}

export async function getAuditUsage(
  params: { since?: string; until?: string } = {},
): Promise<UsageSummary> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const res = await fetch(`${API_BASE}/audit/usage?${qs.toString()}`);
  if (!res.ok) throw new Error("Failed to load token usage");
  return res.json();
}

// ── Fixtures ──────────────────────────────────────────────────────────────────

export interface FixtureDepartmentSummary {
  title: string;
  head: string | null;
}

export interface FixturePersonSummary {
  name: string;
  role: string;
  is_principal: boolean;
}

export interface FixtureSummary {
  name: string;
  display_name: string;
  industry: string;
  stage: string;
  arr: number | null;
  headcount: number | null;
  founding_year: number | null;
  mission: string;
  doc_count: number;
  scenario_count: number;
  departments: FixtureDepartmentSummary[];
  people: FixturePersonSummary[];
  // "curated" = git-tracked YAML seed; "generated" = LLM-authored, DB-backed.
  // Optional for back-compat with older payloads.
  source?: "curated" | "generated";
}

export interface FixtureLoadResult {
  fixture: string;
  display_name: string;
  docs_indexed: number;
  memory_seeded: { decisions: number; initiatives: number; advice_given: number };
}

export async function listFixtures(): Promise<FixtureSummary[]> {
  const res = await fetch(`${API_BASE}/fixtures`);
  if (!res.ok) throw new Error("Failed to list fixtures");
  const data = await res.json();
  return data.fixtures as FixtureSummary[];
}

export async function loadFixture(name: string): Promise<FixtureLoadResult> {
  const res = await fetch(`${API_BASE}/fixtures/${encodeURIComponent(name)}/load`, {
    method: "POST",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to load fixture");
  }
  return res.json() as Promise<FixtureLoadResult>;
}

export interface FixtureStatus {
  active_fixture: string | null;
  has_snapshot: boolean;
}

export interface SnapshotResult {
  snapshot_taken: boolean;
  docs_snapshotted: number;
  memory_snapshotted: { decisions: number; initiatives: number; advice_given: number };
  people_snapshotted: number;
  departments_snapshotted: number;
}

export interface UnloadResult {
  restored_from_backup: boolean;
  display_name: string;
  docs_indexed: number;
  memory_seeded: { decisions: number; initiatives: number; advice_given: number };
  people_seeded: number;
  departments_seeded: number;
}

export async function getFixtureStatus(): Promise<FixtureStatus> {
  const res = await fetch(`${API_BASE}/fixtures/status`);
  if (!res.ok) throw new Error("Failed to fetch fixture status");
  return res.json() as Promise<FixtureStatus>;
}

export async function snapshotCurrentState(): Promise<SnapshotResult> {
  const res = await fetch(`${API_BASE}/fixtures/snapshot`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to snapshot current state");
  }
  return res.json() as Promise<SnapshotResult>;
}

export async function unloadFixture(): Promise<UnloadResult> {
  const res = await fetch(`${API_BASE}/fixtures/unload`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to unload fixture");
  }
  return res.json() as Promise<UnloadResult>;
}

export interface ResetResult {
  reset: boolean;
  departments_seeded: number;
  episodic_cleared: Record<string, number>;
  people_cleared: Record<string, number>;
}

export async function resetAllState(): Promise<ResetResult> {
  const res = await fetch(`${API_BASE}/fixtures/reset`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to reset state");
  }
  return res.json() as Promise<ResetResult>;
}

// ── Generated fixtures (LLM-authored) ───────────────────────────────────────

// A generated fixture bundle. Typed loosely on purpose — the UI only reads a
// few summary fields for the review step; the full object round-trips back to
// the backend on save, which re-validates it against the canonical schema.
export interface GeneratedFixtureBundle {
  profile: {
    name: string;
    industry?: string;
    stage?: string;
    headcount?: number | null;
    annual_revenue_arr?: number | null;
    mission?: string;
    [key: string]: unknown;
  };
  people: Array<{ full_name: string; role?: string; is_principal?: boolean }>;
  departments: Array<{ slug: string; title: string; head_person_name?: string | null }>;
  memory: {
    decisions?: unknown[];
    initiatives?: unknown[];
    advice_given?: unknown[];
    alerts?: unknown[];
  };
  docs: Array<{ filename: string; content?: string }>;
}

export interface GenerateFixtureResult {
  suggested_name: string;
  display_name: string;
  bundle: GeneratedFixtureBundle;
}

export async function generateFixture(
  description: string
): Promise<GenerateFixtureResult> {
  const res = await fetch(`${API_BASE}/fixtures/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to generate fixture");
  }
  return res.json() as Promise<GenerateFixtureResult>;
}

export interface CreateFixtureResult {
  name: string;
  display_name: string;
  source: "generated";
  doc_count: number;
}

export async function createFixture(
  bundle: GeneratedFixtureBundle,
  scenarioDescription: string
): Promise<CreateFixtureResult> {
  const res = await fetch(`${API_BASE}/fixtures`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bundle, scenario_description: scenarioDescription }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to save fixture");
  }
  return res.json() as Promise<CreateFixtureResult>;
}

export async function deleteFixture(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/fixtures/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to delete fixture");
  }
}

// ---------------------------------------------------------------------------
// Departments
// ---------------------------------------------------------------------------

export interface DepartmentCharter {
  mission: string;
  scope: string[];
  out_of_scope: string[];
}

export interface DepartmentConfig {
  slug: string;
  title: string;
  specialist_key: string | null;
  charter: DepartmentCharter;
  authority_level: "auto_execute" | "propose_only" | "escalate";
  head_person_id: number | null;
  head_persona_slug: string | null;
  cadences: Record<string, string>;
  // Department-scoped broadcast channels. When set, OE can post to
  // the department's team room via `send_department_message` instead
  // of (or in addition to) DMing the head.
  slack_channel_id: string | null;
  discord_channel_id: string | null;
  telegram_chat_id: string | null;
}

export type PeriodType = "week" | "month" | "quarter" | "year" | "ongoing";

export interface Goal {
  id: number | null;
  department_slug: string;
  period_type: PeriodType;
  period_value: string;
  key_result: string;
  target: string;
  current: string;
  status: "on_track" | "at_risk" | "off_track";
  created_at: string;
  updated_at: string;
  // When OE last graded this goal (department_check_in workflow, or
  // Phase B: a chat-driven `update_department_goal` tool call). Distinct
  // from `updated_at`, which bumps on any content edit. Empty = never
  // reviewed.
  last_reviewed_at: string;
}

export interface DepartmentState {
  config: DepartmentConfig;
  goals: Goal[];
  headcount: number | null;
  budget_usd: number | null;
  member_person_ids: number[];
  updated_at: string;
}

export async function listDepartments(): Promise<DepartmentState[]> {
  const res = await fetch(`${API_BASE}/departments`);
  if (!res.ok) throw new Error(`Failed to load departments: ${res.statusText}`);
  return res.json();
}

export async function getDepartment(slug: string): Promise<DepartmentState> {
  const res = await fetch(`${API_BASE}/departments/${slug}`);
  if (!res.ok) throw new Error(`Failed to load department: ${res.statusText}`);
  return res.json();
}

export interface DepartmentPatch {
  title?: string;
  charter?: DepartmentCharter;
  authority_level?: DepartmentConfig["authority_level"];
  cadences?: Record<string, string>;
  head_person_id?: number | null;
  headcount?: number | null;
  budget_usd?: number | null;
  slack_channel_id?: string | null;
  discord_channel_id?: string | null;
  telegram_chat_id?: string | null;
}

export async function updateDepartment(slug: string, patch: DepartmentPatch): Promise<DepartmentState> {
  const res = await fetch(`${API_BASE}/departments/${slug}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`Failed to update department: ${res.statusText}`);
  return res.json();
}

export interface DepartmentCreate {
  title: string;
  mission?: string;
}

export async function createDepartment(body: DepartmentCreate): Promise<DepartmentState> {
  const res = await fetch(`${API_BASE}/departments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to create department: ${res.statusText}`);
  return res.json();
}

export async function deleteDepartment(slug: string): Promise<void> {
  const res = await fetch(`${API_BASE}/departments/${slug}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`Failed to delete department: ${res.statusText}`);
}

export interface GoalCreate {
  period_type?: PeriodType;
  period_value: string;
  key_result: string;
  target: string;
  current?: string;
  status?: Goal["status"];
}

export async function createGoal(slug: string, body: GoalCreate): Promise<Goal> {
  const res = await fetch(`${API_BASE}/departments/${slug}/goals`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to create Goal: ${res.statusText}`);
  return res.json();
}

export interface GoalPatch {
  period_type?: PeriodType;
  period_value?: string;
  key_result?: string;
  target?: string;
  current?: string;
  status?: Goal["status"];
}

export async function updateGoal(slug: string, goalId: number, patch: GoalPatch): Promise<Goal> {
  const res = await fetch(`${API_BASE}/departments/${slug}/goals/${goalId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`Failed to update Goal: ${res.statusText}`);
  return res.json();
}

export async function deleteGoal(slug: string, goalId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/departments/${slug}/goals/${goalId}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`Failed to delete Goal: ${res.statusText}`);
}

// ---------------------------------------------------------------------------
// People
// ---------------------------------------------------------------------------

export interface AvailabilityWindow {
  weekdays: number[];
  start_local: string;
  end_local: string;
  timezone: string;
}

export interface Person {
  id: number;
  full_name: string;
  role: string;
  is_principal: boolean;
  department_slugs: string[];
  email: string | null;
  slack_user_id: string | null;
  telegram_chat_id: string | null;
  discord_user_id: string | null;
  preferred_channel: string;
  availability: AvailabilityWindow[];
  authority_scope: string[];
  response_sla_hours: number;
  on_leave_until: string | null;
  reports_to_person_id: number | null;
  archived: boolean;
}

export async function listPeople(): Promise<Person[]> {
  const res = await fetch(`${API_BASE}/people`);
  if (!res.ok) throw new Error(`Failed to load people: ${res.statusText}`);
  return res.json();
}

export async function getPerson(id: number): Promise<Person> {
  const res = await fetch(`${API_BASE}/people/${id}`);
  if (!res.ok) throw new Error(`Failed to load person: ${res.statusText}`);
  return res.json();
}

export interface PersonCreate {
  full_name: string;
  role?: string;
  is_principal?: boolean;
  department_slugs?: string[];
  email?: string | null;
  slack_user_id?: string | null;
  telegram_chat_id?: string | null;
  discord_user_id?: string | null;
  preferred_channel?: string;
  response_sla_hours?: number;
  on_leave_until?: string | null;
  authority_scope?: string[];
  availability?: AvailabilityWindow[];
}

export async function createPerson(body: PersonCreate): Promise<Person> {
  const res = await fetch(`${API_BASE}/people`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to create person: ${res.statusText}`);
  return res.json();
}

export interface PersonPatch {
  full_name?: string;
  role?: string;
  email?: string | null;
  slack_user_id?: string | null;
  telegram_chat_id?: string | null;
  discord_user_id?: string | null;
  preferred_channel?: string;
  response_sla_hours?: number;
  on_leave_until?: string | null;
  clear_on_leave?: boolean;
  department_slugs?: string[];
  authority_scope?: string[];
  availability?: AvailabilityWindow[];
}

export async function updatePerson(id: number, patch: PersonPatch): Promise<Person> {
  const res = await fetch(`${API_BASE}/people/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`Failed to update person: ${res.statusText}`);
  return res.json();
}

export async function archivePerson(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/people/${id}/archive`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to archive person: ${res.statusText}`);
}

// ---------------------------------------------------------------------------
// Talent / executive-search core (clients, engagements, candidates, matching)
// ---------------------------------------------------------------------------

export const CANDIDATE_STAGES = [
  "lead",
  "screened",
  "interviewed",
  "offer",
  "placed",
  "rejected",
] as const;
export type CandidateStage = (typeof CANDIDATE_STAGES)[number];

export const ENGAGEMENT_STATUSES = ["open", "on_hold", "filled", "cancelled"] as const;
export type EngagementStatus = (typeof ENGAGEMENT_STATUSES)[number];

export interface Engagement {
  id: number;
  role_title: string;
  department: string;
  status: EngagementStatus;
  location: string;
  comp_band: string;
  must_haves: string;
  description: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface EngagementCreate {
  role_title: string;
  department?: string;
  status?: EngagementStatus;
  location?: string;
  comp_band?: string;
  must_haves?: string;
  description?: string;
}

export type EngagementPatch = Partial<EngagementCreate>;

export interface Candidate {
  id: number;
  engagement_id: number;
  full_name: string;
  current_title: string;
  current_company: string;
  location: string;
  email: string | null;
  linkedin_url: string | null;
  source: string;
  stage: CandidateStage;
  fit_score: number | null;
  screening_summary: string;
  notes: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface CandidateCreate {
  engagement_id: number;
  full_name: string;
  current_title?: string;
  current_company?: string;
  location?: string;
  email?: string | null;
  linkedin_url?: string | null;
  source?: string;
  stage?: CandidateStage;
  notes?: string;
}

export type CandidatePatch = Partial<Omit<CandidateCreate, "engagement_id" | "stage">>;

export interface CandidateMatch {
  candidate_id: number;
  engagement_id: number;
  stage: string;
  score: number;
}

// ---- Engagements ----

export async function listEngagements(includeArchived = false): Promise<Engagement[]> {
  const res = await fetch(
    `${API_BASE}/engagements?include_archived=${includeArchived}`,
  );
  if (!res.ok) throw new Error(`Failed to load engagements: ${res.statusText}`);
  return res.json();
}

export async function getEngagement(id: number): Promise<Engagement> {
  const res = await fetch(`${API_BASE}/engagements/${id}`);
  if (!res.ok) throw new Error(`Failed to load engagement: ${res.statusText}`);
  return res.json();
}

export async function createEngagement(body: EngagementCreate): Promise<Engagement> {
  const res = await fetch(`${API_BASE}/engagements`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to create engagement: ${res.statusText}`);
  return res.json();
}

export async function updateEngagement(id: number, patch: EngagementPatch): Promise<Engagement> {
  const res = await fetch(`${API_BASE}/engagements/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`Failed to update engagement: ${res.statusText}`);
  return res.json();
}

export async function archiveEngagement(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/engagements/${id}/archive`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to archive engagement: ${res.statusText}`);
}

// ---- Candidates ----

export async function listCandidates(opts: {
  engagementId?: number;
  stage?: CandidateStage;
  includeArchived?: boolean;
} = {}): Promise<Candidate[]> {
  const params = new URLSearchParams({
    include_archived: String(opts.includeArchived ?? false),
  });
  if (opts.engagementId != null) params.set("engagement_id", String(opts.engagementId));
  if (opts.stage) params.set("stage", opts.stage);
  const res = await fetch(`${API_BASE}/candidates?${params.toString()}`);
  if (!res.ok) throw new Error(`Failed to load candidates: ${res.statusText}`);
  return res.json();
}

export async function getCandidate(id: number): Promise<Candidate> {
  const res = await fetch(`${API_BASE}/candidates/${id}`);
  if (!res.ok) throw new Error(`Failed to load candidate: ${res.statusText}`);
  return res.json();
}

export async function createCandidate(body: CandidateCreate): Promise<Candidate> {
  const res = await fetch(`${API_BASE}/candidates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to create candidate: ${res.statusText}`);
  return res.json();
}

export async function updateCandidate(id: number, patch: CandidatePatch): Promise<Candidate> {
  const res = await fetch(`${API_BASE}/candidates/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`Failed to update candidate: ${res.statusText}`);
  return res.json();
}

export async function setCandidateStage(id: number, stage: CandidateStage): Promise<Candidate> {
  const res = await fetch(`${API_BASE}/candidates/${id}/stage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage }),
  });
  if (!res.ok) throw new Error(`Failed to update candidate stage: ${res.statusText}`);
  return res.json();
}

export async function archiveCandidate(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/candidates/${id}/archive`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to archive candidate: ${res.statusText}`);
}

// ---- Talent graph (matching) ----

export async function matchCandidatesForEngagement(
  engagementId: number,
  limit = 10,
): Promise<CandidateMatch[]> {
  const res = await fetch(`${API_BASE}/engagements/${engagementId}/matches?limit=${limit}`);
  if (!res.ok) throw new Error(`Failed to load matches: ${res.statusText}`);
  return res.json();
}

export async function similarCandidates(
  candidateId: number,
  limit = 5,
): Promise<CandidateMatch[]> {
  const res = await fetch(`${API_BASE}/candidates/${candidateId}/similar?limit=${limit}`);
  if (!res.ok) throw new Error(`Failed to load similar candidates: ${res.statusText}`);
  return res.json();
}

export async function reindexTalent(): Promise<{ indexed: number }> {
  const res = await fetch(`${API_BASE}/talent/reindex`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to reindex talent: ${res.statusText}`);
  return res.json();
}

// ---- Offers ----

export const OFFER_STATUSES = [
  "draft",
  "pending_approval",
  "extended",
  "accepted",
  "declined",
  "expired",
  "rescinded",
] as const;
export type OfferStatus = (typeof OFFER_STATUSES)[number];

// Statuses an offer can still move forward from (terminal ones are history).
export const OPEN_OFFER_STATUSES: readonly OfferStatus[] = [
  "draft",
  "pending_approval",
  "extended",
];

export interface Offer {
  id: number;
  candidate_id: number;
  engagement_id: number;
  status: OfferStatus;
  comp_summary: string;
  package_md: string;
  note: string;
  expires_at: string | null;
  extended_at: string;
  decided_at: string;
  approval_run_id: string;
  approved_by_person_id: number | null;
  nudge_action_ids: number[];
  archived: boolean;
  created_at: string;
  updated_at: string;
}

// Mirrors the backend OfferActionResponse: the offer plus what else happened
// (nudges scheduled/cancelled, candidate placed, warnings about sign-off).
export interface OfferActionResponse {
  offer: Offer;
  approval_state: string;
  nudges_scheduled: number;
  cancelled_nudges: number;
  side_effects: string[];
  warnings: string[];
}

// Offer-lifecycle conflicts come back as 409s with a meaningful `detail`
// (e.g. "already has an open offer") — surface that, not just statusText.
async function offerError(res: Response, fallback: string): Promise<Error> {
  try {
    const body = await res.json();
    if (body?.detail) return new Error(String(body.detail));
  } catch {
    // fall through to statusText
  }
  return new Error(`${fallback}: ${res.statusText}`);
}

export async function listOffers(opts: {
  candidateId?: number;
  engagementId?: number;
  status?: OfferStatus;
} = {}): Promise<Offer[]> {
  const params = new URLSearchParams();
  if (opts.candidateId != null) params.set("candidate_id", String(opts.candidateId));
  if (opts.engagementId != null) params.set("engagement_id", String(opts.engagementId));
  if (opts.status) params.set("status", opts.status);
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/offers${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`Failed to load offers: ${res.statusText}`);
  return res.json();
}

export async function createOffer(body: {
  candidate_id: number;
  comp_summary: string;
  note?: string;
}): Promise<Offer> {
  const res = await fetch(`${API_BASE}/offers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await offerError(res, "Failed to create offer");
  return res.json();
}

export async function extendOffer(
  id: number,
  body: { expires_at?: string; expires_in_days?: number; note?: string } = {},
): Promise<OfferActionResponse> {
  const res = await fetch(`${API_BASE}/offers/${id}/extend`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await offerError(res, "Failed to extend offer");
  return res.json();
}

export async function recordOfferDecision(
  id: number,
  decision: OfferStatus,
  note?: string,
): Promise<OfferActionResponse> {
  const res = await fetch(`${API_BASE}/offers/${id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, note }),
  });
  if (!res.ok) throw await offerError(res, "Failed to record offer decision");
  return res.json();
}

// ---------------------------------------------------------------------------
// Today (live dashboard) — was previously named "Morning Brief"
// ---------------------------------------------------------------------------

// An off-track/at-risk goal surfaced inline on the briefing's Department card
// (see today.py GoalBrief) so the card shows substance, not just counts.
export interface GoalBrief {
  key_result: string;
  current: string;
  target: string;
  status: string; // "at_risk" | "off_track"
}

export interface DepartmentBriefItem {
  slug: string;
  title: string;
  authority_level: string;
  goal_count: number;
  at_risk_count: number;
  off_track_count: number;
  awaiting_count: number;
  // The actual problem goals (worst first, capped). Optional so older API
  // builds / test mocks without the field still typecheck.
  attention_goals?: GoalBrief[];
}

export interface PersonBriefItem {
  id: number;
  full_name: string;
  role: string;
  is_principal: boolean;
  preferred_channel: string;
  awaiting_count: number;
  soonest_sla_at: string | null;
  // Enrichment signals (see api/routes/today.py PersonBriefItem).
  status: "on_leave" | "needs_reply" | "awaiting" | "clear";
  awaiting_reply_count: number;
  oldest_awaiting_reply_at: string | null;
  on_leave_until: string | null;
  reachable_now: boolean;
  next_window_at: string | null;
  authority_scope: string[];
  department_slugs: string[];
  last_contact_at: string | null;
  overdue: boolean;
  priority: number;
  insight: string | null;
}

export interface ProposalItem {
  alert_id: number;
  // `headline` is a 160-char excerpt used as the card title; `body` is
  // the full intent text the UI seeds into the chat handoff so the
  // Executive has the full context.
  body: string;
  headline: string;
  routed_to_person_id: number | null;
  suggested_action: string;
  created_at: string;
  topic_tags: string[];
  // Presentation signals from the backend (see briefing/ranking.py).
  // `score` orders the action queue; `category` is "action" (needs a human)
  // or "monitoring" (passive watchlist noise the UI collapses). Optional so
  // older API builds / test mocks still compile.
  score?: number;
  category?: "action" | "monitoring";
  // Why this item is in "Needs you" rather than Monitoring — set only when an
  // external/watchlist signal was pulled into the action lane by its severity
  // (e.g. a large stock move). Null/absent for everything else.
  surfaced_reason?: string | null;
  // Set when this proposal is backed by a decision_instance (a gated calendar
  // booking awaiting approval). The briefing routes Approve/Reject to the
  // /decisions endpoints (which book/cancel server-side) instead of the
  // ack-and-handoff-to-chat flow. Null/absent for ordinary alert proposals.
  decision_instance_id?: number | null;
}

export interface InFlightItem {
  action_id: number;
  intent: string;
  run_at: string;
  kind: string;
  channel: string;
  target: string | null;
  department: string | null;
  overdue: boolean;
}

export interface AwaitingItem {
  person_id: number;
  full_name: string;
  role: string;
  awaiting_count: number;
  oldest_at: string | null;
  overdue: boolean;
}

// One active executive-search engagement, rolled up by pipeline stage for the
// briefing's "Executive Search" section. Mirrors the backend
// briefing.talent_digest.TalentBriefItem.
export interface TalentBriefItem {
  engagement_id: number;
  role_title: string;
  department: string;
  status: string;
  location: string;
  candidate_count: number;
  // Maps a CandidateStage value ("lead", "screened", …) to its count.
  stage_counts: Record<string, number>;
  needs_screening: number;
  offers_out: number;
  // Extended offers within 72h of expiry (or already past it, undecided).
  // Optional to keep older API builds + test mocks compiling.
  offers_expiring_soon?: number;
  stalled_count: number;
}

// Mirrors briefing.onboarding_digest.OnboardingBriefItem.
export interface OnboardingBriefItem {
  plan_id: number;
  full_name: string;
  role: string;
  status: OnboardingStatus;
  current_phase: OnboardingPhase;
  completion_pct: number;
  open_tasks: number;
  overdue_tasks: number;
  days_to_start: number | null;
}

export interface Today {
  departments: DepartmentBriefItem[];
  people: PersonBriefItem[];
  proposals: ProposalItem[];
  // Executive-voice "what's going on" narrative for the briefing header.
  // Null/absent until the backend has generated one.
  narrative?: string | null;
  // In-flight commitments (pending follow-ups/nudges) + people we're awaiting
  // a reply from. Optional/default-empty for older API builds + test mocks.
  in_flight?: InFlightItem[];
  awaiting?: AwaitingItem[];
  // Active executive searches rolled up by pipeline stage. Optional/default-
  // empty for older API builds + test mocks.
  talent?: TalentBriefItem[];
  // New hires currently onboarding, rolled up by progress. Optional/default-
  // empty for older API builds + test mocks.
  onboarding?: OnboardingBriefItem[];
  // Parked client slots in multi-client practice mode (2+ slots). Empty for
  // single-company installs. Optional for older API builds + test mocks.
  practice_clients?: ClientCockpitCard[];
  // The signed-in caller resolved to a Person id on the backend (via
  // x-caller-email). Lets the UI split proposals into "routed to me"
  // vs "across the team". Null when no caller could be resolved.
  // Optional to keep existing test mocks and older API builds compiling.
  caller_person_id?: number | null;
}

export async function getToday(): Promise<Today> {
  const res = await fetch(`${API_BASE}/today`);
  if (!res.ok) throw new Error(`Failed to load today: ${res.statusText}`);
  return res.json();
}

// Mark an alert as read/ack/dismissed. Used by the briefing's
// Approve/Dismiss affordance to groom the "Needs you" queue: any
// non-unread status drops the alert from /today's proposals list.
export async function ackAlert(
  alertId: number,
  status: "read" | "ack" | "dismissed",
): Promise<void> {
  const res = await fetch(`${API_BASE}/alerts/${alertId}/ack`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new Error(`Failed to ack alert ${alertId}: ${res.statusText}`);
}

// Recent self-initiated Executive activity for the briefing rail.
// `kind` is a coarse classifier the UI uses to pick an icon/verb:
// "dm_sent" | "email_sent" | "nudge_sent" | "cadence_sent" |
// "workflow_resumed" | "proposal_routed" | "decision_logged" |
// "advice_given" | "action".
export interface ActivityItem {
  kind: string;
  summary: string;
  actor: string;
  target: string | null;
  department: string | null;
  at: string;
}

export interface ActivityResponse {
  items: ActivityItem[];
}

export async function getActivity(limit: number = 20): Promise<ActivityResponse> {
  const res = await fetch(`${API_BASE}/today/activity?limit=${limit}`);
  if (!res.ok) throw new Error(`Failed to load activity: ${res.statusText}`);
  return res.json();
}

/** One day in the Pulse heartbeat heatmap. `date` is YYYY-MM-DD (UTC). */
export interface DailyActivityCount {
  date: string;
  count: number;
}

export interface DailyActivityResponse {
  /** Dense, oldest → newest; every calendar day present (count 0 when idle). */
  days: DailyActivityCount[];
}

export async function getActivityDaily(
  days: number = 90,
  signal?: AbortSignal,
): Promise<DailyActivityResponse> {
  const res = await fetch(`${API_BASE}/today/activity/daily?days=${days}`, { signal });
  if (!res.ok) throw new Error(`Failed to load activity heatmap: ${res.statusText}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Watch list (external-condition monitors)
// ---------------------------------------------------------------------------

export type WatchlistSignalType =
  | "stock"
  | "rss"
  | "vendor_status"
  | "query"
  | "edgar"
  | "page_watch";
export type WatchlistCadence = "real_time" | "15min" | "hourly" | "daily" | "weekly";
export type WatchlistMode = "active" | "dry_run";
export type WatchlistSeverity = "low" | "medium" | "high" | "urgent";

export interface WatchlistItem {
  id: number;
  slug: string;
  signal_type: string;
  target: string;
  config_json: Record<string, unknown>;
  trigger_json: Record<string, unknown>;
  cadence: string;
  severity_floor: WatchlistSeverity;
  severity_ceiling: WatchlistSeverity;
  route_to_specialist: string;
  route_to_department: string;
  route_to_person_id: number | null;
  mode: string;
  enabled: boolean;
  created_at: string;
  last_polled_at: string | null;
  last_fired_at: string | null;
  fired_count: number;
  dismiss_count: number;
  trust_score: number;
  notes: string;
}

export interface WatchlistSignal {
  id: number;
  watchlist_id: number;
  source_kind: string;
  source_external_id: string;
  captured_at: string;
  normalized_summary: string;
  provenance_url: string;
  severity_hint: string;
  dedup_key: string;
  processed_at: string | null;
  processed_outcome: string | null;
  promoted_alert_id: number | null;
  raw_payload: Record<string, unknown>;
}

export interface WatchlistCreate {
  slug: string;
  signal_type: WatchlistSignalType | string;
  target: string;
  trigger?: Record<string, unknown>;
  config?: Record<string, unknown>;
  cadence?: WatchlistCadence | string;
  severity_floor?: WatchlistSeverity;
  severity_ceiling?: WatchlistSeverity;
  mode?: WatchlistMode | string;
  route_to_specialist?: string;
  notes?: string;
  display_label?: string;
}

export interface WatchlistPatch {
  enabled?: boolean;
  mode?: WatchlistMode | string;
  cadence?: WatchlistCadence | string;
  severity_floor?: WatchlistSeverity;
  severity_ceiling?: WatchlistSeverity;
  trigger?: Record<string, unknown>;
  notes?: string;
}

export async function listWatchlist(options: {
  enabledOnly?: boolean;
  signalType?: string;
} = {}): Promise<WatchlistItem[]> {
  const params = new URLSearchParams();
  if (options.enabledOnly) params.set("enabled_only", "true");
  if (options.signalType) params.set("signal_type", options.signalType);
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/watchlist${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`Failed to load watchlist: ${res.statusText}`);
  return res.json();
}

export async function getWatchlistItem(slug: string): Promise<WatchlistItem> {
  const res = await fetch(`${API_BASE}/watchlist/${encodeURIComponent(slug)}`);
  if (!res.ok) throw new Error(`Failed to load watchlist entry: ${res.statusText}`);
  return res.json();
}

export async function getWatchlistSignals(
  slug: string,
  limit: number = 50,
): Promise<WatchlistSignal[]> {
  const res = await fetch(
    `${API_BASE}/watchlist/${encodeURIComponent(slug)}/signals?limit=${limit}`,
  );
  if (!res.ok) throw new Error(`Failed to load signals: ${res.statusText}`);
  return res.json();
}

export async function createWatchlistItem(body: WatchlistCreate): Promise<WatchlistItem> {
  const res = await fetch(`${API_BASE}/watchlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Failed to create watchlist entry: ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json();
}

export async function patchWatchlistItem(
  slug: string,
  patch: WatchlistPatch,
): Promise<WatchlistItem> {
  const res = await fetch(`${API_BASE}/watchlist/${encodeURIComponent(slug)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Failed to update watchlist entry: ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json();
}

export async function deleteWatchlistItem(slug: string): Promise<void> {
  const res = await fetch(`${API_BASE}/watchlist/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`Failed to delete watchlist entry: ${res.statusText}`);
}

// ---------------------------------------------------------------------------
// Decisions / Trust Ledger (first-climb autonomy)
// ---------------------------------------------------------------------------

export interface DecisionInstance {
  id: number;
  decision_class: string;
  created_at: string;
  department: string;
  originating_session_id: string | null;
  proposed_payload_json: string;
  idempotency_key: string | null;
  gate_mode: string;
  approver_person_id: number | null;
  confidence: number | null;
  status: string;
  resolved_at: string | null;
  resolver_person_id: number | null;
  final_payload_json: string | null;
  external_event_id: string | null;
  reversal_reason: string | null;
  severity: string;
}

export async function approveDecision(
  id: number,
  edits?: Record<string, unknown>,
): Promise<DecisionInstance> {
  const res = await fetch(`${API_BASE}/decisions/${id}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edits: edits ?? null }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Failed to approve decision: ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json();
}

export async function rejectDecision(
  id: number,
  reason = "",
): Promise<DecisionInstance> {
  const res = await fetch(`${API_BASE}/decisions/${id}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Failed to reject decision: ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json();
}


// ── Client-company slots (fractional / multi-client mode) ──────────────────

export interface ClientSlotSummary {
  slug: string;
  display_name: string;
  created_at: string | null;
  saved_at: string | null;
  origin?: string | null;
  has_state: boolean;
  has_mcp_config: boolean;
  doc_count: number;
  industry?: string;
  stage?: string;
  // Engagement metadata (practice-level, lives in meta.json).
  role?: string | null;
  status?: string | null;
  engagement_start?: string | null;
  renewal_date?: string | null;
  retainer?: string | null;
  hours_per_week?: number | null;
  primary_contact?: string | null;
  notes?: string | null;
}

export interface ClientsStatus {
  active: string | null;
  fixture_active: string | null;
  // True while the overnight rotation is switching contexts. Optional for
  // older API builds + test mocks.
  rotation_in_progress?: boolean;
  clients: ClientSlotSummary[];
}

export async function listClients(): Promise<ClientsStatus> {
  const res = await fetch(`${API_BASE}/clients`);
  if (!res.ok) throw new Error("Failed to list clients");
  return res.json() as Promise<ClientsStatus>;
}

export async function createClient(
  displayName: string,
  source: "current" | "blank",
): Promise<{ slug: string; display_name: string; active: boolean }> {
  const res = await fetch(`${API_BASE}/clients`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName, source }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to create client");
  }
  return res.json();
}

export async function activateClient(
  slug: string,
): Promise<{ slug: string; previous?: string | null; mcp_config_changed?: boolean }> {
  const res = await fetch(
    `${API_BASE}/clients/${encodeURIComponent(slug)}/activate`,
    { method: "POST" },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to activate client");
  }
  return res.json();
}

export async function saveActiveClient(): Promise<{ slug: string; saved: boolean }> {
  const res = await fetch(`${API_BASE}/clients/save`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to save client");
  }
  return res.json();
}

export async function deleteClient(slug: string): Promise<void> {
  const res = await fetch(`${API_BASE}/clients/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to delete client");
  }
}

// ---------------------------------------------------------------------------
// Staff onboarding (plans / tasks / templates)
// ---------------------------------------------------------------------------

export type OnboardingStatus = "draft" | "active" | "completed" | "archived";
export type OnboardingPhase =
  | "pre_start"
  | "week_1"
  | "day_30"
  | "day_60"
  | "day_90";
export type OnboardingTaskStatus = "pending" | "in_progress" | "done" | "skipped";

export interface OnboardingTask {
  id: number | null;
  plan_id: number;
  phase: OnboardingPhase;
  title: string;
  category: string;
  owner_person_id: number | null;
  due_date: string | null;
  status: OnboardingTaskStatus;
  completed_at: string | null;
  completed_by_person_id: number | null;
  notes: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface OnboardingPlan {
  id: number | null;
  full_name: string;
  role: string;
  start_date: string;
  person_id: number | null;
  manager_person_id: number | null;
  buddy_person_id: number | null;
  template_name: string;
  status: OnboardingStatus;
  current_phase: OnboardingPhase;
  brief_artifact: string;
  reading_list: string[];
  ramp_segments: string[];
  ramp_next_index: number;
  engagement_id: number | null;
  candidate_id: number | null;
  completion_pct: number;
  archived: boolean;
  created_at: string;
  updated_at: string;
  tasks: OnboardingTask[];
}

export interface OnboardingTemplate {
  name: string;
  title: string;
  description: string;
  department: string;
  ramp_days: number;
  checkin_cadence: string;
  task_specs: Array<{
    title: string;
    category: string;
    phase: OnboardingPhase;
    owner_role: string;
    due_offset_days: number;
  }>;
  brief_sections: string[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface OnboardingPlanCreate {
  full_name: string;
  start_date: string;
  role?: string;
  template_name?: string;
  person_id?: number | null;
  manager_person_id?: number | null;
  buddy_person_id?: number | null;
}

export async function listOnboardingPlans(
  includeArchived = false,
): Promise<OnboardingPlan[]> {
  const res = await fetch(
    `${API_BASE}/onboarding-plans?include_archived=${includeArchived}`,
  );
  if (!res.ok) throw new Error(`Failed to load onboarding plans: ${res.statusText}`);
  return res.json();
}

export async function getOnboardingPlan(id: number): Promise<OnboardingPlan> {
  const res = await fetch(`${API_BASE}/onboarding-plans/${id}`);
  if (!res.ok) throw new Error(`Failed to load onboarding plan: ${res.statusText}`);
  return res.json();
}

export async function createOnboardingPlan(
  body: OnboardingPlanCreate,
): Promise<OnboardingPlan> {
  const res = await fetch(`${API_BASE}/onboarding-plans`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to create onboarding plan: ${res.statusText}`);
  return res.json();
}

export async function setOnboardingTaskStatus(
  taskId: number,
  status: OnboardingTaskStatus,
): Promise<OnboardingTask> {
  const res = await fetch(`${API_BASE}/onboarding-tasks/${taskId}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new Error(`Failed to update task: ${res.statusText}`);
  return res.json();
}

export async function advanceOnboardingPlan(id: number): Promise<OnboardingPlan> {
  const res = await fetch(`${API_BASE}/onboarding-plans/${id}/advance`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`Failed to advance plan: ${res.statusText}`);
  return res.json();
}

export async function activateOnboardingPlan(id: number): Promise<OnboardingPlan> {
  const res = await fetch(`${API_BASE}/onboarding-plans/${id}/activate`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`Failed to activate plan: ${res.statusText}`);
  return res.json();
}

export async function archiveOnboardingPlan(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/onboarding-plans/${id}/archive`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`Failed to archive plan: ${res.statusText}`);
}

export async function listOnboardingTemplates(): Promise<OnboardingTemplate[]> {
  const res = await fetch(`${API_BASE}/onboarding-templates`);
  if (!res.ok) throw new Error(`Failed to load onboarding templates: ${res.statusText}`);
  return res.json();
}

// Engagement intake: grounded AI draft of a real client from intake notes.

export interface ClientDraftBundle {
  profile: { name: string; industry?: string; stage?: string; [k: string]: unknown };
  people: { full_name: string; role?: string; is_principal?: boolean }[];
  departments: { slug: string; title: string }[];
  memory?: {
    decisions?: unknown[];
    initiatives?: unknown[];
    advice_given?: unknown[];
    alerts?: unknown[];
  };
  docs: { filename: string; content: string }[];
}

export interface ClientDraftResult {
  suggested_name: string;
  display_name: string;
  bundle: ClientDraftBundle;
}

export async function generateClientDraft(
  description: string,
  files: File[] = [],
): Promise<ClientDraftResult> {
  // multipart so intake material can include attachments (PDF/Word/Excel/CSV/
  // text). No Content-Type header — the browser sets the multipart boundary.
  const form = new FormData();
  form.append("description", description);
  for (const file of files) form.append("files", file, file.name);

  const res = await fetch(`${API_BASE}/clients/generate`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to generate client draft");
  }
  return res.json() as Promise<ClientDraftResult>;
}

export async function createClientFromDraft(
  displayName: string,
  bundle: ClientDraftBundle,
  intakeDescription: string,
): Promise<{ slug: string; display_name: string; active: boolean }> {
  const res = await fetch(`${API_BASE}/clients`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: displayName,
      source: "generated",
      bundle,
      intake_description: intakeDescription,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to create client from draft");
  }
  return res.json();
}


// ── Practice layer: engagement metadata + cross-client cockpit ─────────────

// Mirrors clients.cockpit.ClientCockpitCard.
export interface ClientCockpitCard {
  slug: string;
  display_name: string;
  is_active: boolean;
  role?: string | null;
  status?: string | null;
  renewal_date?: string | null;
  days_to_renewal?: number | null;
  primary_contact?: string | null;
  pending_actions?: number | null;
  overdue_actions?: number | null;
  awaiting_replies?: number | null;
  unread_alerts?: number | null;
  onboarding_due_soon?: number | null;
  saved_at?: string | null;
  has_state: boolean;
  error: boolean;
}

export async function getClientsCockpit(): Promise<{
  clients: ClientCockpitCard[];
  generated_at: string;
}> {
  const res = await fetch(`${API_BASE}/clients/cockpit`);
  if (!res.ok) throw new Error("Failed to load practice cockpit");
  return res.json();
}

export interface ClientMetaPatch {
  role?: string;
  status?: string;
  engagement_start?: string;
  renewal_date?: string;
  retainer?: string;
  hours_per_week?: number;
  primary_contact?: string;
  notes?: string;
}

export async function updateClientMeta(
  slug: string,
  patch: ClientMetaPatch,
): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_BASE}/clients/${encodeURIComponent(slug)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to update client");
  }
  return res.json();
}
