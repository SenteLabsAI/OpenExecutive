"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useAskOEFormContext } from "@/components/askoe/AskOEContext";
import ToolPicker from "@/components/jobs/ToolPicker";
import WorkflowWizard from "@/components/jobs/WorkflowWizard";
import {
  DYNAMIC_SPECIALISTS,
  DynamicInputField,
  DynamicStep,
  DynamicWorkflowDef,
  PageFormField,
  Person,
  WorkflowSection,
  createCustomWorkflow,
  getCustomWorkflow,
  getWorkflowDesignerSession,
  listPeople,
  updateCustomWorkflow,
} from "@/lib/api";

const SECTIONS: WorkflowSection[] = [
  "Board",
  "Capital & Investors",
  "Growth & GTM",
  "Product",
  "People",
  "Risk, Legal & Crisis",
  "Operating Cadence",
];

type StepKind = DynamicStep["kind"];

function newStep(kind: StepKind, idx: number): DynamicStep {
  const id = `step_${idx + 1}`;
  if (kind === "specialist")
    return { kind, id, title: "", specialist: "cso", goal: "", rag_query: "" };
  if (kind === "approval_gate")
    return {
      kind,
      id,
      title: "",
      person_id: 0,
      question: "",
      timeout_hours: 48,
      on_timeout: "escalate",
    };
  if (kind === "action")
    return { kind, id, title: "", goal: "", tools: [] };
  return { kind, id, title: "Assemble", instructions: "", specialist: "cso" };
}

const inputCls =
  "w-full px-3 py-1.5 text-sm rounded-md bg-surface/60 border border-line text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-1 focus:ring-indigo-500/40";
const labelCls = "block text-xs font-medium text-fg-muted mb-1";

// ---- Ask OE form descriptor helpers ---------------------------------------

const INPUT_FIELDS_SCHEMA =
  'JSON array of input-field objects: {"name": snake_case string, "label": string, ' +
  '"description"?: string, "required": boolean, "multiline": boolean}. ' +
  "Reference fields in step goals with {field_name} placeholders.";

function stepsSchema(people: Person[]): string {
  const roster = people.map((p) => `${p.id} = ${p.full_name} (${p.role})`).join("; ");
  return (
    "JSON array of step objects, run in order. Four kinds: " +
    '{"kind": "specialist", "id": string, "title": string, "specialist": one of [' +
    DYNAMIC_SPECIALISTS.join(", ") +
    '], "goal": string (may use {field} placeholders), "rag_query"?: string} | ' +
    '{"kind": "approval_gate", "id": string, "title": string, "person_id": number, ' +
    '"question": string, "timeout_hours"?: number, "on_timeout"?: "escalate" | "auto_proceed" | "fail"} | ' +
    '{"kind": "action", "id": string, "title": string, "goal": string (what to get done with tools), ' +
    '"tools": string[] (exact tool names — keep the ones already chosen; new ones must come from the tool search), ' +
    '"max_tool_calls"?: number (1-50)} | ' +
    '{"kind": "synthesis", "id": string, "title": string, "specialist"?: string, "instructions"?: string}. ' +
    "The LAST step must be a synthesis step. " +
    (roster ? `person_id must be one of: ${roster}.` : "No people on the roster yet.")
  );
}

