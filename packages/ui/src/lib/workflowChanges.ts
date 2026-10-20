// Plain-words view of a custom workflow: its schedule, and what a revision
// changes compared with the saved version. Pure (type imports only), so the
// node test runner can load it.
import type { DynamicStep, DynamicWorkflowDef } from "./api";

const DAYS: Record<string, string> = {
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};

/** Plain-words rendering of the cadence DSL (daily@HH:MM, weekly@DOW@HH:MM, quarterly@DD-HH:MM). */
export function describeCadence(cadence: string | null | undefined): string {
  if (!cadence) return "Only when you run it";
  const parts = cadence.split("@");
  if (parts[0] === "daily" && parts[1]) return `Every day at ${parts[1]} UTC`;
  if (parts[0] === "weekly" && parts[1] && parts[2]) {
    const day = DAYS[parts[1].toLowerCase()] ?? parts[1];
    return `Every ${day} at ${parts[2]} UTC`;
  }
  if (parts[0] === "quarterly" && parts[1]) {
    const [dd, time] = parts[1].split("-");
    return `Quarterly on day ${Number(dd)} at ${time} UTC`;
  }
  return cadence;
}

/** How to name people, specialists and tools — supplied by the page. */
export interface ChangeLabels {
  person: (id: number | null | undefined) => string;
  specialist: (key: string | undefined) => string;
  tool: (name: string) => string;
}

const ON_TIMEOUT: Record<string, string> = {
  escalate: "flags it for you",
  auto_proceed: "carries on as if approved",
  fail: "stops the run",
};

function list(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function stepWho(step: DynamicStep, labels: ChangeLabels): string {
  if (step.kind === "specialist") return labels.specialist(step.specialist);
  if (step.kind === "approval_gate") return `sign-off from ${labels.person(step.person_id)}`;
  if (step.kind === "action")
    return `uses ${step.tools.length} ${step.tools.length === 1 ? "tool" : "tools"}`;
  return "final write-up";
}

function stepText(step: DynamicStep): string {
  if (step.kind === "approval_gate") return step.question;
  if (step.kind === "synthesis") return step.instructions ?? "";
  return step.goal;
}

// What a sign-off asks for. Anything but approve/reject is a question, not
// permission: the run carries on whatever the answer, so say so.
const REPLY_SHAPE: Record<string, string> = {
  approve_reject: "an approve/reject decision (the run stops unless approved)",
  free_text: "a written reply (the run continues whatever the answer)",
  numeric: "a number (the run continues whatever the answer)",
  document: "a document (the run continues whatever the answer)",
};
const REPLY_SHAPE_SHORT: Record<string, string> = {
  approve_reject: "an approve/reject decision",
  free_text: "a written reply",
  numeric: "a number",
  document: "a document",
};

// Fields each step kind's sentences cover, and the defaults the server fills
// in, so a field the model left out reads the same as its default. Any other
// field that differs still gets a line (see `otherFieldsDiffer`).
const STEP_DEFAULTS: Record<string, Record<string, unknown>> = {
  common: { kind: undefined, id: undefined, title: undefined, description: "" },
  specialist: { specialist: undefined, goal: undefined, rag_query: "", playbook: "" },
  approval_gate: {
    person_id: undefined,
    question: undefined,
    timeout_hours: 48,
    on_timeout: "escalate",
    expected_reply_shape: "approve_reject",
  },
  synthesis: { instructions: "", specialist: "cso" },
  action: { goal: undefined, tools: undefined, max_tool_calls: 20 },
};

function field(obj: object, key: string, defaults: Record<string, unknown>): unknown {
  const v = (obj as Record<string, unknown>)[key];
  return v === undefined || v === null ? defaults[key] : v;
}

/** True when a field not in `covered` differs (a field this file doesn't know yet). */
function otherFieldsDiffer(a: object, b: object, covered: string[]): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) {
    if (covered.includes(k)) continue;
    const va = (a as Record<string, unknown>)[k];
    const vb = (b as Record<string, unknown>)[k];
    if (JSON.stringify(va ?? null) !== JSON.stringify(vb ?? null)) return true;
  }
  return false;
}

