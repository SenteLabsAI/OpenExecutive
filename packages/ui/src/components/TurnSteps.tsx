"use client";

import Icon from "./Icon";
import type { PlanRow } from "@/lib/turnSteps";

// A long turn's steps: with a plan (lib/turnSteps.ts), every step up front,
// done ones ticked, the current one running with what it is doing right now
// under it, and the rest still to do; without one, the steps as they happen,
// done ones ticked, the current one running. Under them, Stop ends the turn and keeps what was written; I'll
// finish it has the Executive stop and write up what it has so far, for the
// person to take from there.
export default function TurnSteps({
  steps,
  plan,
  liveLabel,
  elapsed,
  handingOver,
  stopping,
  onStop,
  onHandOver,
}: {
  steps: string[];
  plan?: PlanRow[] | null;
  liveLabel?: string | null;
  elapsed: string | null;
  handingOver: boolean;
  stopping: boolean;
  onStop: () => void;
  onHandOver: () => void;
}) {
  const busy = handingOver || stopping;
  const at = plan ? plan.findIndex((r) => r.state === "current") : -1;
  const doneCount = plan ? plan.filter((r) => r.state === "done").length : 0;
  const stepOf = plan && plan.length > 1
    ? ` · step ${at >= 0 ? at + 1 : Math.min(doneCount + 1, plan.length)} of ${plan.length}`
    : "";
  return (
    <div className="rounded-xl border border-line bg-surface-elevated px-4 py-3 mb-6 max-w-xl">
      <div className="flex items-baseline justify-between gap-3 mb-2">
        <span className="text-sm font-medium text-fg">
          {handingOver ? "Writing up what I have so far" : `Working on it${stepOf}`}
        </span>
        {elapsed && <span className="text-xs text-fg-muted tabular-nums">{elapsed}</span>}
      </div>
      {plan ? (
        <ol className="space-y-1.5" aria-live="polite">
          {plan.map((row, i) => (
            <li
              key={`${i}-${row.label}`}
              className={`flex items-start gap-2 text-sm ${
                row.state === "current" ? "text-fg font-medium" : row.state === "done" ? "text-fg-muted" : "text-fg-subtle"
              }`}
            >
              <span className="mt-0.5 w-4 flex-shrink-0 flex justify-center" aria-hidden="true">
                {row.state === "current" ? (
                  <span className="inline-block w-3 h-3 rounded-full border-2 border-accent border-r-transparent animate-spin" />
                ) : row.state === "done" ? (
                  <Icon name="check" size="w-3.5 h-3.5" className="text-emerald-500" />
                ) : (
                  <span className="mt-0.5 inline-block w-2.5 h-2.5 rounded-full border border-line-strong" />
                )}
              </span>
              <span>
                {row.label}
                {row.added && (
                  <span className="ml-1.5 rounded border border-accent/50 px-1 text-[11px] font-normal text-accent">
                    Added
                  </span>
                )}
                <span className="sr-only">
                  {row.state === "current" ? " (in progress)" : row.state === "done" ? " (done)" : " (to do)"}
                </span>
                {row.state === "current" && liveLabel && (
                  <span className="block text-xs font-normal text-fg-muted">{liveLabel}</span>
                )}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <ol className="space-y-1.5" aria-live="polite">
          {steps.map((step, i) => {
            const current = i === steps.length - 1 && !handingOver;
            return (
              <li
                key={`${i}-${step}`}
                className={`flex items-start gap-2 text-sm ${current ? "text-fg" : "text-fg-muted"}`}
              >
                <span className="mt-0.5 w-4 flex-shrink-0 flex justify-center" aria-hidden="true">
                  {current ? (
                    <span className="inline-block w-3 h-3 rounded-full border-2 border-accent border-r-transparent animate-spin" />
                  ) : (
                    <Icon name="check" size="w-3.5 h-3.5" className="text-emerald-500" />
                  )}
                </span>
                <span>
                  {step}
                  {current && <span className="sr-only"> (in progress)</span>}
                </span>
              </li>
            );
          })}
        </ol>
      )}
      {!handingOver && (
        <div className="flex flex-wrap gap-2 mt-3">
          <button
            type="button"
            onClick={onStop}
            disabled={busy}
            title="Stop: whatever has been written so far is kept"
            className="rounded-lg border border-line-strong px-3 py-1.5 text-sm text-fg hover:border-fg-muted disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
          >
            Stop
          </button>
          <button
            type="button"
            onClick={onHandOver}
            disabled={busy}
            title="I stop here and give you what I have so far, to finish yourself"
            className="rounded-lg bg-accent-strong px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
          >
            I&apos;ll finish it
          </button>
        </div>
      )}
    </div>
  );
}
