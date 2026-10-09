"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useParams, useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import WorkflowRunner from "@/components/WorkflowRunner";
import Button, { buttonClass } from "@/components/ui/Button";
import ApprovedTargets from "@/components/jobs/ApprovedTargets";
import PendingWorkflowReview from "@/components/jobs/PendingWorkflowReview";
import {
  DynamicWorkflowDef,
  WorkflowInputFieldSchema,
  WorkflowMeta,
  getCustomWorkflow,
  getWorkflow,
  getWorkflowSample,
} from "@/lib/api";
import { describeCadence } from "@/lib/workflowChanges";

type FormState = Record<string, string>;

// Decode the `?prefill=` query param (base64url-encoded UTF-8 JSON).
// Returns an empty object on any decode error — a malformed link must
// not crash the page. Uses TextDecoder so multi-byte UTF-8 chars (em
// dashes, smart quotes, non-ASCII names) round-trip cleanly. Never
// forward decoded values into `href`, `src`, or `innerHTML` without
// sanitizing — only safe placement is React-escaped form value props.
function decodePrefill(raw: string | null): Record<string, unknown> {
  if (!raw) return {};
  try {
    const pad = "=".repeat((4 - (raw.length % 4)) % 4);
    const b64 = raw.replace(/-/g, "+").replace(/_/g, "/") + pad;
    const binary = atob(b64);
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    const json = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    const parsed = JSON.parse(json);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function fieldLabel(name: string, schema: WorkflowInputFieldSchema): string {
  if (schema.title) return schema.title;
  return name
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function isMultiline(name: string, schema: WorkflowInputFieldSchema): boolean {
  // Heuristic: any "string" field with min length >= 10 OR a known multi-line
  // semantic name gets a textarea. Single-line strings stay as <input>.
  const multilineNames = new Set([
    "headline_metrics",
    "wins",
    "challenges",
    "deep_dive_topic_1",
    "deep_dive_topic_2",
    "decisions_needed",
    "description",
    "context",
    "notes",
    "summary",
  ]);
  if (multilineNames.has(name)) return true;
  return typeof schema.minLength === "number" && schema.minLength >= 50;
}

export default function JobDetailPage() {
  const params = useParams<{ name: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const name = params?.name;
  const [workflow, setWorkflow] = useState<WorkflowMeta | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // A custom workflow that is switched off isn't runnable (so getWorkflow
  // 404s); it gets its review card instead, where turning it on approves it.
  const [pendingDef, setPendingDef] = useState<DynamicWorkflowDef | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  // A saved custom workflow, for when it runs.
  const [customDef, setCustomDef] = useState<DynamicWorkflowDef | null>(null);
  // Only offer "Fill with an example" when the workflow has one.
  const [hasSample, setHasSample] = useState(false);
  // Arrived straight from creating or changing it (`?saved=1`). Read once and
  // dropped from the URL, so a refresh doesn't say it again.
  const [justSaved] = useState(() => searchParams?.get("saved") === "1");
  const [form, setForm] = useState<FormState>({});
  const [running, setRunning] = useState(false);
  const [prefillBanner, setPrefillBanner] = useState<
    "suggestion" | "sample" | null
  >(null);
  // Ref-guards: apply URL prefill exactly once per page load, regardless
  // of dev StrictMode double-effects or unrelated searchParams identity
  // changes. Without this, the effect re-applies the prefill on every
  // re-render and silently overwrites the user's in-progress edits.
  const initializedForWorkflowRef = useRef<string | null>(null);
  const prefillRaw = searchParams?.get("prefill") ?? null;

  useEffect(() => {
    if (!justSaved || !pathname) return;
    const rest = new URLSearchParams(searchParams?.toString() ?? "");
    rest.delete("saved");
    const qs = rest.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    // Once, on arrival.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!name) return;
    // Only initialize the form once per workflow name. Re-runs that don't
    // change `name` (StrictMode, unrelated searchParams shape changes,
    // router rerenders) leave the user's edits intact.
    if (initializedForWorkflowRef.current === name) return;
    let cancelled = false;
    getWorkflow(name)
      .then((wf) => {
        if (cancelled) return;
        setWorkflow(wf);
        const initial: FormState = {};
        const props = wf.input_schema.properties ?? {};
        for (const [key, schema] of Object.entries(props)) {
          if (!Object.prototype.hasOwnProperty.call(props, key)) continue;
          initial[key] =
            typeof schema.default === "string" ? schema.default : "";
        }
        const prefill = decodePrefill(prefillRaw);
        let appliedAny = false;
        for (const [key, value] of Object.entries(prefill)) {
          if (
            Object.prototype.hasOwnProperty.call(initial, key) &&
            typeof value === "string"
          ) {
            initial[key] = value;
            appliedAny = true;
          }
        }
        if (appliedAny) setPrefillBanner("suggestion");
        setForm(initial);
        initializedForWorkflowRef.current = name;
        if (wf.is_custom) {
          getCustomWorkflow(name)
            .then((d) => !cancelled && setCustomDef(d))
            .catch(() => {});
        }
        if (Object.keys(props).length > 0) {
          getWorkflowSample(name)
            .then(() => !cancelled && setHasSample(true))
            .catch(() => {});
        }
      })
      .catch(async (e) => {
        const message = e instanceof Error ? e.message : String(e);
        const custom = await getCustomWorkflow(name).catch(() => null);
        if (cancelled) return;
        if (custom && !custom.is_active) setPendingDef(custom);
        else setLoadError(message);
      });
    return () => {
      cancelled = true;
    };
    // prefillRaw is intentionally captured at first init only — re-running
    // when it changes would clobber user edits. See ref guard above.
    // reloadKey re-runs the load once a pending workflow is turned on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, reloadKey]);

  const handleLoadSample = useCallback(async () => {
    if (!name) return;
    try {
      const sample = await getWorkflowSample(name);
      const next: FormState = { ...form };
      for (const [key, value] of Object.entries(sample.inputs)) {
        if (typeof value === "string") next[key] = value;
      }
      setForm(next);
      setPrefillBanner("sample");
    } catch (e) {
      // Non-fatal — surface as a console hint, leave the form as-is.
      console.warn("Failed to load sample inputs:", e);
    }
  }, [name, form]);

  const handleInsertExample = useCallback(
    (field: string, schema: WorkflowInputFieldSchema) => {
      const ex = Array.isArray(schema.examples) ? schema.examples[0] : null;
      if (typeof ex === "string") {
        setForm((prev) => ({ ...prev, [field]: ex }));
      }
    },
    []
  );

  const required = useMemo(
    () => new Set(workflow?.input_schema.required ?? []),
    [workflow]
  );

  const allRequiredFilled = useMemo(() => {
    if (!workflow) return false;
    for (const field of required) {
      if (!form[field] || !form[field].trim()) return false;
    }
    return true;
  }, [workflow, required, form]);

  const handleChange = useCallback((field: string, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    // Once the user edits anything, drop the "pre-filled" banner — its
    // message no longer accurately describes what's in the form.
    setPrefillBanner(null);
  }, []);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      setRunning(true);
    },
    []
  );

  const handleCancel = useCallback(() => {
    setRunning(false);
  }, []);

  if (pendingDef) {
    return (
      <PendingWorkflowReview
        definition={pendingDef}
        onActivated={() => {
          setPendingDef(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  if (loadError) {
    return (
      <div className="flex flex-col h-full bg-surface text-fg items-center justify-center">
        <div className="text-sm text-red-400 mb-4">Error: {loadError}</div>
        <Link href="/jobs" className="text-sm text-fg-muted hover:text-fg">
          ← Back to workflows
        </Link>
      </div>
    );
  }

  if (!workflow) {
    return (
      <div className="flex flex-col h-full bg-surface text-fg-muted items-center justify-center text-sm">
        Loading…
      </div>
    );
  }

  const props = workflow.input_schema.properties ?? {};
  const hasInputs = Object.keys(props).length > 0;
  // "Load context" is setup every run does, not something this workflow does.
  const steps = workflow.steps
    .filter((s) => s.id !== "context")
    .map((s) => {
      const custom = customDef?.steps.find((c) => c.id === s.id);
      const detail = !custom
        ? ""
        : custom.kind === "approval_gate"
          ? custom.question
          : custom.kind === "synthesis"
            ? custom.instructions
            : custom.goal;
      return { id: s.id, title: s.title, detail: s.description || detail };
    });
  const editHref = workflow.is_custom
    ? `/jobs/new?edit=${encodeURIComponent(workflow.name)}`
    : null;
  const runButtons = (
    <div className="flex flex-wrap items-center gap-3">
      <Button
        type="submit"
        variant="primary"
        disabled={!allRequiredFilled}
        className="px-7"
      >
        Run it now
      </Button>
      {editHref && (
        <Link href={editHref} className={buttonClass("secondary")}>
          Change it
        </Link>
      )}
      {hasInputs && (
        <span className="text-sm text-fg-muted">All fields with * are required.</span>
      )}
    </div>
  );

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-4 sm:px-6 py-8">
        <div className="max-w-3xl mx-auto space-y-8">
          <div>
            <Link href="/jobs" className="text-sm text-fg-muted hover:text-fg">
              ← Workflows
            </Link>
            {justSaved && (
              <div className="mt-3">
                <p
                  role="status"
                  className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-3 py-1 text-sm font-medium text-emerald-500"
                >
                  <span aria-hidden="true">✓</span> Saved
                </p>
              </div>
            )}
            <h1 className="mt-2 mb-2 text-2xl sm:text-3xl font-bold tracking-tight text-fg">
              {workflow.title}
            </h1>
            <p className="text-[15px] text-fg-muted leading-relaxed">
              {workflow.description}
            </p>
            {(workflow.playbooks?.length ?? 0) > 0 && (
              <p className="mt-2 text-sm text-fg-muted">
                Follows{" "}
                {workflow.playbooks!.length === 1 ? "playbook" : "playbooks"}:{" "}
                {workflow.playbooks!.map((p, i) => (
                  <span key={p}>
                    {i > 0 && ", "}
                    <Link
                      href={`/jobs?tab=playbooks&playbook=${encodeURIComponent(p)}`}
                      className="text-accent hover:underline"
                    >
                      {p}
                    </Link>
                  </span>
                ))}
                {" "}— customize it to change how this workflow writes.
              </p>
            )}
          </div>

          {!running && (steps.length > 0 || customDef) && (
            <section className="rounded-2xl border border-line bg-surface-elevated p-5 shadow-sm sm:p-7">
              {customDef && (
                <div
                  className={`flex items-start justify-between gap-3 ${
                    steps.length > 0 ? "border-b border-line pb-4 mb-4" : ""
                  }`}
                >
                  <div>
                    <p className="text-[15px] font-semibold text-fg">
                      {customDef.cadence
                        ? describeCadence(customDef.cadence)
                        : "Runs when you start it"}
                    </p>
                    <p className="text-sm text-fg-muted">
                      {hasInputs
                        ? "You fill in a few details each time."
                        : "Nothing to fill in."}
                    </p>
                  </div>
                  {editHref && (
                    <Link
                      href={editHref}
                      className="shrink-0 text-sm text-fg-muted hover:text-fg"
                    >
                      Change
                    </Link>
                  )}
                </div>
              )}
              {steps.length > 0 && (
                <>
                  <h2 className="text-lg font-semibold text-fg">What it does</h2>
                  <ol className="mt-3 space-y-2.5">
                    {steps.map((step, i) => (
                      <li key={step.id} className="flex gap-3 text-[15px]">
                        <span className="shrink-0 mt-0.5 flex h-6 w-6 items-center justify-center rounded-full bg-surface-overlay text-xs text-fg-muted">
                          {i + 1}
                        </span>
                        <div className="min-w-0">
                          <p className="text-fg">{step.title}</p>
                          {step.detail && (
                            <p className="text-sm text-fg-muted line-clamp-2 break-words">
                              {step.detail}
                            </p>
                          )}
                        </div>
                      </li>
                    ))}
                  </ol>
                </>
              )}
            </section>
          )}

          {!running && !hasInputs && (
            <form onSubmit={handleSubmit}>{runButtons}</form>
          )}

          {!running && hasInputs && (
            <form
              onSubmit={handleSubmit}
              className="space-y-6 rounded-2xl border border-line bg-surface-elevated p-5 shadow-sm sm:p-7"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-lg font-semibold text-fg">What it needs</h2>
                {/* Fills every field with a realistic sample; each field's
                    own "Use example" fills just that one. */}
                {hasSample && (
                  <button
                    type="button"
                    onClick={handleLoadSample}
                    title="Load a realistic sample to see what good inputs look like. You can edit anything before running."
                    className="min-h-10 rounded-lg px-2 text-[15px] font-semibold text-accent hover:underline underline-offset-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
                  >
                    Fill with an example
                  </button>
                )}
              </div>

              {prefillBanner && (
                <div
                  role="status"
                  aria-live="polite"
                  className="flex items-start justify-between gap-3 rounded-xl border border-accent/30 bg-accent/10 px-4 py-2.5 text-sm text-fg"
                >
                  <span>
                    {prefillBanner === "suggestion"
                      ? "Inputs pre-filled from a suggestion. Review and edit before running."
                      : "Sample inputs loaded. Edit anything before running."}
                  </span>
                  <button
                    type="button"
                    onClick={() => setPrefillBanner(null)}
                    aria-label="Dismiss notice"
                    className="shrink-0 -my-1 h-8 w-8 rounded-lg text-fg-muted hover:text-fg hover:bg-surface-overlay focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
                  >
                    ×
                  </button>
                </div>
              )}

              {Object.entries(props).map(([fieldName, schema]) => {
                const isRequired = required.has(fieldName);
                const multiline = isMultiline(fieldName, schema);
                const inputId = `field-${fieldName}`;
                const hasExample =
                  Array.isArray(schema.examples) &&
                  schema.examples.length > 0 &&
                  typeof schema.examples[0] === "string" &&
                  (schema.examples[0] as string).length > 0;
                return (
                  <div key={fieldName}>
                    <div className="flex items-baseline justify-between gap-3 mb-1.5">
                      <label
                        htmlFor={inputId}
                        className="block text-[15px] font-semibold text-fg"
                      >
                        {fieldLabel(fieldName, schema)}
                        {isRequired && (
                          <span className="text-red-400 ml-0.5">*</span>
                        )}
                      </label>
                      {hasExample && (
                        <button
                          type="button"
                          onClick={() => handleInsertExample(fieldName, schema)}
                          aria-label={`Insert example value for ${fieldLabel(fieldName, schema)}`}
                          className="shrink-0 text-sm text-fg-muted hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 rounded px-1 transition"
                        >
                          Use example
                        </button>
                      )}
                    </div>
                    {schema.description && (
                      <p className="text-sm text-fg-muted mb-2 leading-relaxed">
                        {schema.description}
                      </p>
                    )}
                    {multiline ? (
                      <textarea
                        id={inputId}
                        value={form[fieldName] ?? ""}
                        onChange={(e) => handleChange(fieldName, e.target.value)}
                        rows={4}
                        required={isRequired}
                        placeholder={
                          Array.isArray(schema.examples) && schema.examples.length
                            ? String(schema.examples[0])
                            : ""
                        }
                        className="w-full rounded-xl bg-surface border border-line focus:ring-2 focus:ring-accent/40 focus:outline-none text-[15px] text-fg px-3.5 py-2.5 placeholder:text-fg-subtle leading-relaxed"
                      />
                    ) : (
                      <input
                        id={inputId}
                        type="text"
                        value={form[fieldName] ?? ""}
                        onChange={(e) => handleChange(fieldName, e.target.value)}
                        required={isRequired}
                        placeholder={
                          Array.isArray(schema.examples) && schema.examples.length
                            ? String(schema.examples[0])
                            : ""
                        }
                        className="w-full h-11 rounded-xl bg-surface border border-line focus:ring-2 focus:ring-accent/40 focus:outline-none text-[15px] text-fg px-3.5 placeholder:text-fg-subtle"
                      />
                    )}
                  </div>
                );
              })}

              <div className="pt-1">{runButtons}</div>
            </form>
          )}

          {!running && workflow.is_custom && <ApprovedTargets name={workflow.name} />}

          {running && (
            <WorkflowRunner
              workflow={workflow}
              inputs={form}
              onCancel={handleCancel}
            />
          )}
        </div>
      </main>
    </div>
  );
}
