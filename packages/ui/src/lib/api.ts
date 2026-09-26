import type { AnswerSources } from "@/lib/answerSources";
import type { SetupStatus } from "@/lib/setupStatus";

const API_BASE = "/api/backend";

export type CommitteePhase = "drafting" | "reviewing" | "finalizing";

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

// Names the round of tool calls currently in flight, so the progress line can
// say what is actually happening. One event per tool-use iteration, emitted
// immediately before `thinking`. Ephemeral — unlike ActionTaken it is never
// persisted with the assistant message. Real specialist fan-out is
// deliberately generic ("Consulting specialists…"); individual specialist
// names are never surfaced. See backend `orchestrator/activity_labels.py`.
// Shown when a tool round is in flight but no `activity` event named it —
// an older backend, or the agent loop's defensive fallback when the labeller
// itself failed. Deliberately worded the same as the backend's own
// `FALLBACK_LABEL` in orchestrator/activity_labels.py: both mean "something is
// running that we can't name". Nothing enforces that across the language
// boundary, so change the two together.
export const FALLBACK_ACTIVITY_LABEL = "Working…";

export interface Activity {
  type: "activity";
  // Present-progressive and already ends in an ellipsis — render verbatim.
  label: string;
  // Canonical tool behind the label; for MCP this is the underlying tool name.
  tool: string;
  iteration?: number;
}

export interface ChatChunk {
  type:
    | "chunk"
    | "done"
    | "error"
    | "thinking"
    | "phase"
    | "committee_critique"
    // The user pressed Stop. Always followed by `done` over the same still-open
    // stream, so the consumer's state machine still reaches a clean end.
    | "stopped";
  content?: string;
  session_id?: string;
  // On `done`: row id of the persisted assistant reply, used to attach 👍/👎.
  message_id?: number;
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

// Sent once after the reply: what it looked at, and which part of the
// analysis it had to leave out (see lib/answerSources.ts).
export interface SourcesEvent extends AnswerSources {
  type: "sources";
  session_id?: string;
}

export type StreamItem =
  | ChatChunk
  | DebugEvent
  | ActionTaken
  | FormPatch
  | Activity
  | SourcesEvent;

export interface StreamChatOptions {
  committeeReview?: boolean;
  // Files to attach to this turn. When non-empty, the request switches to
  // the multipart /chat/upload route; documents are auto-indexed into
  // Company Resources and images are forwarded as vision blocks.
  files?: File[];
  // Ask OE panel only — what page/form the user is looking at. Ignored on
  // the multipart route (the panel doesn't support attachments).
  pageContext?: PageContext;
  // Client-minted id for this turn, so it can be addressed by `stopChat`.
  // Omit it and the turn simply isn't stoppable.
  clientTurnId?: string;
  // What peer memory records as the user's words for this turn, when
  // `message` carries text they did not write (a briefing handoff seeds the
  // Executive's own card body). JSON route only; the multipart route derives
  // its own from the typed text and filenames.
  memoryText?: string;
  // Safety net only. The normal stop path leaves the stream open and lets the
  // server wind down and send `stopped` + `done`; this aborts the fetch
  // outright if that never arrives.
  signal?: AbortSignal;
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
    if (opts?.clientTurnId) form.append("client_turn_id", opts.clientTurnId);
    for (const file of files) form.append("files", file, file.name);
    response = await fetch(`${API_BASE}/chat/upload`, {
      method: "POST",
      body: form,
      signal: opts?.signal,
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
        client_turn_id: opts?.clientTurnId ?? undefined,
        memory_text: opts?.memoryText ?? undefined,
      }),
      signal: opts?.signal,
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

  try {
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
  } finally {
    // `break`-ing out of the caller's `for await` closes this generator but
    // would otherwise leave the HTTP body open.
    await reader.cancel().catch(() => {});
  }
}

/** Ask the backend to stop an in-flight turn.
 *
 * Best-effort and never throws: a 404 just means the turn already ended, and a
 * network failure is covered by the caller's abort fallback. The SSE stream
 * stays the authority on how the turn actually finished — this only flips the
 * server-side switch. Returns whether the server accepted the stop.
 */
export async function stopChat(clientTurnId: string): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/chat/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_turn_id: clientTurnId }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export interface OnboardStatus {
  session_id: string;
  // The step's place among the steps this wizard asks (a solo workspace
  // skips the team steps), not an index into the full step list.
  current_step: number;
  total_steps: number;
  current_question: string | null;
  progress_percent: number;
  completed: boolean;
  // Whether the current step can be skipped. Absent from older backends.
  optional?: boolean;
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

// ── conversational onboarding (/onboard/interview/*) ─────────────────────────
// The step-by-step wizard above stays as a fallback; this is the default flow.

export interface OnboardPersonDraft {
  full_name: string;
  role: string;
  is_principal: boolean;
}

export interface OnboardDepartmentDraft {
  title: string;
  mission: string;
  head_person_name: string;
  authority_level: string;
}

export interface OnboardTurn {
  session_id: string;
  phase: "question" | "draft";
  questions_asked: number;
  max_questions: number;
  question: string | null;
  question_hint: string | null;
  draft: CompanyProfile | null;
  draft_people: OnboardPersonDraft[];
  draft_departments: OnboardDepartmentDraft[];
  confidence_notes: string[];
  summary: string | null;
}

export interface OnboardTranscriptTurn {
  role: "user" | "assistant";
  text: string;
}

export interface OnboardSession extends OnboardTurn {
  turns: OnboardTranscriptTurn[];
  saved: boolean;
}

/** Pulls the backend's `detail` when there is one, so the user sees the real
 * reason (file too large, message too long) rather than a generic failure. */
async function onboardError(res: Response, fallback: string): Promise<Error> {
  const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
  return new Error(typeof body.detail === "string" ? body.detail : fallback);
}

export async function startOnboardInterview(
  description: string,
  files: File[] = []
): Promise<OnboardTurn> {
  const form = new FormData();
  form.append("description", description);
  for (const file of files) form.append("files", file);
  const res = await fetch(`${API_BASE}/onboard/interview/start`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw await onboardError(res, "Could not start setup");
  return res.json();
}

export async function sendOnboardMessage(
  sessionId: string,
  message: string
): Promise<OnboardTurn> {
  const res = await fetch(`${API_BASE}/onboard/interview/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!res.ok) throw await onboardError(res, "Could not send that message");
  return res.json();
}

export async function forceOnboardDraft(sessionId: string): Promise<OnboardTurn> {
  const res = await fetch(`${API_BASE}/onboard/interview/draft`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!res.ok) throw await onboardError(res, "Could not draft your profile");
  return res.json();
}

export async function getOnboardInterview(sessionId: string): Promise<OnboardSession> {
  const res = await fetch(`${API_BASE}/onboard/interview/${sessionId}`);
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

export async function commitOnboardDraft(
  sessionId: string,
  profile: CompanyProfile,
  people: OnboardPersonDraft[],
  departments: OnboardDepartmentDraft[],
  /** The principal's sign-in email, confirmed on the review screen. Blank = don't set one. */
  ownerEmail = ""
): Promise<CompanyProfile> {
  const res = await fetch(`${API_BASE}/onboard/interview/commit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: sessionId,
      profile,
      people,
      departments,
      owner_email: ownerEmail.trim() || null,
    }),
  });
  if (!res.ok) throw await onboardError(res, "Could not save your profile");
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
  /** External dependencies the research watch policy treats as company data. */
  vendors: string[];
  tickers: string[];
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

