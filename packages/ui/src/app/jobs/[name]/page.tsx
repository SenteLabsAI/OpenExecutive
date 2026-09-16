"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import WorkflowRunner from "@/components/WorkflowRunner";
import {
  WorkflowInputFieldSchema,
  WorkflowMeta,
  getWorkflow,
  getWorkflowSample,
} from "@/lib/api";

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
  const name = params?.name;
  const [workflow, setWorkflow] = useState<WorkflowMeta | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
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
      })
      .catch((e) => {
        if (!cancelled) {
          setLoadError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      cancelled = true;
    };
    // prefillRaw is intentionally captured at first init only — re-running
    // when it changes would clobber user edits. See ref guard above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

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

  if (loadError) {
    return (
      <div className="flex flex-col h-full bg-surface text-fg items-center justify-center">
        <div className="text-sm text-red-400 mb-4">Error: {loadError}</div>
        <Link href="/jobs" className="text-sm text-fg-muted hover:text-fg">
          ← Back to jobs
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

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-6 py-8">
        <div className="max-w-3xl mx-auto space-y-8">
          <div>
            <h1 className="text-2xl font-semibold text-fg mb-1">
              {workflow.title}
            </h1>
            <p className="text-sm text-fg-muted leading-relaxed">
              {workflow.description}
            </p>
          </div>

          {!running && (
            <form onSubmit={handleSubmit} className="space-y-5">
              <div className="flex items-center justify-between rounded-md border border-line bg-surface/40 px-3 py-2">
                <div className="text-xs text-fg-muted leading-relaxed">
                  Stuck on how to fill this out? Load a realistic sample to
                  see what good inputs look like. You can edit anything
                  before running.
                </div>
                <button
                  type="button"
                  onClick={handleLoadSample}
                  className="ml-3 shrink-0 text-xs text-indigo-300 hover:text-indigo-200 underline underline-offset-2"
                >
                  Load sample run
                </button>
              </div>

              {prefillBanner && (
                <div
                  role="status"
                  aria-live="polite"
                  className="flex items-start justify-between gap-3 rounded-md border border-indigo-500/30 bg-indigo-500/10 px-3 py-2 text-xs text-indigo-200"
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
                    className="shrink-0 text-indigo-300 hover:text-indigo-100 focus:outline-none focus-visible:ring-1 focus-visible:ring-indigo-300 rounded px-1"
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
                    <div className="flex items-baseline justify-between mb-1">
                      <label
                        htmlFor={inputId}
                        className="block text-sm font-medium text-fg"
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
                          className="text-[11px] text-fg-muted hover:text-indigo-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-indigo-400 rounded px-1 transition"
                        >
                          Insert example
                        </button>
                      )}
                    </div>
                    {schema.description && (
                      <p className="text-xs text-fg-muted mb-2 leading-relaxed">
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
                        className="w-full rounded-md bg-surface/60 border border-line focus:border-indigo-500 focus:outline-none text-sm text-fg px-3 py-2 placeholder:text-fg-subtle font-mono leading-relaxed"
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
                        className="w-full rounded-md bg-surface/60 border border-line focus:border-indigo-500 focus:outline-none text-sm text-fg px-3 py-2 placeholder:text-fg-subtle"
                      />
                    )}
                  </div>
                );
              })}

              <div className="flex items-center gap-3 pt-2">
                <button
                  type="submit"
                  disabled={!allRequiredFilled}
                  className="px-4 py-2 rounded-md bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium transition disabled:bg-surface-overlay disabled:text-fg-muted disabled:cursor-not-allowed"
                >
                  Continue
                </button>
                <span className="text-xs text-fg-muted">
                  All fields with * are required.
                </span>
              </div>
            </form>
          )}

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
