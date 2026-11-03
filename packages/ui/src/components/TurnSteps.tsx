"use client";

import {
  FOUND_SHOWN,
  USED_SHOWN,
  firstFew,
  newestFew,
  readChips,
  type ReadCounts,
  type TurnProgress,
} from "@/lib/turnSteps";

// A long turn's card (lib/turnSteps.ts): the task, one live line for what it
// is doing right now, what it has used and what it has found so far. It stays
// the same size however long the turn runs: the first few sources show, then
// "+N more"; the newest few findings show, then "+N earlier". A turn that
// names nothing itself shows its activity line and the reads counted from its
// rounds. Under them, Stop ends the turn and keeps what was written; I'll
// finish it has the Executive stop and write up what it has so far, for the
// person to take from there.
export default function TurnSteps({
  progress,
  reads,
  liveLine,
  elapsed,
  handingOver,
  stopping,
  onStop,
  onHandOver,
}: {
  progress: TurnProgress | null;
  reads: ReadCounts;
  liveLine: string | null;
  elapsed: string | null;
  handingOver: boolean;
  stopping: boolean;
  onStop: () => void;
  onHandOver: () => void;
}) {
  const busy = handingOver || stopping;
  const used = progress && progress.used.length > 0
    ? firstFew(progress.used, USED_SHOWN)
    : firstFew(readChips(reads), USED_SHOWN);
  const named = Boolean(progress && progress.used.length > 0);
  const found = newestFew(progress?.found ?? [], FOUND_SHOWN);
  const title = handingOver ? "Writing up what I have so far" : progress?.task || "Working on it";
  return (
    <div className="rounded-xl border border-line bg-surface-elevated px-4 py-3 mb-6 max-w-xl">
      <div className="flex items-baseline justify-between gap-3 mb-2">
        <span className="text-sm font-medium text-fg">{title}</span>
        {elapsed && <span className="text-xs text-fg-muted tabular-nums">{elapsed}</span>}
      </div>
      {!handingOver && liveLine && (
        <p className="flex items-start gap-2 text-sm text-fg" aria-live="polite">
          <span className="mt-1 w-4 flex-shrink-0 flex justify-center" aria-hidden="true">
            <span className="inline-block w-3 h-3 rounded-full border-2 border-accent border-r-transparent animate-spin" />
          </span>
          <span>{liveLine}</span>
        </p>
      )}
      {used.shown.length > 0 && (
        <div className="mt-3">
          <p className="text-[11px] uppercase tracking-wide text-fg-subtle mb-1">Used</p>
          <ul className="flex flex-wrap gap-1.5">
            {used.shown.map((label, i) => (
              <li
                key={`${i}-${label}`}
                className={`max-w-full truncate rounded-full border border-line bg-surface-overlay px-2.5 py-0.5 text-xs ${
                  named && i === 0 ? "text-fg font-medium" : "text-fg-muted"
                }`}
                title={label}
              >
                {label}
              </li>
            ))}
            {used.more > 0 && (
              <li className="rounded-full border border-line px-2.5 py-0.5 text-xs text-fg-muted">
                +{used.more} more
              </li>
            )}
          </ul>
        </div>
      )}
      {found.shown.length > 0 && (
        <div className="mt-3">
          <p className="text-[11px] uppercase tracking-wide text-fg-subtle mb-1">Found so far</p>
          {found.earlier > 0 && (
            <p className="text-xs text-fg-muted mb-1">+{found.earlier} earlier</p>
          )}
          <ul className="space-y-1">
            {found.shown.map((line, i) => (
              <li key={`${i}-${line}`} className="border-l-2 border-accent pl-2.5 text-sm text-fg">
                {line}
              </li>
            ))}
          </ul>
        </div>
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