// Mirrors SKILL_CATEGORIES in packages/core/openexecutive/knowledge/skills.py.
export const SKILL_CATEGORIES = [
  "strategy",
  "finance",
  "hr",
  "legal",
  "operations",
  "marketing",
  "product",
  "board",
  "sales",
  "general",
] as const;

export interface SkillMeta {
  name: string;
  category: string;
  description: string;
  when_to_use: string;
  source: "builtin" | "company";
  filename: string;
  /** A company skill that replaces a built-in of the same name. */
  customized: boolean;
  /** A built-in hidden for this company (only listed with includeHidden). */
  hidden: boolean;
  /** Workflows whose steps follow this playbook (switched-off custom ones too). */
  used_by: { name: string; title: string; is_custom: boolean }[];
}

export interface SkillDetail extends SkillMeta {
  body: string;
}

export interface SkillInput {
  name: string;
  category: string;
  description: string;
  when_to_use: string;
  body: string;
}

/** What DELETE did: removed, reverted a customization, or hid a built-in. */
export type SkillDeleteOutcome = "deleted" | "reverted" | "hidden";

export interface SkillSearchHit {
  name: string;
  category: string;
  description: string;
  when_to_use: string;
  source: "builtin" | "company";
  score: number;
}

async function skillError(res: Response, fallback: string): Promise<Error> {
  const err = await res.json().catch(() => ({}));
  const detail = (err as { detail?: unknown }).detail;
  return new Error(typeof detail === "string" ? detail : fallback);
}

export async function listSkills(includeHidden = false): Promise<SkillMeta[]> {
  const qs = includeHidden ? "?include_hidden=true" : "";
  const res = await fetch(`${API_BASE}/skills${qs}`);
  if (!res.ok) throw new Error("Failed to list playbooks");
  const data = await res.json();
  return data.skills;
}

export async function getSkill(name: string): Promise<SkillDetail> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`);
  if (!res.ok) throw await skillError(res, "Failed to load playbook");
  return res.json();
}

export async function searchSkills(
  q: string,
  n = 5
): Promise<SkillSearchHit[]> {
  const params = new URLSearchParams({ q, n: String(n) });
  const res = await fetch(`${API_BASE}/skills/search?${params.toString()}`);
  if (!res.ok) throw new Error("Failed to search playbooks");
  const data = await res.json();
  return data.results;
}

export async function createSkill(input: SkillInput): Promise<SkillDetail> {
  const res = await fetch(`${API_BASE}/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await skillError(res, "Failed to create playbook");
  return res.json();
}

/** Full replace. On a built-in this saves a customized copy. */
export async function updateSkill(input: SkillInput): Promise<SkillDetail> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(input.name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await skillError(res, "Failed to save playbook");
  return res.json();
}

export async function deleteSkill(name: string): Promise<SkillDeleteOutcome> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw await skillError(res, "Failed to delete playbook");
  const data = (await res.json()) as { outcome: SkillDeleteOutcome };
  return data.outcome;
}

export async function restoreSkill(name: string): Promise<SkillDetail> {
  const res = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(name)}/restore`,
    { method: "POST" }
  );
  if (!res.ok) throw await skillError(res, "Failed to restore playbook");
  return res.json();
}

/** A playbook change the Executive proposed from chat, awaiting review. */
export interface SkillDraft {
  action: "create" | "update" | "delete";
  name: string;
  category: string;
  description: string;
  when_to_use: string;
  body: string;
  proposed_at: string;
  /** Version token: approve/discard act only on this exact draft. */
  id: string;
  /** The playbook in effect now (null for a create). */
  current: SkillDetail | null;
  /** Workflows that follow this name (for a create too). */
  followers: { name: string; title: string; is_custom: boolean }[];
}

export async function listSkillDrafts(): Promise<SkillDraft[]> {
  const res = await fetch(`${API_BASE}/skill-drafts`);
  if (!res.ok) throw new Error("Failed to list playbook drafts");
  const data = await res.json();
  return data.drafts;
}

export async function getSkillDraft(name: string): Promise<SkillDraft> {
  const res = await fetch(`${API_BASE}/skill-drafts/${encodeURIComponent(name)}`);
  if (!res.ok) throw await skillError(res, "Failed to load draft");
  return res.json();
}

/** Approve the reviewed version (`id`); 409 if the draft changed since. */
export async function approveSkillDraft(
  name: string,
  id: string
): Promise<{ action: SkillDraft["action"]; skill: SkillDetail | null }> {
  const res = await fetch(
    `${API_BASE}/skill-drafts/${encodeURIComponent(name)}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }
  );
  if (!res.ok) throw await skillError(res, "Failed to approve draft");
  return res.json();
}

