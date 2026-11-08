"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import {
  DYNAMIC_SPECIALISTS,
  DynamicStep,
  DynamicWorkflowDef,
  Person,
  CustomWorkflowError,
  WorkflowDesignerDraft,
  activateCustomWorkflow,
  createCustomWorkflow,
  saveWorkflowDesignerEdit,
} from "@/lib/api";
import {
  addedTools,
  describeCadenceLocal,
  describeChanges,
  replyShapeLabel,
} from "@/lib/workflowChanges";
import { groupTools, stepApps } from "@/lib/workflowTools";
import ToolAppIcon from "./ToolAppIcon";
import { mayWrite, toolLabel, useToolInfo } from "./ToolChips";

// Keyed by DYNAMIC_SPECIALISTS so adding a specialist there without a label
// here fails the build instead of falling back to the raw key.
const SPECIALIST_LABELS: Record<(typeof DYNAMIC_SPECIALISTS)[number], string> = {
  cso: "Strategy",
  cfo: "Finance",
  chro: "People",
  gc: "Legal",
  coo: "Operations",
  cmo: "Marketing",
  cpo: "Product",
  sales: "Sales",
  board_comms: "Board comms",
};

function specialistLabel(key: string | undefined): string {
  if (!key) return "Strategy";
  return (SPECIALIST_LABELS as Record<string, string>)[key] ?? key;
}

function personName(people: Person[], id: number | null | undefined): string {
  if (id == null) return "someone";
  return people.find((p) => p.id === id)?.full_name ?? `person #${id}`;
}

function stepLine(step: DynamicStep, people: Person[]): { who: string | null; what: string } {
  if (step.kind === "specialist")
    return { who: specialistLabel(step.specialist), what: step.goal };
  if (step.kind === "approval_gate") {
    // A question rather than a yes/no: the run continues whatever the answer.
    const shape = replyShapeLabel(step.expected_reply_shape);
    return {
      who:
        `Sign-off · ${personName(people, step.person_id)}` +
        (shape ? ` · asks for ${shape}, continues whatever the answer` : ""),
      what: step.question,
    };
  }
  // An action step's apps show as icons by its title; its tools are listed
  // once, by app, in "what it can do" below the steps.
  if (step.kind === "action") return { who: null, what: step.goal };
  return {
    who: `Assemble · ${specialistLabel(step.specialist)}`,
    what: step.instructions || "Combines the steps above into the final deliverable.",
  };
}

/**
 * The human check on a workflow before it can act. Modes:
 * - wizard draft (sessionId + onRefine): "Create workflow" saves it;
 * - `edit`: a revision of a saved workflow — lists what changes and
 *   "Save changes" updates it;
 * - `pending`: a workflow chat saved switched off — "Turn on workflow"
 *   activates it, and that click is the approval of its tools;
 * - `readOnly`: the saved workflow as it is, with no actions.
 */
