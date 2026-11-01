"use client";

import Icon from "./Icon";

// A long turn's steps as they happen: done ones ticked, the current one
// running. Under them, Stop ends the turn and keeps what was written; I'll
// finish it has the Executive stop and write up what it has so far, for the
// person to take from there.
export default function TurnSteps({
  steps,
  elapsed,
  handingOver,
  stopping,
  onStop,
  onHandOver,
}: {
  steps: string[];
  elapsed: string | null;
  handingOver: boolean;
  stopping: boolean;
  onStop: () => void;
  onHandOver: () => void;
}) {
  const busy = handingOver || stopping;
  return (
    <div className="rounded-xl border border-line bg-surface-elevated px-4 py-3 mb-6 max-w-xl">
      <div className="flex items-baseline justify-between gap-3 mb-2">
        <span className="text-sm font-medium text-fg">
          {handingOver ? "Writing up what I have so far" : "Working on it"}
        </span>
        {elapsed && <span className="text-xs text-fg-muted tabular-nums">{elapsed}</span>}
      </div>
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