/** Parse a json-typed proposal value (the model may send a JSON string). */
function asJsonValue(raw: unknown): unknown {
  if (typeof raw !== "string") return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function coerceInputFields(raw: unknown): DynamicInputField[] | null {
  const v = asJsonValue(raw);
  if (!Array.isArray(v)) return null;
  const out: DynamicInputField[] = [];
  for (const f of v) {
    if (typeof f !== "object" || f === null) continue;
    const rec = f as Record<string, unknown>;
    if (typeof rec.name !== "string" || typeof rec.label !== "string") continue;
    out.push({
      name: rec.name,
      label: rec.label,
      description: typeof rec.description === "string" ? rec.description : "",
      required: rec.required !== false,
      multiline: rec.multiline === true,
    });
  }
  return out;
}

const STEP_KINDS: ReadonlySet<string> = new Set([
  "specialist",
  "action",
  "approval_gate",
  "synthesis",
]);

function coerceSteps(raw: unknown): DynamicStep[] | null {
  const v = asJsonValue(raw);
  if (!Array.isArray(v)) return null;
  const out: DynamicStep[] = [];
  v.forEach((s, i) => {
    if (typeof s !== "object" || s === null) return;
    const rec = s as Record<string, unknown>;
    const kind = rec.kind;
    if (typeof kind !== "string" || !STEP_KINDS.has(kind)) return;
    // Start from the kind's defaults so missing optional keys stay valid,
    // then overlay whatever the proposal supplied.
    const base = newStep(kind as StepKind, i) as unknown as Record<string, unknown>;
    const merged = { ...base, ...rec, kind } as unknown as DynamicStep;
    if (typeof merged.id !== "string" || !merged.id) merged.id = `step_${i + 1}`;
    out.push(merged);
  });
  return out.length > 0 ? out : null;
}

function BuilderInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const editName = searchParams.get("edit");
  // A wizard session whose draft seeds this form ("Edit details"). Create
  // mode — the draft has not been saved yet.
  const designerId = editName ? null : searchParams.get("designer");

  const [people, setPeople] = useState<Person[]>([]);
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [section, setSection] = useState<WorkflowSection>("Operating Cadence");
  const [estimatedMinutes, setEstimatedMinutes] = useState(4);
  const [fields, setFields] = useState<DynamicInputField[]>([]);
  const [steps, setSteps] = useState<DynamicStep[]>([
    newStep("specialist", 0),
    newStep("synthesis", 1),
  ]);
  const [cadenceEnabled, setCadenceEnabled] = useState(false);
  const [cadence, setCadence] = useState("weekly@mon@09:00");
  const [cadencePersonId, setCadencePersonId] = useState<number>(0);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!editName || !!designerId);
  // Saving edits keeps a switched-off workflow off: turning it on from its
  // review card is the approval step, not "Save changes".
  const [isActive, setIsActive] = useState(true);

  useEffect(() => {
    listPeople()
      .then((p) => setPeople(p.filter((x) => !x.archived)))
      .catch(() => setPeople([]));
  }, []);

  useEffect(() => {
    const load: Promise<DynamicWorkflowDef> | null = editName
      ? getCustomWorkflow(editName)
      : designerId
      ? getWorkflowDesignerSession(designerId).then((t) => {
          if (!t.draft) throw new Error("That conversation has no draft yet.");
          return t.draft.definition;
        })
      : null;
    if (!load) return;
    load
      .then((d) => {
        setName(d.name);
        setTitle(d.title);
        setDescription(d.description ?? "");
        setSection(d.section);
        setEstimatedMinutes(d.estimated_minutes);
        setFields(d.input_fields);
        setSteps(d.steps);
        if (editName) setIsActive(d.is_active !== false);
        if (d.cadence) {
          setCadenceEnabled(true);
          setCadence(d.cadence);
          setCadencePersonId(d.cadence_person_id ?? 0);
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [editName, designerId]);

  // ---- Ask OE registration -------------------------------------------------
  // Recreated each render so getFields/applyPatch close over current state;
  // the hook re-registers only when formId changes and refreshes closures
  // on every render.
  const { suggestedCls, clearSuggested } = useAskOEFormContext({
    formId: "workflow_builder",
    title: editName ? "Edit workflow" : "New workflow",
    description:
      "Builds a reusable executive job from specialist steps, optional approval gates, and a final synthesis step.",
    getFields: (): PageFormField[] => [
      {
        name: "name",
        label: "Name (snake_case, unique)",
        type: "text",
        value: name,
        required: true,
        description: editName
          ? "Immutable — this workflow already exists."
          : "snake_case unique identifier, e.g. weekly_competitor_watch.",
      },
      { name: "title", label: "Title", type: "text", value: title, required: true },
      { name: "description", label: "Description", type: "text", value: description },
      {
        name: "section",
        label: "Section",
        type: "select",
        options: [...SECTIONS],
        value: section,
      },
      {
        name: "estimated_minutes",
        label: "Estimated minutes",
        type: "number",
        value: estimatedMinutes,
        description: "1-120.",
      },
      {
        name: "input_fields",
        label: "Input fields",
        type: "json",
        value: fields,
        description: INPUT_FIELDS_SCHEMA,
      },
      {
        name: "steps",
        label: "Steps",
        type: "json",
        value: steps,
        required: true,
        description: stepsSchema(people),
      },
      {
        name: "cadence_enabled",
        label: "Run on a schedule",
        type: "boolean",
        value: cadenceEnabled,
      },
      {
        name: "cadence",
        label: "Cadence",
        type: "text",
        value: cadence,
        description: "daily@HH:MM / weekly@DOW@HH:MM / quarterly@DD-HH:MM, UTC.",
      },
      {
        name: "cadence_person_id",
        label: "Deliver artifact to (person id)",
        type: "number",
        value: cadencePersonId,
        description:
          people.map((p) => `${p.id} = ${p.full_name}`).join("; ") || "No people yet.",
      },
    ],
    applyPatch: (values) => {
      const prior = {
        name, title, description, section, estimatedMinutes,
        fields, steps, cadenceEnabled, cadence, cadencePersonId,
      };
      const applied: string[] = [];
      const skipped: string[] = [];
      for (const [key, raw] of Object.entries(values)) {
        switch (key) {
          case "name":
            if (editName || typeof raw !== "string") skipped.push(key);
            else { setName(raw); applied.push(key); }
            break;
          case "title":
            if (typeof raw !== "string") skipped.push(key);
            else { setTitle(raw); applied.push(key); }
            break;
          case "description":
            if (typeof raw !== "string") skipped.push(key);
            else { setDescription(raw); applied.push(key); }
            break;
          case "section":
            if (typeof raw === "string" && (SECTIONS as string[]).includes(raw)) {
              setSection(raw as WorkflowSection);
              applied.push(key);
            } else skipped.push(key);
            break;
          case "estimated_minutes": {
            const n = Number(raw);
            if (Number.isFinite(n) && n >= 1 && n <= 120) {
              setEstimatedMinutes(Math.round(n));
              applied.push(key);
            } else skipped.push(key);
            break;
          }
          case "input_fields": {
            const parsed = coerceInputFields(raw);
            if (parsed !== null) { setFields(parsed); applied.push(key); }
            else skipped.push(key);
            break;
          }
          case "steps": {
            const parsed = coerceSteps(raw);
            if (parsed !== null) { setSteps(parsed); applied.push(key); }
            else skipped.push(key);
            break;
          }
          case "cadence_enabled":
            if (typeof raw === "boolean") { setCadenceEnabled(raw); applied.push(key); }
            else skipped.push(key);
            break;
          case "cadence":
            if (typeof raw !== "string") skipped.push(key);
            else { setCadence(raw); setCadenceEnabled(true); applied.push(key); }
            break;
          case "cadence_person_id": {
            const n = Number(raw);
            if (Number.isFinite(n) && people.some((p) => p.id === n)) {
              setCadencePersonId(n);
              applied.push(key);
            } else skipped.push(key);
            break;
          }
          default:
            skipped.push(key);
        }
      }
      return {
        applied,
        skipped,
        undo: () => {
          setName(prior.name);
          setTitle(prior.title);
          setDescription(prior.description);
          setSection(prior.section);
          setEstimatedMinutes(prior.estimatedMinutes);
          setFields(prior.fields);
          setSteps(prior.steps);
          setCadenceEnabled(prior.cadenceEnabled);
          setCadence(prior.cadence);
          setCadencePersonId(prior.cadencePersonId);
        },
      };
    },
  });

  const updateField = useCallback(
    (i: number, patch: Partial<DynamicInputField>) =>
      setFields((fs) => fs.map((f, idx) => (idx === i ? { ...f, ...patch } : f))),
    []
  );
  const updateStep = useCallback(
    (i: number, patch: Partial<DynamicStep>) =>
      setSteps((ss) =>
        ss.map((s, idx) => (idx === i ? ({ ...s, ...patch } as DynamicStep) : s))
      ),
    []
  );
  const moveStep = useCallback(
    (i: number, dir: -1 | 1) =>
      setSteps((ss) => {
        const j = i + dir;
        if (j < 0 || j >= ss.length) return ss;
        const next = [...ss];
        [next[i], next[j]] = [next[j], next[i]];
        return next;
      }),
    []
  );

  async function handleSave() {
    setError(null);
    setSaving(true);
    const def: DynamicWorkflowDef = {
      name: name.trim(),
      title: title.trim(),
      description: description.trim(),
      section,
      estimated_minutes: estimatedMinutes,
      input_fields: fields,
      steps,
      cadence: cadenceEnabled ? cadence.trim() : null,
      cadence_person_id: cadenceEnabled ? cadencePersonId : null,
      is_active: isActive,
    };
    try {
      if (editName) await updateCustomWorkflow(editName, def);
      else await createCustomWorkflow(def);
      router.push(`/jobs/${encodeURIComponent(def.name)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  }

  if (loading) {
    return <div className="text-sm text-fg-muted">Loading…</div>;
  }

  return (
    <div className="space-y-8">
      {/* Metadata */}
      <section className="space-y-4">
        <h2 className="text-base font-semibold text-fg">Details</h2>
        <div className="grid sm:grid-cols-2 gap-4">
          <div>
            <label className={labelCls}>Name (snake_case, unique)</label>
            <input
              className={`${inputCls} ${suggestedCls("name")}`}
              value={name}
              disabled={!!editName}
              onChange={(e) => { setName(e.target.value); clearSuggested("name"); }}
              placeholder="weekly_competitor_watch"
            />
          </div>
          <div>
            <label className={labelCls}>Title</label>
            <input
              className={`${inputCls} ${suggestedCls("title")}`}
              value={title}
              onChange={(e) => { setTitle(e.target.value); clearSuggested("title"); }}
              placeholder="Weekly Competitor Watch"
            />
          </div>
        </div>
        <div>
          <label className={labelCls}>Description</label>
          <input
            className={`${inputCls} ${suggestedCls("description")}`}
            value={description}
            onChange={(e) => { setDescription(e.target.value); clearSuggested("description"); }}
            placeholder="What this workflow produces"
          />
        </div>
        <div className="grid sm:grid-cols-2 gap-4">
          <div>
            <label className={labelCls}>Section</label>
            <select
              className={`${inputCls} ${suggestedCls("section")}`}
              value={section}
              onChange={(e) => { setSection(e.target.value as WorkflowSection); clearSuggested("section"); }}
            >
              {SECTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelCls}>Estimated minutes</label>
            <input
              type="number"
              min={1}
              max={120}
              className={`${inputCls} ${suggestedCls("estimated_minutes")}`}
              value={estimatedMinutes}
              onChange={(e) => { setEstimatedMinutes(Number(e.target.value)); clearSuggested("estimated_minutes"); }}
            />
          </div>
        </div>
      </section>

      {/* Input fields */}
      <section
        className={`space-y-3 rounded-md ${suggestedCls("input_fields")}`}
        onInput={() => clearSuggested("input_fields")}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-fg">Input fields</h2>
          <button
            type="button"
            className="text-xs text-indigo-400 hover:text-indigo-300"
            onClick={() =>
              setFields((fs) => [
                ...fs,
                { name: "", label: "", description: "", required: true, multiline: false },
              ])
            }
          >
            + Add field
          </button>
        </div>
        <p className="text-xs text-fg-muted">
          Free-text fields the user fills when running. Reference them in step
          goals with <code>{"{field_name}"}</code>.
        </p>
        {fields.length === 0 && (
          <p className="text-xs text-fg-subtle">No input fields.</p>
        )}
        {fields.map((f, i) => (
          <div
            key={i}
            className="rounded-md border border-line bg-surface/30 p-3 grid sm:grid-cols-[1fr_1fr_auto] gap-3 items-end"
          >
            <div>
              <label className={labelCls}>Field name</label>
              <input
                className={inputCls}
                value={f.name}
                onChange={(e) => updateField(i, { name: e.target.value })}
                placeholder="topic"
              />
            </div>
            <div>
              <label className={labelCls}>Label</label>
              <input
                className={inputCls}
                value={f.label}
                onChange={(e) => updateField(i, { label: e.target.value })}
                placeholder="Topic"
              />
            </div>
            <div className="flex items-center gap-3 pb-1.5">
              <label className="flex items-center gap-1 text-xs text-fg-muted">
                <input
                  type="checkbox"
                  checked={f.required}
                  onChange={(e) => updateField(i, { required: e.target.checked })}
                />
                Required
              </label>
              <button
                type="button"
                className="text-xs text-fg-muted hover:text-red-400"
                onClick={() => setFields((fs) => fs.filter((_, idx) => idx !== i))}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </section>

      {/* Steps */}
      <section
        className={`space-y-3 rounded-md ${suggestedCls("steps")}`}
        onInput={() => clearSuggested("steps")}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-fg">Steps</h2>
          <div className="flex gap-2">
            <button
              type="button"
              className="text-xs text-indigo-400 hover:text-indigo-300"
              onClick={() =>
                setSteps((ss) => [...ss, newStep("specialist", ss.length)])
              }
            >
              + Specialist
            </button>
            <button
              type="button"
              className="text-xs text-indigo-400 hover:text-indigo-300"
              onClick={() => setSteps((ss) => [...ss, newStep("action", ss.length)])}
            >
              + Action
            </button>
            <button
              type="button"
              className="text-xs text-indigo-400 hover:text-indigo-300"
              onClick={() =>
                setSteps((ss) => [...ss, newStep("approval_gate", ss.length)])
              }
            >
              + Approval gate
            </button>
          </div>
        </div>
        <p className="text-xs text-fg-muted">
          Steps run in order. <b>Specialist</b> steps analyze and write;{" "}
          <b>action</b> steps get things done with the tools you choose. The last
          step must be a <b>synthesis</b> step that assembles the result. Place any
          approval gate just before it.
        </p>
        {steps.map((s, i) => (
          <StepEditor
            key={i}
            step={s}
            index={i}
            total={steps.length}
            people={people}
            onChange={(patch) => updateStep(i, patch)}
            onMove={(dir) => moveStep(i, dir)}
            onRemove={() => setSteps((ss) => ss.filter((_, idx) => idx !== i))}
          />
        ))}
      </section>

      {/* Cadence */}
      <section className="space-y-3">
        <label className="flex items-center gap-2 text-base font-semibold text-fg">
          <input
            type="checkbox"
            checked={cadenceEnabled}
            onChange={(e) => setCadenceEnabled(e.target.checked)}
          />
          Run on a schedule
        </label>
        {cadenceEnabled && (
          <div className="grid sm:grid-cols-2 gap-4">
            <div>
              <label className={labelCls}>
                Cadence (daily@HH:MM / weekly@DOW@HH:MM / quarterly@DD-HH:MM, UTC)
              </label>
              <input
                className={`${inputCls} ${suggestedCls("cadence")}`}
                value={cadence}
                onChange={(e) => { setCadence(e.target.value); clearSuggested("cadence"); }}
                placeholder="weekly@mon@09:00"
              />
            </div>
            <div>
              <label className={labelCls}>Deliver artifact to</label>
              <select
                className={`${inputCls} ${suggestedCls("cadence_person_id")}`}
                value={cadencePersonId}
                onChange={(e) => { setCadencePersonId(Number(e.target.value)); clearSuggested("cadence_person_id"); }}
              >
                <option value={0}>Select a person…</option>
                {people.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.full_name} — {p.role}
                  </option>
                ))}
              </select>
            </div>
            <p className="sm:col-span-2 text-xs text-fg-subtle">
              Scheduled runs supply no inputs, so a scheduled workflow must have
              no <b>required</b> input fields.
            </p>
          </div>
        )}
      </section>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="flex items-center gap-3 border-t border-line pt-4">
        <button
          type="button"
          disabled={saving}
          onClick={handleSave}
          className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50 transition"
        >
          {saving ? "Saving…" : editName ? "Save changes" : "Create workflow"}
        </button>
        <Link
          href={
            designerId
              ? `/jobs/new?session=${encodeURIComponent(designerId)}`
              : "/jobs"
          }
          className="text-sm text-fg-muted hover:text-fg"
        >
          {designerId ? "Back to conversation" : "Cancel"}
        </Link>
      </div>
    </div>
  );
}

function StepEditor({
  step,
  index,
  total,
  people,
  onChange,
  onMove,
  onRemove,
}: {
  step: DynamicStep;
  index: number;
  total: number;
  people: Person[];
  onChange: (patch: Partial<DynamicStep>) => void;
  onMove: (dir: -1 | 1) => void;
  onRemove: () => void;
}) {
  return (
    <div className="rounded-md border border-line bg-surface/30 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-fg-muted">
          {index + 1}. {step.kind.replace("_", " ")}
        </span>
        <div className="flex items-center gap-2 text-xs text-fg-muted">
          <button type="button" onClick={() => onMove(-1)} disabled={index === 0}>
            ↑
          </button>
          <button
            type="button"
            onClick={() => onMove(1)}
            disabled={index === total - 1}
          >
            ↓
          </button>
          <button
            type="button"
            className="hover:text-red-400"
            onClick={onRemove}
          >
            Remove
          </button>
        </div>
      </div>

      <div className="grid sm:grid-cols-2 gap-3">
        <div>
          <label className={labelCls}>Step id</label>
          <input
            className={inputCls}
            value={step.id}
            onChange={(e) => onChange({ id: e.target.value })}
          />
        </div>
        <div>
          <label className={labelCls}>Title</label>
          <input
            className={inputCls}
            value={step.title}
            onChange={(e) => onChange({ title: e.target.value })}
          />
        </div>
      </div>

      {step.kind === "specialist" && (
        <>
          <div>
            <label className={labelCls}>Specialist</label>
            <select
              className={inputCls}
              value={step.specialist}
              onChange={(e) => onChange({ specialist: e.target.value })}
            >
              {DYNAMIC_SPECIALISTS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelCls}>Goal (use {"{field}"} placeholders)</label>
            <textarea
              className={`${inputCls} min-h-[80px]`}
              value={step.goal}
              onChange={(e) => onChange({ goal: e.target.value })}
            />
          </div>
          <div>
            <label className={labelCls}>Knowledge base query (optional)</label>
            <input
              className={inputCls}
              value={step.rag_query ?? ""}
              onChange={(e) => onChange({ rag_query: e.target.value })}
            />
          </div>
        </>
      )}

      {step.kind === "action" && (
        <>
          <div>
            <label className={labelCls}>
              Goal — what to get done, and where (use {"{field}"} placeholders)
            </label>
            <textarea
              className={`${inputCls} min-h-[80px]`}
              value={step.goal}
              placeholder="e.g. Find today's emailed bills, read each PDF, and add a row per bill (vendor, amount, due date) to the Bill tracker sheet. Skip bills already listed."
              onChange={(e) => onChange({ goal: e.target.value })}
            />
          </div>
          <div>
            <label className={labelCls}>
              Tools this step may use — saving the workflow approves them
            </label>
            <ToolPicker
              value={step.tools}
              onChange={(tools) => onChange({ tools })}
              inputCls={inputCls}
            />
          </div>
          <div className="sm:w-48">
            <label className={labelCls}>Max tool calls per run</label>
            <input
              type="number"
              min={1}
              max={50}
              className={inputCls}
              placeholder="20 (default)"
              value={step.max_tool_calls ?? ""}
              onChange={(e) =>
                // Empty means "use the server default" — undefined is dropped
                // from the saved JSON, where 0 would fail validation.
                onChange({
                  max_tool_calls: e.target.value === "" ? undefined : Number(e.target.value),
                })
              }
            />
          </div>
        </>
      )}

      {step.kind === "approval_gate" && (
        <>
          <div>
            <label className={labelCls}>Ask which person</label>
            <select
              className={inputCls}
              value={step.person_id}
              onChange={(e) => onChange({ person_id: Number(e.target.value) })}
            >
              <option value={0}>Select a person…</option>
              {people.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.full_name} — {p.role}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelCls}>Question</label>
            <textarea
              className={`${inputCls} min-h-[60px]`}
              value={step.question}
              onChange={(e) => onChange({ question: e.target.value })}
            />
          </div>
          <div className="grid sm:grid-cols-2 gap-3">
            <div>
              <label className={labelCls}>Timeout (hours)</label>
              <input
                type="number"
                min={1}
                max={720}
                className={inputCls}
                value={step.timeout_hours ?? 48}
                onChange={(e) => onChange({ timeout_hours: Number(e.target.value) })}
              />
            </div>
            <div>
              <label className={labelCls}>On timeout</label>
              <select
                className={inputCls}
                value={step.on_timeout ?? "escalate"}
                onChange={(e) =>
                  onChange({
                    on_timeout: e.target.value as "escalate" | "auto_proceed" | "fail",
                  })
                }
              >
                <option value="escalate">escalate</option>
                <option value="auto_proceed">auto_proceed</option>
                <option value="fail">fail</option>
              </select>
            </div>
          </div>
        </>
      )}

      {step.kind === "synthesis" && (
        <>
          <div>
            <label className={labelCls}>Synthesis specialist</label>
            <select
              className={inputCls}
              value={step.specialist ?? "cso"}
              onChange={(e) => onChange({ specialist: e.target.value })}
            >
              {DYNAMIC_SPECIALISTS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelCls}>
              Instructions (optional — leave blank to just concatenate sections)
            </label>
            <textarea
              className={`${inputCls} min-h-[60px]`}
              value={step.instructions ?? ""}
              onChange={(e) => onChange({ instructions: e.target.value })}
            />
          </div>
        </>
      )}
    </div>
  );
}

function AdvancedBuilderPage() {
  const searchParams = useSearchParams();
  const editing = !!searchParams.get("edit");
  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-6 py-8">
        <div className="max-w-3xl mx-auto">
          <div className="mb-6">
            <Link href="/jobs" className="text-xs text-fg-muted hover:text-fg">
              ← Back to jobs
            </Link>
            <h1 className="text-2xl font-semibold text-fg mt-2 mb-1">
              {editing ? "Edit workflow" : "New workflow"}
            </h1>
            <p className="text-sm text-fg-muted">
              Build a reusable executive job from specialist steps, optional
              approval gates, and a final synthesis step.
              {!editing && (
                <>
                  {" "}
                  <Link href="/jobs/new" className="text-indigo-400 hover:text-indigo-300">
                    Describe it instead
                  </Link>{" "}
                  and let the assistant draft it.
                </>
              )}
            </p>
          </div>
          <BuilderInner />
        </div>
      </main>
    </div>
  );
}

function WizardPage() {
  return (
    <div className="flex flex-col h-full min-h-0 bg-surface text-fg">
      <div className="border-b border-line px-6 py-4">
        <div className="max-w-3xl mx-auto">
          <Link href="/jobs" className="text-xs text-fg-muted hover:text-fg">
            ← Back to jobs
          </Link>
          <h1 className="text-xl font-semibold text-fg mt-1">New workflow</h1>
        </div>
      </div>
      <div className="flex-1 min-h-0">
        <WorkflowWizard />
      </div>
    </div>
  );
}

function NewWorkflowRouter() {
  const searchParams = useSearchParams();
  // The step-by-step form is the advanced editor: editing a saved workflow,
  // refining a wizard draft ("Edit details"), or opting in explicitly.
  const advanced =
    !!searchParams.get("edit") ||
    !!searchParams.get("designer") ||
    searchParams.get("mode") === "advanced";
  return advanced ? <AdvancedBuilderPage /> : <WizardPage />;
}

export default function NewWorkflowPage() {
  return (
    <Suspense fallback={null}>
      <NewWorkflowRouter />
    </Suspense>
  );
}