/** Discard the reviewed version (`id`); resolves quietly if it's already gone. */
export async function discardSkillDraft(name: string, id: string): Promise<void> {
  const qs = new URLSearchParams({ id }).toString();
  const res = await fetch(`${API_BASE}/skill-drafts/${encodeURIComponent(name)}?${qs}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 404) throw await skillError(res, "Failed to discard draft");
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

// One suggested next message for the chat composer, grounded in the tail of
// the session. Resolves to null on any non-OK response — the composer then
// keeps its static placeholder — but rejects on abort so callers can tell a
// cancelled fetch apart from "no suggestion".
export async function getFollowupSuggestion(
  sessionId: string,
  signal?: AbortSignal,
): Promise<string | null> {
  const res = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/followup`,
    { signal },
  );
  if (!res.ok) return null;
  const body: { suggestion?: unknown } = await res.json();
  return typeof body.suggestion === "string" && body.suggestion.trim()
    ? body.suggestion.trim()
    : null;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  // Inline action chips for assistant messages — populated from
  // `action_taken` SSE events during the live turn, and persisted to
  // `chat_messages.action_chips` so reopening a saved session restores
  // them (the backend re-attaches them here via load_messages).
  actions?: ActionTaken[];
  // True when the user stopped this reply mid-stream. Persisted to
  // `chat_messages.stopped`, so the marker survives a reload rather than
  // letting a truncated reply read as a complete one.
  stopped?: boolean;
  // Assistant rows only: what the reply looked at and which part of the
  // analysis it left out — from the stream's `sources` event, and persisted
  // to `chat_messages.sources` so a reloaded chat still shows it.
  sources?: AnswerSources;
  // Assistant rows only: the persisted row id (from the stream's `done`
  // event, or from a reloaded session) and any 👍/👎 on it. The id is what
  // lets the reply be rated.
  id?: number;
  feedback?: "up" | "down" | null;
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

// Peer memory — what the Executive has learned about each person, derived
// server-side and read-only here (no edit/delete: it is not the Executive's
// own record the way decisions and advice are).
export interface PersonConclusion {
  content: string;
  created_at: string;
}

export interface PersonMemory {
  person_id: number;
  full_name: string;
  is_principal: boolean;
  card: string[];
  conclusion_count: number;
  last_observed_at: string | null;
  recent: PersonConclusion[];
  error: string | null;
}

export interface PeopleMemory {
  status: "ok" | "disabled" | "error";
  people: PersonMemory[];
  conclusion_total: number;
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

// The Pulse header and the People tab both mount on page load and both need
// this; behind it are one Honcho listing plus two reads per person, so the
// two mounts share one in-flight request instead of doubling that fan-out.
const PEOPLE_MEMORY_SHARE_MS = 5_000;
// `settledAt` is null while the request is still running: a pending request is
// always shared, however long it has run (the backend bounds it, not us), and a
// resolved one for five seconds after it settled.
let peopleMemoryShared: {
  recent: number;
  promise: Promise<PeopleMemory>;
  settledAt: number | null;
} | null = null;

export function listPeopleMemory(recent = 5): Promise<PeopleMemory> {
  const shared = peopleMemoryShared;
  if (
    shared &&
    shared.recent === recent &&
    (shared.settledAt === null || Date.now() - shared.settledAt < PEOPLE_MEMORY_SHARE_MS)
  ) {
    return shared.promise;
  }
  const promise = (async () => {
    const res = await fetch(`${API_BASE}/memories/people?recent=${recent}`);
    if (!res.ok) throw new Error("Failed to list people memory");
    return (await res.json()) as PeopleMemory;
  })();
  const entry = { recent, promise, settledAt: null as number | null };
  peopleMemoryShared = entry;
  promise.then(
    () => {
      entry.settledAt = Date.now();
    },
    () => {
      // A failure must not be served to the next caller.
      if (peopleMemoryShared === entry) peopleMemoryShared = null;
    },
  );
  return promise;
}

export interface PersonConclusionsPage {
  status: "ok" | "disabled" | "error";
  person_id: number;
  items: PersonConclusion[];
  page: number;
  size: number;
  total: number | null;
  has_more: boolean;
}

// Every conclusion about one person, newest first, one page at a time — the
// People tab's "show all" pane. The overview above carries only the newest few.
export async function listPersonConclusions(
  personId: number,
  page: number,
  size = 50,
): Promise<PersonConclusionsPage> {
  const res = await fetch(
    `${API_BASE}/memories/people/${personId}/conclusions?page=${page}&size=${size}`,
  );
  if (!res.ok) throw new Error("Failed to list person conclusions");
  return res.json();
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
    // Signed-in users can cancel once the API and the UI share
    // BACKEND_SHARED_SECRET (every internet-reachable deploy sets it).
    throw new Error(
      "Cancel isn't enabled on this server yet. Ask your administrator to set the same BACKEND_SHARED_SECRET on the API and the UI.",
    );
  }
  if (!res.ok) throw new Error("Failed to cancel scheduled action");
  return res.json();
}

// ----------------------------------------------------------------------------
// Executive pause switch — holds all autonomous work (scheduler, inbox,
// workflow timers); chat and direct messages keep working.
// ----------------------------------------------------------------------------

export interface ExecutiveStatus {
  paused: boolean;
  paused_at: string | null;
  paused_by: string | null;
  reason: string | null;
  // Pending scheduled actions already due — they fire on resume.
  held_actions: number;
  // Whether the signed-in viewer may resume (principal-only once one exists).
  can_resume: boolean;
}

export async function getExecutiveStatus(signal?: AbortSignal): Promise<ExecutiveStatus> {
  const res = await fetch(`${API_BASE}/executive/status`, { signal });
  if (!res.ok) throw new Error("Failed to load executive status");
  return res.json();
}

// Settings → Setup status. The API tests each part of the install live, so
// this can take a few seconds; see api/setup_checks.py.
export async function getSetupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  const res = await fetch(`${API_BASE}/setup/status`, { signal, cache: "no-store" });
  if (!res.ok) throw new Error(`Setup status request failed (HTTP ${res.status})`);
  return res.json();
}

export async function pauseExecutive(reason?: string): Promise<ExecutiveStatus> {
  const res = await fetch(`${API_BASE}/executive/pause`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason?.trim() || null }),
  });
  if (!res.ok) throw new Error("Failed to pause the Executive");
  return res.json();
}

export async function resumeExecutive(): Promise<ExecutiveStatus> {
  const res = await fetch(`${API_BASE}/executive/resume`, { method: "POST" });
  if (res.status === 403) throw new Error("Only the principal can resume the Executive.");
  if (!res.ok) throw new Error("Failed to resume the Executive");
  return res.json();
}

// ----------------------------------------------------------------------------
// Workspace settings — solo / team mode, the user's time zone, and the
// principal's role.
// ----------------------------------------------------------------------------

// "solo": one person using Open Executive just for themselves (no department
// check-ins). "team": a company with departments and people (the default).
export type WorkspaceMode = "solo" | "team";

// How the principal relates to the organisation in their profile: they own
// it, they work inside one they don't own, they serve clients independently,
// or something else.
export type RoleKind = "owner" | "in_house" | "independent" | "other";

// What the principal does. Solo mode tells the Executive and its specialists;
// every field is null until set.
export interface PrincipalRole {
  role_kind: RoleKind | null;
  role_title: string | null;
  reports_to: string | null;
  remit: string | null;
  measured_on: string | null;
}

export interface WorkspaceSettings extends PrincipalRole {
  mode: WorkspaceMode;
  // IANA zone the user set, or null when none is set.
  timezone: string | null;
  // The zone in effect: `timezone`, else the server's USER_TIMEZONE, else UTC.
  effective_timezone: string;
}

// Partial update: only the fields present change. `timezone: null` (or "")
// clears the stored zone; the role fields clear the same way.
export interface WorkspaceUpdate extends Partial<PrincipalRole> {
  mode?: WorkspaceMode;
  timezone?: string | null;
}

export async function getWorkspace(signal?: AbortSignal): Promise<WorkspaceSettings> {
  const res = await fetch(`${API_BASE}/workspace`, { signal });
  if (!res.ok) throw new Error("Failed to load workspace settings");
  return res.json();
}