function stepChanges(a: DynamicStep, b: DynamicStep, labels: ChangeLabels): string[] {
  const out: string[] = [];
  const name = `“${b.title}”`;
  const defaults = { ...STEP_DEFAULTS.common, ...STEP_DEFAULTS[b.kind] };
  const get = (s: DynamicStep, k: string) => field(s, k, defaults);
  if (a.title !== b.title) out.push(`Renamed step “${a.title}” to ${name}`);
  if (a.kind === "specialist" && b.kind === "specialist" && a.specialist !== b.specialist)
    out.push(`${name} is now handled by ${labels.specialist(b.specialist)} instead of ${labels.specialist(a.specialist)}`);
  if (a.kind === "synthesis" && b.kind === "synthesis" && get(a, "specialist") !== get(b, "specialist"))
    out.push(`${name} is now written by ${labels.specialist(b.specialist)} instead of ${labels.specialist(a.specialist)}`);
  if (a.kind === "approval_gate" && b.kind === "approval_gate") {
    if (a.person_id !== b.person_id)
      out.push(`${name}: sign-off from ${labels.person(b.person_id)} instead of ${labels.person(a.person_id)}`);
    const sa = String(get(a, "expected_reply_shape"));
    const sb = String(get(b, "expected_reply_shape"));
    if (sa !== sb)
      out.push(`${name} now asks for ${REPLY_SHAPE[sb] ?? sb}, not ${REPLY_SHAPE_SHORT[sa] ?? sa}`);
    const ha = get(a, "timeout_hours");
    const hb = get(b, "timeout_hours");
    if (ha !== hb) out.push(`${name}: waits up to ${hb} hours for an answer (was ${ha})`);
    const tb = String(get(b, "on_timeout"));
    if (get(a, "on_timeout") !== tb) out.push(`${name}: if nobody answers, it now ${ON_TIMEOUT[tb] ?? tb}`);
  }
  if (a.kind === "action" && b.kind === "action") {
    const added = b.tools.filter((t) => !a.tools.includes(t));
    const removed = a.tools.filter((t) => !b.tools.includes(t));
    if (added.length) out.push(`${name} can now use ${list(added.map(labels.tool))}`);
    if (removed.length) out.push(`${name} no longer uses ${list(removed.map(labels.tool))}`);
    const ma = get(a, "max_tool_calls");
    const mb = get(b, "max_tool_calls");
    if (ma !== mb) out.push(`${name} may now use tools up to ${mb} times a run (was ${ma})`);
  }
  if (a.kind === "specialist" && b.kind === "specialist") {
    if (get(a, "playbook") !== get(b, "playbook"))
      out.push(
        b.playbook
          ? `${name} now follows the “${b.playbook}” playbook`
          : `${name} no longer follows a playbook`
      );
    if (get(a, "rag_query") !== get(b, "rag_query"))
      out.push(`${name} looks up different things in your documents`);
  }
  if (stepText(a).trim() !== stepText(b).trim()) out.push(`New instructions for ${name}`);
  if (String(get(a, "description")).trim() !== String(get(b, "description")).trim())
    out.push(`Updated the description of ${name}`);
  if (otherFieldsDiffer(a, b, Object.keys(defaults))) out.push(`Changed other settings of ${name}`);
  return out;
}

// Top-level fields covered above, plus ones the server owns (not changes).
const TOP_LEVEL_FIELDS = [
  "name", "title", "description", "section", "estimated_minutes", "input_fields", "steps",
  "cadence", "cadence_person_id", "is_active", "created_at", "updated_at", "owner_person_id",
];

/** Plain words for what a sign-off asks for (shown on the review card). */
export function replyShapeLabel(shape: string | undefined): string | null {
  if (!shape || shape === "approve_reject") return null;
  return REPLY_SHAPE_SHORT[shape] ?? shape;
}

/**
 * What `after` changes compared with `before`, one plain sentence per change.
 * Empty when nothing a person would notice changed.
 */