export default function WorkflowDraftReview({
  draft,
  people,
  busy = false,
  sessionId,
  onRefine,
  pending,
  edit,
  readOnly = false,
}: {
  draft: WorkflowDesignerDraft;
  people: Person[];
  busy?: boolean;
  sessionId?: string;
  onRefine?: () => void;
  pending?: { onActivated: () => void };
  edit?: { original: DynamicWorkflowDef };
  readOnly?: boolean;
}) {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // HTTP status of the last error. On a pending card, 409 = the stored
  // workflow changed since this card loaded (its message already says to
  // reload) and 404 = it was deleted; anything else is fixed in the editor.
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const def = draft.definition;
  const stepTools = def.steps.flatMap((s) => (s.kind === "action" ? s.tools : []));
  const toolInfo = useToolInfo(stepTools);
  // Editing: only the tools this revision adds need a fresh look.
  const reviewTools = edit ? addedTools(edit.original, def) : Array.from(new Set(stepTools));
  const toolGroups = groupTools(reviewTools, (t) => !mayWrite(t, toolInfo));
  const changes = edit
    ? describeChanges(edit.original, def, {
        person: (id) => personName(people, id),
        specialist: specialistLabel,
        tool: (t) => toolLabel(t).label,
        cadence: (c) => describeCadenceLocal(c),
      })
    : [];

  async function confirm() {
    setError(null);
    setSaving(true);
    try {
      if (pending) {
        await activateCustomWorkflow(def);
        pending.onActivated();
      } else if (edit && sessionId) {
        const saved = await saveWorkflowDesignerEdit(sessionId, def);
        router.push(`/jobs/${encodeURIComponent(saved.name)}?saved=1`);
      } else {
        const saved = await createCustomWorkflow(def);
        router.push(`/jobs/${encodeURIComponent(saved.name)}?saved=1`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setErrorStatus(e instanceof CustomWorkflowError ? e.status : null);
      setSaving(false);
    }
  }

  const editHref = readOnly
    ? null
    : pending
    ? `/jobs/new?edit=${encodeURIComponent(def.name)}&mode=advanced`
    : edit && sessionId
      ? `/jobs/new?edit=${encodeURIComponent(def.name)}&designer=${encodeURIComponent(sessionId)}`
      : sessionId
      ? `/jobs/new?designer=${encodeURIComponent(sessionId)}`
      : null;

  return (
    <div className="rounded-xl border border-indigo-500/30 bg-surface-elevated/60 p-4 space-y-4">
      <div>
        <p className="text-[10px] uppercase tracking-wide text-indigo-300 mb-1">
          {pending
            ? "Waiting for your approval · off"
            : readOnly
              ? "How it works now"
              : edit
                ? "Proposed changes"
                : "Draft workflow"}
        </p>
        <h3 className="text-base font-semibold text-fg">{def.title}</h3>
        {def.owner_person_id != null && (
          <p className="text-xs text-fg-subtle mt-0.5">
            Created by {personName(people, def.owner_person_id)}
          </p>
        )}
        {def.description && (
          <p className="text-sm text-fg-muted mt-0.5">{def.description}</p>
        )}
        {draft.summary && (
          <p className="text-sm text-fg mt-2 whitespace-pre-wrap">{draft.summary}</p>
        )}
      </div>

      {edit && (
        <div className="rounded-md border border-indigo-500/30 bg-indigo-500/5 px-3 py-2">
          <p className="text-xs text-indigo-300 mb-1">What changes</p>
          {changes.length === 0 ? (
            <p className="text-sm text-fg-muted">
              Nothing has changed yet — tell me what you&rsquo;d like different.
            </p>
          ) : (
            <ul className="list-disc pl-4 space-y-0.5 text-sm text-fg">
              {changes.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
        <div>
          <dt className="text-fg-subtle">Schedule</dt>
          <dd className="text-fg mt-0.5">
            {describeCadenceLocal(def.cadence)}
            {def.cadence && (
              <span className="text-fg-muted">
                {" "}
                · to {personName(people, def.cadence_person_id)}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-fg-subtle">You fill in each run</dt>
          <dd className="text-fg mt-0.5">
            {def.input_fields.length === 0
              ? "Nothing"
              : def.input_fields
                  .map((f) => (f.required === false ? `${f.label} (optional)` : f.label))
                  .join(", ")}
          </dd>
        </div>
        <div>
          <dt className="text-fg-subtle">Takes about</dt>
          <dd className="text-fg mt-0.5">{def.estimated_minutes} min</dd>
        </div>
      </dl>

      <ol className="space-y-2">
        {def.steps.map((step, i) => {
          const { who, what } = stepLine(step, people);
          return (
            <li key={step.id} className="flex gap-3 text-sm">
              <span className="shrink-0 w-5 h-5 rounded-full bg-surface-overlay text-[11px] text-fg-muted flex items-center justify-center mt-0.5">
                {i + 1}
              </span>
              <div className="min-w-0">
                <p className="text-fg">
                  {step.kind === "action" &&
                    stepApps(step.tools).map((app) => (
                      <span key={app.label} className="mr-1.5 inline-flex align-[-1px]">
                        <ToolAppIcon app={app} />
                      </span>
                    ))}
                  {step.title}
                  {who && <span className="text-xs text-fg-muted"> · {who}</span>}
                </p>
                {/* Unclamped: this card is the human check on every goal before it is saved. */}
                <p className="text-xs text-fg-muted whitespace-pre-wrap break-words">{what}</p>
              </div>
            </li>
          );
        })}
      </ol>

      {!readOnly && draft.assumptions.length > 0 && (
        <div className="rounded-md bg-amber-500/5 border border-amber-500/20 px-3 py-2">
          <p className="text-xs text-amber-300 mb-1">Assumptions — tell me if any are wrong</p>
          <ul className="list-disc pl-4 space-y-0.5 text-xs text-fg-muted">
            {draft.assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </div>
      )}

      {toolGroups.length > 0 && (
        <div className="rounded-md border border-indigo-500/30 bg-indigo-500/5 px-3 py-2.5 text-xs">
          <p className="text-fg font-medium mb-1.5">
            {readOnly
              ? "What it can do on each run"
              : pending
                ? "Turning it on lets it do these on every run, without asking again"
                : edit
                  ? "Saving these changes lets it also do these on every run, without asking again"
                  : "Creating it lets it do these on every run, without asking again"}
          </p>
          <ul className="space-y-1">
            {toolGroups.map((g) => (
              <li key={g.app.label} className="flex gap-3">
                <span className="flex w-32 shrink-0 items-center gap-1.5 text-fg-muted">
                  <ToolAppIcon app={g.app} />
                  {g.app.label}
                </span>
                <span className="min-w-0 text-fg">
                  {g.changes.length > 0 && (
                    <>
                      {g.changes.map((a, i) => (
                        <span key={a.action}>
                          {i === 0 ? a.action.charAt(0).toUpperCase() + a.action.slice(1) : a.action}
                          {i < g.changes.length - 1 ? ", " : ""}
                        </span>
                      ))}{" "}
                      <span className="rounded bg-amber-500/15 px-1 text-[10px] text-amber-300">
                        can change things
                      </span>
                    </>
                  )}
                  {g.changes.length > 0 && g.looks.length > 0 && (
                    <span className="text-fg-subtle"> · </span>
                  )}
                  {g.looks.length > 0 && (
                    <>
                      {g.looks.map((a, i) => (
                        <span key={a.action}>
                          {i === 0 && g.changes.length === 0
                            ? a.action.charAt(0).toUpperCase() + a.action.slice(1)
                            : a.action}
                          {i < g.looks.length - 1 ? ", " : ""}
                        </span>
                      ))}{" "}
                      <span className="rounded bg-surface-overlay px-1 text-[10px] text-fg-muted">
                        look only
                      </span>
                    </>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {error && (
        <p className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          {!pending
            ? edit && errorStatus === 409
              ? error
              : `${error} — adjust it in the details editor, or tell me what to change.`
            : errorStatus === 409
              ? error
              : errorStatus === 404
                ? `${error} — it may have been deleted; go back to the workflow list.`
                : `${error} — adjust it in the details editor.`}
        </p>
      )}

      {!readOnly && (
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => void confirm()}
          disabled={saving || busy || (!!edit && changes.length === 0)}
          className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50 transition"
        >
          {pending
            ? saving
              ? "Turning on…"
              : "Turn on workflow"
            : edit
              ? saving
                ? "Saving…"
                : "Save changes"
              : saving
                ? "Creating…"
                : "Create workflow"}
        </button>
        {onRefine && (
          <button
            type="button"
            onClick={onRefine}
            disabled={saving || busy}
            className="text-sm text-fg-muted hover:text-fg disabled:opacity-50 transition"
          >
            Keep refining
          </button>
        )}
        {editHref && (
          <button
            type="button"
            onClick={() => router.push(editHref)}
            disabled={saving || busy}
            className="text-sm text-indigo-400 hover:text-indigo-300 disabled:opacity-50 transition"
          >
            Edit details
          </button>
        )}
      </div>
      )}
    </div>
  );
}