export async function updateWorkspace(update: WorkspaceUpdate): Promise<WorkspaceSettings> {
  const res = await fetch(`${API_BASE}/workspace`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  if (res.status === 403) throw new Error("Only the principal can change workspace settings.");
  if (res.status === 422) {
    // FastAPI validation errors: `detail` is a list of {msg}; surface the
    // first one ("timezone 'Mars/Base' is not a known IANA zone",
    // "remit must be at most 500 characters").
    const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
    const first = Array.isArray(body.detail) ? (body.detail[0] as { msg?: unknown }) : undefined;
    const msg = typeof first?.msg === "string" ? first.msg.replace(/^Value error, /, "") : null;
    throw new Error(msg ?? "Invalid workspace settings");
  }
  if (!res.ok) throw new Error("Failed to update workspace settings");
  return res.json();
}

// ----------------------------------------------------------------------------
// Act as me — the Executive drafts email AS you, in your own Gmail Drafts,
// when you ask it to. It never sends. Only the owner can have it for now.
// ----------------------------------------------------------------------------
export type DelegationGmailStatus =
  | "connected"
  | "not_configured"
  | "needs_reconnect"
  | "mismatch"
  | "no_email"
  | "shared_mailbox"
  | "error";

export interface DelegationSettings {
  enabled: boolean;
  gmail: {
    status: DelegationGmailStatus;
    message: string;
    email: string | null;
    // How to connect your own Gmail (the token is minted locally).
    connect_command: string;
  };
}

// "How I write": learned from your own sent mail; you can edit and lock it.
export interface VoiceProfile {
  greetings: Record<string, string>;
  sign_off: string;
  signature: string;
  length: string;
  formality: string;
  habits: string[];
  avoid: string[];
  exemplars: string[];
  locked: boolean;
  learned_at: string | null;
  sample_count: number;
  updated_at: string | null;
}

export interface VoiceUpdate {
  greetings?: Record<string, string>;
  sign_off?: string;
  length?: string;
  formality?: string;
  habits?: string[];
  avoid?: string[];
  locked?: boolean;
  clear_exemplars?: boolean;
  clear_signature?: boolean;
}

// The route's errors carry `detail: {code, message}` (or FastAPI's list).
async function delegationError(res: Response, fallback: string): Promise<Error> {
  const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
  const detail = body.detail;
  if (typeof detail === "string") return new Error(detail);
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return new Error(message);
  }
  return new Error(fallback);
}

// null when this viewer can't have it (403) or the backend predates it (404).
export async function getDelegation(signal?: AbortSignal): Promise<DelegationSettings | null> {
  const res = await fetch(`${API_BASE}/delegation`, { signal });
  if (res.status === 403 || res.status === 404) return null;
  if (!res.ok) throw await delegationError(res, "Couldn't load Act as me.");
  return res.json();
}

export async function setDelegationEnabled(enabled: boolean): Promise<DelegationSettings> {
  const res = await fetch(`${API_BASE}/delegation`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw await delegationError(res, "Couldn't change Act as me.");
  return res.json();
}

export async function getVoiceProfile(signal?: AbortSignal): Promise<VoiceProfile> {
  const res = await fetch(`${API_BASE}/delegation/voice`, { signal });
  if (!res.ok) throw await delegationError(res, "Couldn't load how you write.");
  return res.json();
}

export async function learnVoiceProfile(): Promise<VoiceProfile> {
  const res = await fetch(`${API_BASE}/delegation/voice/learn`, { method: "POST" });
  if (!res.ok) throw await delegationError(res, "Couldn't learn how you write.");
  return res.json();
}

export async function updateVoiceProfile(update: VoiceUpdate): Promise<VoiceProfile> {
  const res = await fetch(`${API_BASE}/delegation/voice`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  if (!res.ok) throw await delegationError(res, "Couldn't save how you write.");
  return res.json();
}

// Takes the signature from your Gmail settings again; nothing else changes.
export async function refreshVoiceSignature(): Promise<VoiceProfile> {
  const res = await fetch(`${API_BASE}/delegation/voice/signature`, { method: "POST" });
  if (!res.ok) throw await delegationError(res, "Couldn't read your Gmail signature.");
  return res.json();
}

export async function resetVoiceProfile(): Promise<VoiceProfile> {
  const res = await fetch(`${API_BASE}/delegation/voice`, { method: "DELETE" });
  if (!res.ok) throw await delegationError(res, "Couldn't reset how you write.");
  return res.json();
}

// ----------------------------------------------------------------------------
// Decision classes — whether the Executive acts on a class of decision on its
// own ("auto_execute") or proposes it for approval first ("propose").
// ----------------------------------------------------------------------------

export type DecisionClassMode = "propose" | "auto_execute";

export interface DecisionClassSetting {
  decision_class: string;
  mode: DecisionClassMode;
}

// The class behind "Book meetings without asking".
export const MEETING_SCHEDULING_CLASS = "meeting_scheduling";

/** The class's mode, or null when this backend has no setting for it (404). */
export async function getDecisionClassMode(
  decisionClass: string,
  signal?: AbortSignal,
): Promise<DecisionClassSetting | null> {
  const res = await fetch(
    `${API_BASE}/decisions/classes/${encodeURIComponent(decisionClass)}`,
    { signal },
  );
  if (res.status === 404) return null;
  if (!res.ok) throw new Error("Failed to load the setting");
  return res.json();
}

export async function setDecisionClassMode(
  decisionClass: string,
  mode: DecisionClassMode,
): Promise<DecisionClassSetting> {
  const res = await fetch(`${API_BASE}/decisions/classes/${encodeURIComponent(decisionClass)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (res.status === 403) throw new Error("Only the principal can change this setting.");
  // 409: auto-booking can't be turned on before a principal exists; the
  // backend's detail says so in plain words.
  if (res.status === 409) throw await onboardError(res, "This setting can't be turned on yet.");
  if (!res.ok) throw new Error("Failed to save the setting");
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
  // Run by the system itself (scheduler, onboarding, reflection); the catalog
  // files these under "System".
  background?: boolean;
  /** Playbooks (skills) this workflow's steps follow. */
  playbooks?: string[];
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
      /** Name of a playbook (skill) the step follows. */
      playbook?: string;
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
    }
  | {
      // Gets something done with tools. `tools` is the exact allowlist the
      // user approves when they create the workflow (or turn it on).
      kind: "action";
      id: string;
      title: string;
      description?: string;
      goal: string;
      tools: string[];
      max_tool_calls?: number;
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
  // Who created it (server-managed; echoing it back in a body has no effect).
  // Asked to approve the workflow's first write to a new target.
  owner_person_id?: number | null;
}

// The specialists a dynamic step may consult (matches SPECIALIST_REGISTRY,
// excluding the internal `triage` router).
export const DYNAMIC_SPECIALISTS = [
  "cso",
  "cfo",
  "chro",
  "gc",
  "coo",
  "cmo",
  "cpo",
  "sales",
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

/** An error from the custom-workflow endpoints, carrying the HTTP status. */
export class CustomWorkflowError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

/** The server's error detail; a 422's validation-error array is joined. */
async function _customError(res: Response): Promise<CustomWorkflowError> {
  let detail: unknown = res.statusText;
  try {
    detail = (await res.json()).detail;
  } catch {
    /* keep statusText */
  }
  const msg = Array.isArray(detail) ? detail.join("; ") : String(detail);
  return new CustomWorkflowError(msg, res.status);
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
  if (!res.ok) throw await _customError(res);
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

/**
 * Turn a custom workflow on — the approval for one chat saved switched off.
 * `reviewed` is the definition the user was shown; the server refuses (409)
 * if the stored one has changed since, so only what was seen gets switched on.
 */
export async function activateCustomWorkflow(
  reviewed: DynamicWorkflowDef
): Promise<DynamicWorkflowDef> {
  const res = await fetch(
    `${API_BASE}/workflows/custom/${encodeURIComponent(reviewed.name)}/activate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: true, definition: reviewed }),
    }
  );
  if (!res.ok) throw await _customError(res);
  return res.json();
}