export function describeChanges(
  before: DynamicWorkflowDef,
  after: DynamicWorkflowDef,
  labels: ChangeLabels
): string[] {
  const out: string[] = [];

  if (before.title !== after.title) out.push(`Renamed “${before.title}” to “${after.title}”`);
  if ((before.description ?? "").trim() !== (after.description ?? "").trim())
    out.push("Updated the description");
  if (before.section !== after.section) out.push(`Moved from ${before.section} to ${after.section}`);
  if (before.estimated_minutes !== after.estimated_minutes)
    out.push(`Expected to take about ${after.estimated_minutes} min (was ${before.estimated_minutes})`);

  // Schedule.
  const ca = before.cadence || null;
  const cb = after.cadence || null;
  if (ca !== cb) {
    out.push(
      cb
        ? `Schedule: ${describeCadence(cb)}, sent to ${labels.person(after.cadence_person_id)} (was: ${describeCadence(ca)})`
        : `Schedule: only when you run it (was: ${describeCadence(ca)})`
    );
  } else if (cb && before.cadence_person_id !== after.cadence_person_id) {
    out.push(
      `Scheduled results go to ${labels.person(after.cadence_person_id)} instead of ${labels.person(before.cadence_person_id)}`
    );
  }

  // Inputs, matched by field name.
  const fieldsBefore = new Map(before.input_fields.map((f) => [f.name, f]));
  const fieldsAfter = new Map(after.input_fields.map((f) => [f.name, f]));
  for (const f of after.input_fields) {
    const old = fieldsBefore.get(f.name);
    if (!old) {
      out.push(`Asks for “${f.label}” on each run${f.required === false ? " (optional)" : ""}`);
      continue;
    }
    if (old.label !== f.label) out.push(`Input “${old.label}” is now called “${f.label}”`);
    if ((old.required !== false) !== (f.required !== false))
      out.push(`“${f.label}” is now ${f.required === false ? "optional" : "required"}`);
    if (
      (old.description ?? "").trim() !== (f.description ?? "").trim() ||
      !!old.multiline !== !!f.multiline ||
      otherFieldsDiffer(old, f, ["name", "label", "required", "description", "multiline"])
    )
      out.push(`Changed how the form asks for “${f.label}”`);
  }
  for (const f of before.input_fields)
    if (!fieldsAfter.has(f.name)) out.push(`No longer asks for “${f.label}”`);

  // Steps: matched by id, then by title for any the revision re-numbered.
  const unmatched = new Map(before.steps.map((s) => [s.id, s]));
  const pairs: [DynamicStep | null, DynamicStep][] = after.steps.map((s) => {
    const old = unmatched.get(s.id);
    if (old && old.kind === s.kind) {
      unmatched.delete(s.id);
      return [old, s];
    }
    return [null, s];
  });
  for (const pair of pairs) {
    if (pair[0]) continue;
    const s = pair[1];
    const byTitle = Array.from(unmatched.values()).find(
      (o) => o.kind === s.kind && o.title === s.title
    );
    if (byTitle) {
      unmatched.delete(byTitle.id);
      pair[0] = byTitle;
    }
  }
  pairs.forEach(([old, s], i) => {
    if (!old) out.push(`Added step ${i + 1}: “${s.title}” (${stepWho(s, labels)})`);
    else out.push(...stepChanges(old, s, labels));
  });
  for (const old of unmatched.values()) out.push(`Removed step “${old.title}”`);

  const kept = pairs.flatMap(([old]) => (old ? [old.id] : []));
  const keptBefore = before.steps.map((s) => s.id).filter((id) => kept.includes(id));
  if (kept.join("\n") !== keptBefore.join("\n")) out.push("Changed the order of the steps");

  if (otherFieldsDiffer(before, after, TOP_LEVEL_FIELDS)) out.push("Changed other settings");

  return out;
}

/** Tool names a revision adds that the saved version's steps did not use. */
export function addedTools(before: DynamicWorkflowDef, after: DynamicWorkflowDef): string[] {
  const had = new Set(before.steps.flatMap((s) => (s.kind === "action" ? s.tools : [])));
  return Array.from(
    new Set(after.steps.flatMap((s) => (s.kind === "action" ? s.tools : [])))
  ).filter((t) => !had.has(t));
}
