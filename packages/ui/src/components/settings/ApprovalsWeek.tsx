"use client";

import { useState } from "react";

import type { ApprovalsWaited, ApprovalsWeek as Week } from "@/lib/api";

// Under the level on Delegate → Approvals: what it did in the last 7 days,
// As you and As me together, for the person reading. "See what waited"
// opens the list, each with why it waited and what you did. Nothing shows
// while both counts are 0.

const OUTCOME: Record<ApprovalsWaited["outcome"], string> = {
  approved: "You approved it",
  declined: "You declined it",
  waiting: "Still waiting for you",
  closed: "Settled in your mailbox",
};

function things(n: number): string {
  return n === 1 ? "1 thing" : `${n}`;
}

export function weekLine(week: Week): { head: string; detail: string } | null {
  if (week.went === 0 && week.waited === 0) return null;
  const head = `Last ${week.days} days: ${things(week.went)} went on ${week.went === 1 ? "its" : "their"} own, ${week.waited} waited for you`;
  const parts: string[] = [];
  if (week.waited > 0) {
    parts.push(`Of the ${week.waited}, you approved ${week.approved} and declined ${week.declined}.`);
  }
  if (week.because_count > 0 && week.because) {
    parts.push(`${week.because_count} waited because ${week.because}.`);
  }
  return { head, detail: parts.join(" ") };
}

export default function ApprovalsWeek({ week }: { week: Week }) {
  const [open, setOpen] = useState(false);
  const line = weekLine(week);
  if (!line) return null;
  return (
    <div className="mt-4 rounded-xl border border-line p-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
        <div className="min-w-0">
          <p className="text-[15px] font-medium text-fg">{line.head}</p>
          {line.detail && <p className="mt-0.5 text-sm text-fg-muted leading-snug">{line.detail}</p>}
        </div>
        {week.items.length > 0 && (
          <button
            type="button"
            aria-expanded={open}
            aria-controls="approvals-waited"
            onClick={() => setOpen((v) => !v)}
            className="flex-shrink-0 self-start text-sm font-semibold text-accent hover:underline sm:self-center"
          >
            {open ? "Hide what waited" : "See what waited"}
          </button>
        )}
      </div>
      {open && (
        <ul id="approvals-waited" className="mt-3 flex flex-col divide-y divide-line border-t border-line">
          {week.items.map((item, i) => (
            <li key={`${item.at}-${i}`} className="py-2.5">
              <span className="block text-[15px] text-fg break-words">{item.label}</span>
              <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted">
                {item.side === "as_me" ? "As me" : "As you"} ·{" "}
                {new Date(item.at).toLocaleDateString(undefined, { month: "short", day: "numeric" })} ·{" "}
                {OUTCOME[item.outcome]}
                {item.why ? ` · ${item.why}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
