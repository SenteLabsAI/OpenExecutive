"use client";

import { useState } from "react";
import {
  FOUND_SHOWN,
  USED_SHOWN,
  detailsSummary,
  firstFew,
  newestFew,
  readChips,
  type ReadCounts,
  type TurnProgress,
} from "@/lib/turnSteps";

// A long turn while it works (lib/turnSteps.ts): one live line for what it is
// doing right now and how long it has run, and under it "7 sources · 3 found",
// which opens what it has used and found so far. Closed by default, so the
// turn takes two lines however long it runs. Stop is the chat bar's.
export default function TurnSteps({
  progress,
  reads,
  liveLine,
  elapsed,
  handingOver,
}: {
  progress: TurnProgress | null;
  reads: ReadCounts;
  liveLine: string | null;
  elapsed: string | null;
  handingOver: boolean;
}) {
  const [open, setOpen] = useState(false);
  const named = Boolean(progress && progress.used.length > 0);
  const used = named
    ? firstFew(progress?.used ?? [], USED_SHOWN)
    : firstFew(readChips(reads), USED_SHOWN);
  const found = newestFew(progress?.found ?? [], FOUND_SHOWN);
  const summary = detailsSummary(progress, reads);
  const line = handingOver
    ? "Writing up what I have so far"
    : liveLine || progress?.now || progress?.task || "Working on it";
  return (
    <div className="mb-6 max-w-xl">
      <p className="flex items-center gap-2 text-sm text-fg" aria-live="polite">
        <span className="w-3 flex-shrink-0 flex justify-center" aria-hidden="true">
          <span className="inline-block w-3 h-3 rounded-full border-2 border-accent border-r-transparent animate-spin" />
        </span>
        <span className="min-w-0 truncate">{line}</span>
        {elapsed && <span className="flex-shrink-0 text-xs text-fg-muted tabular-nums">· {elapsed}</span>}
      </p>
      {summary && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="ml-5 mt-1 text-xs text-accent hover:underline cursor-pointer"
        >
          {summary} {open ? "▾" : "›"}
        </button>
      )}
      {open && summary && (
        <div className="ml-5 mt-2 border-l-2 border-line pl-3 text-sm space-y-1">
          {used.shown.length > 0 && (
            <p className="text-fg-muted">
              {used.shown.join(", ")}
              {used.more > 0 && ` +${used.more} more`}
            </p>
          )}
          {found.earlier > 0 && <p className="text-xs text-fg-muted">+{found.earlier} earlier</p>}
          {found.shown.map((f, i) => (
            <p key={`${i}-${f}`} className="text-fg">
              {f}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