/** A target a workflow's tool steps may write to without asking again. */
export interface ApprovedTarget {
  value: string;
  key: string;
  approved_at: string;
  run_id: string;
}

export async function listApprovedTargets(name: string): Promise<ApprovedTarget[]> {
  const res = await fetch(`${API_BASE}/workflows/custom/${encodeURIComponent(name)}/targets`);
  if (!res.ok) throw await _customError(res);
  return (await res.json()).targets;
}

export async function forgetApprovedTarget(name: string, value: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/workflows/custom/${encodeURIComponent(name)}/targets?value=${encodeURIComponent(value)}`,
    { method: "DELETE" }
  );
  if (!res.ok) throw await _customError(res);
}

export async function deleteCustomWorkflow(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/workflows/custom/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error("Failed to delete custom workflow");
}

// ---- Tools a workflow action step can use ----

export interface WorkflowToolInfo {
  name: string;
  description: string;
  // true = only reads; null = may change something (can't tell from the name)
  read_only: boolean | null;
  source: "mcp" | "builtin";
}

export async function searchWorkflowTools(q: string): Promise<WorkflowToolInfo[]> {
  const res = await fetch(
    `${API_BASE}/workflows/tools/search?q=${encodeURIComponent(q)}`
  );
  if (!res.ok) throw new Error("Tool search failed");
  return (await res.json()).tools;
}

export async function describeWorkflowTools(
  names: string[]
): Promise<WorkflowToolInfo[]> {
  if (names.length === 0) return [];
  const res = await fetch(
    `${API_BASE}/workflows/tools/describe?names=${encodeURIComponent(names.join(","))}`
  );
  if (!res.ok) throw new Error("Tool lookup failed");
  return (await res.json()).tools;
}

// ---- Conversational workflow designer (/jobs/new wizard) ----

export interface WorkflowDesignerDraft {
  // Exactly the body POST /workflows/custom takes.
  definition: DynamicWorkflowDef;
  summary: string;
  assumptions: string[];
}

export interface WorkflowDesignerTurn {
  session_id: string;
  phase: "question" | "draft";
  questions_asked: number;
  max_questions: number;
  question: string | null;
  hint: string | null;
  options: string[];
  draft: WorkflowDesignerDraft | null;
  transcript: { role: "user" | "assistant"; text: string }[];
}

async function _designerPost(
  path: string,
  body: Record<string, string>,
  fallback: string
): Promise<WorkflowDesignerTurn> {
  const res = await fetch(`${API_BASE}/workflows/designer/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await onboardError(res, fallback);
  return res.json();
}

export function startWorkflowDesigner(message: string): Promise<WorkflowDesignerTurn> {
  return _designerPost("start", { message }, "Could not start the workflow assistant");
}

export function sendWorkflowDesignerMessage(
  sessionId: string,
  message: string
): Promise<WorkflowDesignerTurn> {
  return _designerPost(
    "message",
    { session_id: sessionId, message },
    "Could not send that message"
  );
}

export function forceWorkflowDesignerDraft(sessionId: string): Promise<WorkflowDesignerTurn> {
  return _designerPost("draft", { session_id: sessionId }, "Could not draft the workflow");
}

export async function getWorkflowDesignerSession(
  sessionId: string
): Promise<WorkflowDesignerTurn> {
  const res = await fetch(
    `${API_BASE}/workflows/designer/${encodeURIComponent(sessionId)}`
  );
  if (!res.ok) throw await onboardError(res, "That workflow draft has expired");
  return res.json();
}

export interface WorkflowSample {
  workflow: string;
  inputs: Record<string, unknown>;
}

/** Every status `workflow_runs.status` can hold. `awaiting_human`, `resolved`
 *  and `timed_out` have been served for a while; the union only ever listed
 *  three, so the others reached the UI as unhandled strings. */
export type WorkflowRunStatus =
  | "running"
  | "done"
  | "error"
  | "awaiting_human"
  | "resolved"
  | "timed_out";

/** Statuses the run will not move on from by itself. Anything else means a
 *  poll is worth repeating: `running` is working, `awaiting_human` is waiting
 *  on a person, and `resolved` is queued for the resumer to pick up. */
export const TERMINAL_RUN_STATUSES: ReadonlySet<WorkflowRunStatus> = new Set([
  "done",
  "error",
  "timed_out",
]);

/** How the gate's question actually reached the approver. Anything other than
 *  `sent` or `self` means nobody was asked, and the UI must not imply
 *  otherwise. */
export type GateDelivery = "self" | "sent" | "suppressed" | "alerted" | "failed";

export interface WorkflowRunSummary {
  run_id: string;
  workflow_name: string;
  title: string;
  status: WorkflowRunStatus;
  created_at: string;
  updated_at: string;
}

/** Which steps a paused run already finished. The server sends this in place
 *  of the raw resume payload, which carries every completed step's full text
 *  and would otherwise ride on every poll. */
export interface ResumeProgress {
  gate_step_id: string;
  completed_step_ids: string[];
}

export interface WorkflowRunDetail extends WorkflowRunSummary {
  inputs: Record<string, unknown>;
  artifact: string | null;
  error: string | null;
  // Checkpoint columns the run record has always carried; the detail route
  // returns the whole row, so these were already on the wire untyped.
  awaiting_person_id?: number | null;
  awaiting_until?: string | null;
  state_json?: string | null;
  resolution_json?: string | null;
  resume_progress?: ResumeProgress | null;
}

/** The serialized gate in `state_json`, for rendering what a paused run is
 *  waiting on. Every field is optional: older checkpoints predate some of
 *  them, which is exactly how the server tells legacy rows apart. */
export interface GateState {
  question?: string;
  person_id?: number;
  expected_reply_shape?: string;
  delivery?: GateDelivery;
  channel?: string;
}

