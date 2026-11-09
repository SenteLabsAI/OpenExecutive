"use client";

import { useEffect, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import { getHandledReplies, type HandledReply, type HandleIt } from "@/lib/api";
import { handledLine } from "@/lib/inviteCards";
import { sentTodayLine } from "@/lib/reach";
import { formatAgo } from "@/lib/setupStatus";

// Delegate → As you: what went out as you on its own in the last week (GET
// /delegation/handled), meeting invites it accepted on its own included.
// One row, closed: today's count, and the week's list on a click.
export default function SentCard({ handleIt }: { handleIt: HandleIt }) {
  const [handled, setHandled] = useState<HandledReply[] | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getHandledReplies(controller.signal)
      .then(setHandled)
      .catch(() => setHandled(null));
    return () => controller.abort();
  }, [handleIt.sent_today]);

  const toggle = handled && handled.length > 0 && (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={open ? "sent-list" : undefined}
      onClick={() => setOpen((v) => !v)}
      className="text-sm font-medium text-accent hover:underline cursor-pointer"
    >
      {open ? "Hide" : `This week (${handled.length})`}
    </button>
  );

  return (
    <SettingsCard
      title="Sent as you on its own"
      titleId="sent-label"
      description={handled && handled.length === 0 ? `${sentTodayLine(handleIt.sent_today)} Nothing sent in the last week.` : sentTodayLine(handleIt.sent_today)}
      action={toggle || undefined}
    >
      {open && handled && handled.length > 0 && (
        <ul id="sent-list" className="flex flex-col gap-2" aria-label="Sent as you this week">
          {handled.map((h) => (
            <li key={h.decision_id} className="rounded-md border border-border px-3 py-2 text-sm">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                {/* A reply names who it went to; an accepted invite says so. */}
                <span className="min-w-0 font-medium">{handledLine(h)}</span>
                <span className="text-xs text-fg-muted">{formatAgo(h.sent_at)}</span>
              </div>
              {h.open_questions.length > 0 && (
                <p className="mt-1 text-fg-muted">Still yours to answer: {h.open_questions.join(" ")}</p>
              )}
              {h.gmail_link && (
                <a href={h.gmail_link} target="_blank" rel="noreferrer" className="mt-1 inline-block text-accent">
                  Open in your mailbox
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </SettingsCard>
  );
}
