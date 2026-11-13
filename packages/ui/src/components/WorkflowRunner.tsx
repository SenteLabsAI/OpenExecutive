"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Button from "@/components/ui/Button";
import {
  WorkflowEvent,
  WorkflowMeta,
  WorkflowStepDef,
  runWorkflow,
  stopWorkflowRun,
} from "@/lib/api";
import { shownSteps } from "@/lib/workflowRun";

type StepState = "pending" | "running" | "done" | "skipped" | "paused";

interface StepStatus {
  def: WorkflowStepDef;
  state: StepState;
  summary?: string;
}

interface WorkflowRunnerProps {
  workflow: WorkflowMeta;
  inputs: Record<string, unknown>;
}

/**
 * Runs the workflow as soon as it mounts: the Run or Run it now that showed
 * it was the person's go, so there is no second confirm here. Approvals, Your
 * rules and In training still hold a step mid-run, with an Approve card.
 */
export default function WorkflowRunner({ workflow, inputs }: WorkflowRunnerProps) {
  const router = useRouter();
  const [steps, setSteps] = useState<StepStatus[]>(
    shownSteps(workflow.steps).map((s) => ({ def: s, state: "pending" }))
  );
  const [runId, setRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(true);
  const [paused, setPaused] = useState<WorkflowEvent | null>(null);
  const [stopped, setStopped] = useState(false);
  const [stopping, setStopping] = useState(false);

  async function handleStop() {
    if (!runId) return;
    setStopping(true);
    try {
      await stopWorkflowRun(runId);
      setStopped(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't stop the run.");
    } finally {
      setStopping(false);
    }
  }

  async function start() {
    try {
      for await (const evt of runWorkflow(workflow.name, inputs)) {
        applyEvent(evt);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStreaming(false);
    }
  }

  function applyEvent(evt: WorkflowEvent) {
    if (evt.type === "run_created" && evt.run_id) {
      setRunId(evt.run_id);
      return;
    }
    if (evt.type === "step_start" && evt.step_id) {
      setSteps((prev) =>
        prev.map((s) =>
          s.def.id === evt.step_id ? { ...s, state: "running" } : s
        )
      );
      return;
    }
    if (evt.type === "progress" && evt.step_id) {
      // A running action step reports each tool it uses; show the latest.
      setSteps((prev) =>
        prev.map((s) =>
          s.def.id === evt.step_id ? { ...s, summary: evt.summary } : s
        )
      );
      return;
    }
    if (evt.type === "step_done" && evt.step_id) {
      const isSkipped =
        typeof evt.summary === "string" && evt.summary.startsWith("Skipped");
      setSteps((prev) =>
        prev.map((s) =>
          s.def.id === evt.step_id
            ? {
                ...s,
                state: isSkipped ? "skipped" : "done",
                summary: evt.summary,
              }
            : s
        )
      );
      return;
    }
    if (evt.type === "awaiting_human") {
      // The stream ends here — no `done` or `error` follows. Without this the
      // gate step sat spinning on "running" and the panel just went quiet.
      setPaused(evt);
      setSteps((prev) =>
        prev.map((s) => (s.state === "running" ? { ...s, state: "paused" } : s))
      );
      return;
    }
    if (evt.type === "stopped") {
      setStopped(true);
      setSteps((prev) =>
        prev.map((s) => (s.state === "running" ? { ...s, state: "skipped" } : s))
      );
      return;
    }
    if (evt.type === "done" && evt.run_id) {
      // Redirect to the run page to view the artifact
      router.push(`/jobs/runs/${encodeURIComponent(evt.run_id)}`);
      return;
    }
    if (evt.type === "error") {
      setError(evt.message ?? "Workflow failed");
      return;
    }
  }

  // One run per mount, even when React runs effects twice in development.
  const startedRef = useRef(false);
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    void start();
    // Once, on mount: the inputs are fixed for this run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="rounded-2xl border border-line bg-surface-elevated p-5 shadow-sm sm:p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-fg">Progress</h3>
        {streaming && !stopped && (
          <span className="flex items-center gap-3">
            <span className="text-xs text-amber-400 flex items-center gap-1.5">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
              Running
            </span>
            {runId && (
              <Button variant="secondary" onClick={handleStop} disabled={stopping}>
                {stopping ? "Stopping…" : "Stop run"}
              </Button>
            )}
          </span>
        )}
        {stopped && <span className="text-xs text-fg-muted">Stopped</span>}
        {!streaming && !error && !stopped && paused && (
          <span className="text-xs text-amber-400">Paused</span>
        )}
        {!streaming && !error && !paused && !stopped && runId && (
          <span className="text-xs text-emerald-400">Complete</span>
        )}
        {error && <span className="text-xs text-red-400">Failed</span>}
      </div>
      <ol className="space-y-3">
        {steps.map((s, i) => (
          <li key={s.def.id} className="flex gap-3">
            <StepIndicator state={s.state} index={i + 1} />
            <div className="flex-1 min-w-0">
              <div className="flex items-baseline justify-between gap-3">
                <div className="text-[15px] text-fg font-medium">
                  {s.def.title}
                </div>
                <div className="text-[10px] text-fg-muted uppercase tracking-wide">
                  {s.state}
                </div>
              </div>
              <div className="text-sm text-fg-muted mt-0.5">
                {s.def.description}
              </div>
              {s.summary && (
                <div className="text-xs text-fg-muted mt-1.5 italic">
                  {s.summary}
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>
      {paused && (
        <div className="mt-4">
          <PausedNotice event={paused} runId={runId} />
        </div>
      )}
      {stopped && (
        <div className="mt-4 text-sm text-fg-muted rounded-md border border-line p-3">
          Stopped. Nothing more runs. What it already did stays done.
        </div>
      )}
      {error && (
        <div className="mt-4 text-sm text-red-400 bg-red-500/5 border border-red-500/20 rounded-md p-3">
          <div className="font-medium mb-1">Workflow failed</div>
          <div className="text-xs">{error}</div>
        </div>
      )}
    </div>
  );
}

function PausedNotice({
  event,
  runId,
}: {
  event: WorkflowEvent;
  runId: string | null;
}) {
  // Only `sent` and `self` mean the approver actually has the question. Saying
  // "waiting on them" for the others would describe a request nobody received.
  const delivery = event.delivery;
  const reached = delivery === "sent" || delivery === "self";
  return (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-amber-300 space-y-2">
      <div className="font-medium">Paused for sign-off</div>
      {event.question && (
        <div className="text-fg">&ldquo;{event.question}&rdquo;</div>
      )}
      <div className="text-xs space-y-1">
        <div>
          {reached
            ? `Sent to person ${event.person_id} — waiting on their reply.`
            : `Person ${event.person_id} has NOT been asked yet (${delivery ?? "unknown"}).`}
        </div>
        {event.resumable ? (
          <div>The run continues by itself once they answer.</div>
        ) : (
          <div>
            Their answer is recorded, but this run stops at the gate.
          </div>
        )}
      </div>
      {runId && (
        <Link
          href={`/jobs/runs/${encodeURIComponent(runId)}`}
          className="inline-block text-sm font-medium text-accent hover:underline"
        >
          Follow this run →
        </Link>
      )}
    </div>
  );
}

function StepIndicator({ state, index }: { state: StepState; index: number }) {
  if (state === "paused") {
    return (
      <div className="flex-shrink-0 w-6 h-6 rounded-full bg-amber-500/20 text-amber-400 flex items-center justify-center text-[10px] font-bold">
        ⏸
      </div>
    );
  }
  if (state === "done") {
    return (
      <div className="flex-shrink-0 w-6 h-6 rounded-full bg-emerald-500/20 text-emerald-400 flex items-center justify-center text-[10px] font-bold">
        ✓
      </div>
    );
  }
  if (state === "skipped") {
    return (
      <div className="flex-shrink-0 w-6 h-6 rounded-full bg-surface-overlay text-fg-muted flex items-center justify-center text-[10px] font-medium">
        —
      </div>
    );
  }
  if (state === "running") {
    return (
      <div className="flex-shrink-0 w-6 h-6 rounded-full bg-amber-500/20 text-amber-400 flex items-center justify-center text-[10px] font-bold animate-pulse">
        {index}
      </div>
    );
  }
  return (
    <div className="flex-shrink-0 w-6 h-6 rounded-full bg-surface-elevated border border-line text-fg-subtle flex items-center justify-center text-[10px] font-medium">
      {index}
    </div>
  );
}