export interface WorkflowEvent {
  type:
    | "run_created"
    | "step_start"
    | "step_done"
    // A running step reports activity (an action step using a tool).
    | "progress"
    | "result"
    | "artifact"
    | "done"
    | "error"
    | "awaiting_human";
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
  /** `result` events only. */
  data?: Record<string, unknown>;
  // `awaiting_human` only. A paused run emits NO `done` or `error` — this
  // frame is the last one, which `terminal` says explicitly so a client
  // doesn't sit waiting for an end that never comes.
  person_id?: number;
  question?: string;
  awaiting_until?: string;
  delivery?: GateDelivery;
  resumable?: boolean;
  terminal?: boolean;
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

/** Answer a run waiting on a yes/no sign-off (the person asked, or the principal). */
export async function decideWorkflowRun(
  runId: string,
  decision: "approve" | "reject"
): Promise<void> {
  const res = await fetch(`${API_BASE}/workflows/runs/${encodeURIComponent(runId)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
  if (!res.ok) throw await _customError(res);
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
  // Registered format — see packages/core/openexecutive/orchestrator/artifact_formats.py.
  format: ArtifactFormat;
  format_label: string;
  // Download targets; the first is the artifact's own file. Empty for links.
  downloads: ArtifactFormat[];
  external_url: string | null;
  link_label: string | null;
  supersedes_id: string | null; // id of the earlier version this revised
}

export type ArtifactFormat = "docx" | "html" | "link" | "markdown" | "xlsx";

export const ARTIFACT_EXTENSIONS: Record<ArtifactFormat, string | null> = {
  docx: "docx",
  html: "html",
  link: null,
  markdown: "md",
  xlsx: "xlsx",
};

export interface ArtifactDetail extends ArtifactSummary {
  // Sanitized HTML for "html"; Markdown for every other format.
  body: string;
  rationale: string | null;
}

// Same-origin proxy URL, so a plain <a href> download carries the session.
export function artifactDownloadUrl(id: string, as?: ArtifactFormat): string {
  const qs = as ? `?as=${encodeURIComponent(as)}` : "";
  return `${API_BASE}/artifacts/${encodeURIComponent(id)}/download${qs}`;
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
// `failure` = shipped or user-authored failure case studies. They have their
// own id namespace (`failure:<domain>:<file>`) so a user upload can never
// collide with a shipped one.
export type ReviewContentType = "builtin" | "external" | "failure";
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
  /** True only for content that ships with the product, not a user upload. */
  trusted_default: boolean;
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

// A bulk approve must always carry a selector — the backend rejects a call
// with none, so clearing the whole queue is an explicit `all_pending` opt-in
// rather than an empty body.
export async function bulkApproveReviewItems(
  domain?: string,
): Promise<{ approved_count: number; item_ids: string[] }> {
  const res = await fetch(`${API_BASE}/review/bulk-approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(domain ? { domain } : { all_pending: true }),
  });
  if (!res.ok) throw new Error("Failed to bulk approve");
  return res.json();
}

