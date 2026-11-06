"use client";

import Link from "next/link";

import type { DoneRow } from "@/lib/api";
import { doneLink, statusLabel } from "@/lib/doneForYou";
import { whenLabel } from "@/lib/pulseView";

import { buttonClass } from "../ui/Button";

// Done for you on Home: what the Executive did or is doing for you in the
// last day (GET /today `done_for_you`). A card you approved is one row whose
// chip changes in place from Working on it to Done (or Didn't work, with
// Try again); what it did on its own follows. Nothing here waits on you, so
// none of it is in Needs you. The full list is Memory's activity.

export const DONE_FOR_YOU_ID = "done-for-you";
const SHOWN = 5;

const CHIP: Record<DoneRow["status"], string> = {
  working: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  done: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  failed: "bg-rose-500/10 text-rose-700 dark:text-rose-300",
};

export default function DoneForYou({
  rows,
  onRetry,
  onOpenChat,
}: {
  rows: DoneRow[];
  onRetry?: (row: DoneRow) => void;
  // Opens a chat in place. Home is already at `/`, so a link to
  // `/?session=` there changes the address and nothing else.
  onOpenChat?: (sessionId: string, watch?: boolean) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <section id={DONE_FOR_YOU_ID} aria-labelledby="sec-done-for-you" className="mb-8 scroll-mt-4">
      <h2 id="sec-done-for-you" className="mb-3 text-lg font-semibold text-fg">
        Done for you <span className="font-normal text-fg-muted">(last day)</span>
      </h2>
      <ul className="rounded-2xl border border-line bg-surface-elevated px-4 sm:px-5">
        {rows.slice(0, SHOWN).map((row) => {
          const link = doneLink(row);
          const when = whenLabel(row.at);
          return (
            <li key={row.key} className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line py-3 first:border-t-0">
              <div className="min-w-0 flex-1">
                <p className="text-[15px] text-fg break-words">{row.title}</p>
                {row.detail && <p className="text-sm text-fg-muted break-words">{row.detail}</p>}
              </div>
              {when && <span className="text-xs tabular-nums text-fg-muted">{when}</span>}
              <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${CHIP[row.status]}`}>
                {statusLabel(row.status)}
              </span>
              {row.status === "failed" && row.alert_id != null && onRetry && (
                <button type="button" onClick={() => onRetry(row)} className={buttonClass("secondary", "sm")}>
                  Try again
                </button>
              )}
              {link && row.session_id && onOpenChat ? (
                <button
                  type="button"
                  onClick={() => onOpenChat(row.session_id as string, row.status === "working")}
                  className={buttonClass("ghost", "sm")}
                >
                  {link.label}
                </button>
              ) : link ? (
                <Link href={link.href} className={buttonClass("ghost", "sm")}>
                  {link.label}
                </Link>
              ) : null}
            </li>
          );
        })}
        <li className="border-t border-line py-3">
          <Link href="/memories?tab=activity" className="text-sm font-medium text-accent hover:underline">
            See all in Activity
          </Link>
        </li>
      </ul>
    </section>
  );
}
