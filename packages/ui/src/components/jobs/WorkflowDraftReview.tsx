"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import {
  DynamicStep,
  Person,
  WorkflowDesignerDraft,
  createCustomWorkflow,
} from "@/lib/api";

const SPECIALIST_LABELS: Record<string, string> = {
  cso: "Strategy",
  cfo: "Finance",
  chro: "People",
  gc: "Legal",
  coo: "Operations",
  cmo: "Marketing",
  cpo: "Product",
  board_comms: "Board comms",
};

const DAYS: Record<string, string> = {
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};

function specialistLabel(key: string | undefined): string {
  if (!key) return "Strategy";
  return SPECIALIST_LABELS[key] ?? key;
}

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

function personName(people: Person[], id: number | null | undefined): string {
  if (id == null) return "someone";
  return people.find((p) => p.id === id)?.full_name ?? `person #${id}`;
}

function stepLine(step: DynamicStep, people: Person[]): { who: string; what: string } {
  if (step.kind === "specialist")
    return { who: specialistLabel(step.specialist), what: step.goal };
  if (step.kind === "approval_gate")
    return {
      who: `Sign-off · ${personName(people, step.person_id)}`,
      what: step.question,
    };
  return {
    who: `Assemble · ${specialistLabel(step.specialist)}`,
    what: step.instructions || "Combines the steps above into the final deliverable.",
  };
}

export default function WorkflowDraftReview({
  draft,
  sessionId,
  people,
  onRefine,
  busy,
}: {
  draft: WorkflowDesignerDraft;
  sessionId: string;
  people: Person[];
  onRefine: () => void;
  busy: boolean;
}) {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const def = draft.definition;

  async function create() {
    setError(null);
    setSaving(true);
    try {
      const saved = await createCustomWorkflow(def);
      router.push(`/jobs/${encodeURIComponent(saved.name)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  }

  return (
    <div className="rounded-xl border border-indigo-500/30 bg-surface-elevated/60 p-4 space-y-4">
      <div>
        <p className="text-[10px] uppercase tracking-wide text-indigo-300 mb-1">
          Draft workflow
        </p>
        <h3 className="text-base font-semibold text-fg">{def.title}</h3>
        {def.description && (
          <p className="text-sm text-fg-muted mt-0.5">{def.description}</p>
        )}
        {draft.summary && (
          <p className="text-sm text-fg mt-2 whitespace-pre-wrap">{draft.summary}</p>
        )}
      </div>

      <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
        <div>
          <dt className="text-fg-subtle">Schedule</dt>
          <dd className="text-fg mt-0.5">
            {describeCadence(def.cadence)}
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
          <dt className="text-fg-subtle">Section · time</dt>
          <dd className="text-fg mt-0.5">
            {def.section} · ~{def.estimated_minutes} min
          </dd>
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
                  {step.title}{" "}
                  <span className="text-xs text-fg-muted">· {who}</span>
                </p>
                <p className="text-xs text-fg-muted line-clamp-2">{what}</p>
              </div>
            </li>
          );
        })}
      </ol>

      {draft.assumptions.length > 0 && (
        <div className="rounded-md bg-amber-500/5 border border-amber-500/20 px-3 py-2">
          <p className="text-xs text-amber-300 mb-1">Assumptions — tell me if any are wrong</p>
          <ul className="list-disc pl-4 space-y-0.5 text-xs text-fg-muted">
            {draft.assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </div>
      )}

      {error && (
        <p className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          {error} — adjust it in the details editor, or tell me what to change.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => void create()}
          disabled={saving || busy}
          className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50 transition"
        >
          {saving ? "Creating…" : "Create workflow"}
        </button>
        <button
          type="button"
          onClick={onRefine}
          disabled={saving || busy}
          className="text-sm text-fg-muted hover:text-fg disabled:opacity-50 transition"
        >
          Keep refining
        </button>
        <button
          type="button"
          onClick={() =>
            router.push(`/jobs/new?designer=${encodeURIComponent(sessionId)}`)
          }
          disabled={saving || busy}
          className="text-sm text-indigo-400 hover:text-indigo-300 disabled:opacity-50 transition"
        >
          Edit details
        </button>
      </div>
    </div>
  );
}