export async function curateDomain(
  domain: string,
  action: "start" | "stop",
): Promise<{ domain: string; action: "start" | "stop"; affected_count: number }> {
  const res = await fetch(`${API_BASE}/review/curate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domain, action }),
  });
  if (!res.ok) throw new Error("Failed to update curation");
  return res.json();
}

/** Domain → count of shipped docs nobody has reviewed yet. */
export async function getTrustedDefaults(): Promise<Record<string, number>> {
  const res = await fetch(`${API_BASE}/review/trusted-defaults`);
  if (!res.ok) throw new Error("Failed to load trusted defaults");
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
    let refusal: string | null = null;
    try {
      const body = await response.json();
      detail = JSON.stringify(body.detail ?? body);
      // A 403 ("Only the principal can run this workflow.") is a sentence
      // for the person, so show it as is rather than as a request failure.
      if (response.status === 403 && typeof body.detail === "string") {
        refusal = body.detail;
      }
    } catch {
      // body wasn't JSON
    }
    throw new Error(refusal ?? `Workflow request failed: ${detail}`);
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

// One allowlisted model, grouped for the Council's Provider → Model picker.
// Mirrors ModelOption in api/routes/agents.py. `route` says which backend
// actually serves the id (it mirrors providers.registry.get_provider).
export interface ModelOption {
  id: string;
  provider: string;
  provider_label: string;
  route: "direct" | "openrouter" | "local";
  label: string;
}

export async function listAgentModelOptions(agentId?: string): Promise<ModelOption[]> {
  const qs = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  const res = await fetch(`${API_BASE}/agents/models/options${qs}`);
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

// Cross-session token-usage aggregate used by /audit/usage. Totals plus by-day,
// by-model and by-source breakdowns, summed from cache_event rows. `cost_usd` is
// the actual OpenRouter charge captured per call (0 for rows that predate
// capture); `web_search_requests` counts server-side searches the calls made.

export interface UsageTotals {
  calls: number;
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  // Absent on rows written before searches were recorded.
  web_search_requests?: number;
  cost_usd: number;
}

export interface UsageByDay extends UsageTotals {
  day: string; // YYYY-MM-DD (UTC)
}

export interface UsageByModel extends UsageTotals {
  model: string;
}

// One row per call source (the audit `actor`): executive, specialist,
// specialist_workflow, specialist_research, research_synthesis,
// research_watchlist, triage, memory_extractor, agent_test, …
export interface UsageBySource extends UsageTotals {
  source: string;
}

export interface UsageSummary {
  since: string | null;
  until: string | null;
  totals: UsageTotals;
  by_day: UsageByDay[];
  by_model: UsageByModel[];
  // Absent on a backend older than the by-source breakdown.
  by_source?: UsageBySource[];
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
  // Named external entities this department wants monitored. The research
  // watch policy treats them as strong grounding: a proposal about one can
  // be added to the watch list on its own and is routed to this department.
  watched_entities: string[];
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
  watched_entities?: string[];
}

export async function updateDepartment(slug: string, patch: DepartmentPatch): Promise<DepartmentState> {
  const res = await fetch(`${API_BASE}/departments/${slug}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw await onboardError(res, `Failed to update department: ${res.statusText}`);
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

// Only `key_result` is required: the server fills a missing `period_value`
// with the current period for `period_type`, and `target` may be empty.
export interface GoalCreate {
  period_type?: PeriodType;
  period_value?: string;
  key_result: string;
  target?: string;
  current?: string;
  status?: Goal["status"];
}

// The server's reason when it gives one — a string `detail`, or the first
// FastAPI validation message — else `fallback`.
async function goalError(res: Response, fallback: string): Promise<Error> {
  const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
  if (typeof body.detail === "string") return new Error(body.detail);
  const first = Array.isArray(body.detail) ? (body.detail[0] as { msg?: unknown; loc?: unknown }) : undefined;
  if (typeof first?.msg === "string") {
    const loc = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : undefined;
    const field = typeof loc === "string" && loc !== "body" ? `${loc.replace(/_/g, " ")}: ` : "";
    return new Error(`${fallback} — ${field}${first.msg.replace(/^Value error, /, "")}`);
  }
  return new Error(`${fallback} (${res.status})`);
}

export async function createGoal(slug: string, body: GoalCreate): Promise<Goal> {
  const res = await fetch(`${API_BASE}/departments/${slug}/goals`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await goalError(res, "Couldn't add the goal");
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
  if (!res.ok) throw await goalError(res, "Couldn't save the goal");
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

// "team": works with the principal — can sign in, talk to the bot, approve and
// be chased. "contact": someone outside the team (a client, a contractor) the
// Executive emails or invites only when the principal asks it to directly.
export type PersonKind = "team" | "contact";

export interface Person {
  id: number;
  full_name: string;
  role: string;
  is_principal: boolean;
  kind: PersonKind;
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

// Team members only by default — the pickers (department head, workflow
// approver, …) must never offer a contact. The People page asks for both;
// the server honours that only for the principal (contacts are theirs alone).
export async function listPeople(opts: { includeContacts?: boolean } = {}): Promise<Person[]> {
  const query = opts.includeContacts ? "?include_contacts=true" : "";
  const res = await fetch(`${API_BASE}/people${query}`);
  if (!res.ok) throw new Error(`Failed to load people: ${res.statusText}`);
  return res.json();
}

// Who the signed-in viewer is on the roster. Contacts are private to the
// principal, so the People page offers them only when `is_principal`.
export interface PeopleViewer {
  person_id: number | null;
  is_principal: boolean;
}

export async function getPeopleViewer(): Promise<PeopleViewer> {
  const res = await fetch(`${API_BASE}/people/me`);
  if (!res.ok) throw new Error(`Failed to load viewer: ${res.statusText}`);
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
  kind?: PersonKind;
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

// Adding, editing and archiving people is the principal's alone (403 for anyone
// else): the People list is also who can sign in and who the Executive emails.
const PEOPLE_PRINCIPAL_ONLY = "Only the principal can change the People list.";

export async function createPerson(body: PersonCreate): Promise<Person> {
  const res = await fetch(`${API_BASE}/people`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 403) throw new Error(PEOPLE_PRINCIPAL_ONLY);
  if (!res.ok) throw new Error(`Failed to create person: ${res.statusText}`);
  return res.json();
}

export interface PersonPatch {
  full_name?: string;
  role?: string;
  kind?: PersonKind;
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
  if (res.status === 403) throw new Error(PEOPLE_PRINCIPAL_ONLY);
  if (!res.ok) throw new Error(`Failed to update person: ${res.statusText}`);
  return res.json();
}

export async function archivePerson(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/people/${id}/archive`, { method: "POST" });
  if (res.status === 403) throw new Error(PEOPLE_PRINCIPAL_ONLY);
  if (!res.ok) throw new Error(`Failed to archive person: ${res.statusText}`);
}

// Attunement — open loops: things a person committed to or was asked for in
// conversation and hasn't reported done. Overdue ones are chased by the
// nudge engine; closing one stops the chase.
export interface OpenLoop {
  loop_id: number;
  owner_person_id: number;
  owner_name: string;
  description: string;
  due_at: string;
  created_at: string;
}

export async function getPersonOpenLoops(id: number): Promise<OpenLoop[]> {
  const res = await fetch(`${API_BASE}/people/${id}/open-loops`);
  if (!res.ok) throw new Error(`Failed to load open loops: ${res.statusText}`);
  return res.json();
}

export async function closeOpenLoop(
  loopId: number,
  reason: "done" | "not_needed" | "cancelled" = "done",
): Promise<void> {
  const res = await fetch(`${API_BASE}/open-loops/${loopId}/close`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!res.ok) throw new Error(`Failed to close open loop: ${res.statusText}`);
}

// Attunement outcome ledger: how a person responded to proactive outreach
// over the last 30 days, per kind of outreach.
export interface OutreachStat {
  source: string;
  label: string;
  sent: number;
  replied: number;
  acted: number;
  ignored: number;
  pending: number;
}

export async function getPersonOutreach(id: number): Promise<OutreachStat[]> {
  const res = await fetch(`${API_BASE}/people/${id}/outreach`);
  if (!res.ok) throw new Error(`Failed to load outreach: ${res.statusText}`);
  return res.json();
}

// Attunement working style: a few short "how they like replies" rules pinned
// into this person's own turns. Learned from their own 👍/👎 and requests;
// editable, lockable (a locked profile is never re-learned) and resettable.
export interface WorkingStyle {
  rules: { text: string; basis: string }[];
  locked: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

export async function getPersonWorkingStyle(id: number): Promise<WorkingStyle> {
  const res = await fetch(`${API_BASE}/people/${id}/attunement`);
  if (!res.ok) throw new Error(`Failed to load working style: ${res.statusText}`);
  return res.json();
}

// `rules` null keeps the current rules and only sets the lock.
export async function savePersonWorkingStyle(
  id: number,
  rules: string[] | null,
  locked: boolean,
): Promise<WorkingStyle> {
  const res = await fetch(`${API_BASE}/people/${id}/attunement`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rules, locked }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string" ? body.detail : `Failed to save: ${res.statusText}`,
    );
  }
  return res.json();
}

export async function resetPersonWorkingStyle(id: number): Promise<void> {
  const res = await fetch(`${API_BASE}/people/${id}/attunement`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Failed to reset working style: ${res.statusText}`);
}

// Explicit 👍/👎 on one assistant reply (null clears it).
export async function setMessageFeedback(
  sessionId: string,
  messageId: number,
  feedback: "up" | "down" | null,
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/messages/${messageId}/feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback }),
    },
  );
  if (!res.ok) throw new Error(`Failed to save feedback: ${res.statusText}`);
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
  // Alert lifecycle (alerts/lifecycle.py + alerts/review.py). All optional so
  // older API builds and test mocks keep compiling.
  // Coalescing: how many times the same situation re-fired, and when last.
  occurrence_count?: number;
  last_seen_at?: string | null;
  // The Executive's latest review: verdict ('' | relevant | changed |
  // likely_stale | drafted | routed | merged | resolved | stale — the last
  // two only on closed rows), the one-line "what changed
  // since you last looked", the move the card should lead with, a short
  // "why now", and a deadline when one exists.
  last_reviewed_at?: string | null;
  review_verdict?: string;
  review_note?: string;
  recommended_move?: string;
  why_now?: string;
  due_at?: string | null;
  // Rows the review folded into this one (merge).
  superseded_count?: number;
  // Registry workflow the review suggested as the next step ('' = none).
  suggested_workflow?: string;
  // Drafted artifacts only: the format (body is already Markdown for every
  // format) and, for "link" artifacts, the URL in the connected app.
  artifact_format?: ArtifactFormat | null;
  artifact_url?: string | null;
}

// One autonomous alert-review move since the last delivered morning brief
// (routed / nudged / escalated / drafted / merged / closed / changed).
export interface HandledItem {
  kind: string;
  summary: string;
  at: string;
  alert_id?: number | null;
  // Structured view of the audit row (all additive; `summary` is the fallback).
  event_type?: string;
  headline?: string | null;
  target?: string | null;
  detail?: string;
  // "resolved" | "dismissed" for a close, "proposed" for a gated route, else "".
  outcome?: string;
  evidence_ref?: string;
  superseded_by_alert_id?: number | null;
  // The alert's status NOW: "open" | "resolved" | "dismissed" | "expired" |
  // "merged" | "" — "open" on a closed row means the close was already undone.
  status?: string;
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
  // Parked client slots in multi-client practice mode (2+ slots). Empty for
  // single-company installs. Optional for older API builds + test mocks.
  practice_clients?: ClientCockpitCard[];
  // The signed-in caller resolved to a Person id on the backend (via
  // x-caller-email). Lets the UI split proposals into "routed to me"
  // vs "across the team". Null when no caller could be resolved.
  // Optional to keep existing test mocks and older API builds compiling.
  caller_person_id?: number | null;
  // What the Executive's alert review did on its own since the last morning
  // brief — the "handled overnight" rail with Undo. Optional/default-empty.
  handled_overnight?: HandledItem[];
}

export async function getToday(): Promise<Today> {
  const res = await fetch(`${API_BASE}/today`);
  if (!res.ok) throw new Error(`Failed to load today: ${res.statusText}`);
  return res.json();
}

// The latest morning brief or end-of-day digest that didn't reach the owner,
// and what fixes it. Null while briefs are going out, and for anyone but the
// owner.
export interface BriefDeliveryNotice {
  brief: string;
  at: string;
  problem: string;
  fix: string;
  // False when it couldn't be written, so there is nothing to read.
  readable: boolean;
}

export async function getBriefDelivery(signal?: AbortSignal): Promise<BriefDeliveryNotice | null> {
  const res = await fetch(`${API_BASE}/today/brief-delivery`, { signal });
  if (!res.ok) throw new Error(`Failed to load brief delivery: ${res.statusText}`);
  return res.json();
}

// Solo only: today's top three, as the morning brief picks them. Null in
// team mode and for anyone but the owner. Loaded apart from /today because
// it may read the calendar (up to 4 s).
export interface TopThreeItem {
  key: string;
  kind: "commitment" | "goal" | "project";
  text: string;
  why: string;
  // "10:00–11:00" (local) when a calendar was read on a business day; ""
  // when no free block is left for it; null when no calendar was read.
  slot: string | null;
}

export interface TopThreeToday {
  items: TopThreeItem[];
}

export async function getTopThree(signal?: AbortSignal): Promise<TopThreeToday | null> {
  const res = await fetch(`${API_BASE}/today/top-three`, { signal });
  if (!res.ok) throw new Error(`Failed to load the top three: ${res.statusText}`);
  return res.json();
}

// Solo only: the latest completed weekly review. Null when none has run, in
// team mode and for anyone but the owner. The full review is its run page
// (/jobs/runs/{run_id}).
export interface WeeklyReviewSummary {
  run_id: string;
  completed_at: string;
  period: string;
  // Next week's top three, plain text.
  top_three: string[];
  // Shown when top_three is empty: the review's own note, or its first lines.
  excerpt: string;
}

export async function getWeeklyReview(signal?: AbortSignal): Promise<WeeklyReviewSummary | null> {
  const res = await fetch(`${API_BASE}/today/weekly-review`, { signal });
  if (!res.ok) throw new Error(`Failed to load the weekly review: ${res.statusText}`);
  return res.json();
}

// Mark an alert as read/ack/dismissed. Used by the briefing's
// Approve/Dismiss affordance to groom the "Needs you" queue: any
// non-unread status drops the alert from /today's proposals list.
export async function ackAlert(
  alertId: number,
  status: "read" | "ack" | "dismissed",
  opts: { muteTopic?: string | boolean } = {},
): Promise<void> {
  const body: Record<string, unknown> = { status };
  if (opts.muteTopic) body.mute_topic = opts.muteTopic;
  const res = await fetch(`${API_BASE}/alerts/${alertId}/ack`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to ack alert ${alertId}: ${res.statusText}`);
}

// Groom many alerts at once. The briefing sends explicit ids (it knows which
// cards the caller owns); `older_than_days` / `category` are for ops use.
export async function bulkAckAlerts(body: {
  status: "ack" | "dismissed";
  alert_ids?: number[];
  older_than_days?: number;
  category?: "action" | "monitoring";
}): Promise<{ count: number }> {
  const res = await fetch(`${API_BASE}/alerts/bulk-ack`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to bulk-ack alerts: ${res.statusText}`);
  return res.json();
}

// Undo for an autonomous close / expiry / dismiss: back to the live queue.
export async function reopenAlert(alertId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/alerts/${alertId}/reopen`, { method: "POST" });
  // 409 = already open (not an Undo target any more); callers treat it as done.
  if (!res.ok) throw new Error(`Failed to reopen alert ${alertId}: ${res.status} ${res.statusText}`);
}

export interface AlertReviewSummary {
  reviewed: number;
  closed: number;
  changed: number;
  routed: number;
  nudged: number;
  escalated: number;
  drafted: number;
  merged: number;
  suggested: number;
  annotated: number;
}

// Run the Executive's relevance review on demand ("Re-check relevance").
export async function reviewAlerts(): Promise<AlertReviewSummary> {
  const res = await fetch(`${API_BASE}/alerts/review`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to run alert review: ${res.statusText}`);
  return res.json();
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
/** Who put the row on the watchlist. `research_proposed` = a pending suggestion. */
export type WatchlistOrigin = "manual" | "executive" | "research" | "research_proposed";
/** Why a research suggestion / watch was declined — each picks a different remedy. */
export type WatchDeclineReason = "not_relevant" | "too_noisy" | "wrong_source";

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
  origin: WatchlistOrigin | string;
}

export interface WatchlistSignal {
  id: number;
  watchlist_id: number;
  source_kind: string;
  source_external_id: string;
  captured_at: string;
  /** Upstream publish time (RSS pubDate, EDGAR filing date); null when the source has none. */
  published_at: string | null;
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

export async function deleteWatchlistItem(
  slug: string,
  reason?: WatchDeclineReason,
): Promise<void> {
  const qs = reason ? `?reason=${encodeURIComponent(reason)}` : "";
  const res = await fetch(`${API_BASE}/watchlist/${encodeURIComponent(slug)}${qs}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`Failed to delete watchlist entry: ${res.statusText}`);
}

/** Turn a research suggestion (origin=research_proposed, dry_run) into a live watch. */
export async function approveWatchSuggestion(slug: string): Promise<WatchlistItem> {
  const res = await fetch(`${API_BASE}/watchlist/${encodeURIComponent(slug)}/approve`, {
    method: "POST",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Failed to approve suggestion: ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json();
}

export interface WatchDeclineResponse {
  slug: string;
  reason: string;
  /** "removed" (declined + deleted) or "kept_high_floor" (too_noisy → live, only high-severity signals surface). */
  result: "removed" | "kept_high_floor" | string;
}

/** Decline a research suggestion. The reason picks the remedy server-side. */
export async function declineWatchSuggestion(
  slug: string,
  reason: WatchDeclineReason,
): Promise<WatchDeclineResponse> {
  const res = await fetch(`${API_BASE}/watchlist/${encodeURIComponent(slug)}/decline`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Failed to decline suggestion: ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json();
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
