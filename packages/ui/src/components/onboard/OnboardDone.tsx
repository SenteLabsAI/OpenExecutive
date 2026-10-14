"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import ExecEmailRow from "@/components/onboard/ExecEmailRow";
import { buttonClass } from "@/components/ui/Button";
import { getCompanyProfile, listPeople } from "@/lib/api";
import { FIRST_RUN_LINKS, OWN_MAILBOX_SENDS, STORAGE_KEYS, firstQuestion, remember } from "@/lib/firstRun";

interface Props {
  /** Personal (one person) or a team; sets the wording. */
  solo: boolean;
  /** Opens Home. */
  onDone: () => void;
}

// The last setup screen ends on a conversation: one question, built from the
// priorities setup just saved, which Home asks as soon as it opens (handed
// over in session storage, never the URL, so a link can't make someone ask
// something). Under it, what the Executive can already do, written as things
// to try: its own email, the brief, drafting replies as you. The connections
// are also rows on Home's getting-started checklist, so nothing is lost by
// asking first.
export default function OnboardDone({ solo, onDone }: Props) {
  const [companyName, setCompanyName] = useState<string | null>(null);
  const [peopleCount, setPeopleCount] = useState<number | null>(null);
  const [question, setQuestion] = useState<string>(() => firstQuestion(null, solo));
  // Set once they type: a profile that loads late must not overwrite them.
  const edited = useRef(false);

  useEffect(() => {
    let cancelled = false;
    getCompanyProfile()
      .then((p) => {
        if (cancelled) return;
        setCompanyName(p.name?.trim() || null);
        if (!edited.current) setQuestion(firstQuestion(p.strategic_priorities, solo));
      })
      .catch(() => undefined);
    listPeople()
      .then((ps) => {
        if (!cancelled) setPeopleCount(ps.length);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [solo]);

  const knows = [
    companyName,
    !solo && peopleCount !== null && peopleCount > 1 ? `your ${peopleCount} people` : null,
    "this year's priorities",
  ].filter(Boolean) as string[];
  const knowsLine = `It knows ${knows.length > 1 ? `${knows.slice(0, -1).join(", ")} and ${knows[knows.length - 1]}` : knows[0]}.`;

  function ask() {
    const text = question.trim();
    if (!text) return;
    remember(STORAGE_KEYS.firstQuestion, text);
    onDone();
  }

  return (
    <div className="max-w-2xl mx-auto px-4 sm:px-6 py-12 sm:py-16 w-full">
      <div className="text-4xl leading-none text-emerald-400" aria-hidden>
        ✓
      </div>
      <h1 className="mt-3 text-2xl font-bold tracking-tight text-fg">Your Executive is ready</h1>
      <p className="text-sm text-fg-muted mt-1">{knowsLine} Everything stays editable in Settings.</p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          ask();
        }}
        className="mt-6 rounded-2xl border border-accent/30 bg-accent/5 p-4"
      >
        <label htmlFor="first-question" className="text-xs font-semibold uppercase tracking-wider text-accent">
          Start here
        </label>
        <textarea
          id="first-question"
          value={question}
          onChange={(e) => {
            edited.current = true;
            setQuestion(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              ask();
            }
          }}
          rows={2}
          className="mt-2 w-full resize-none rounded-xl border border-line-strong bg-surface-elevated px-3 py-2 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
        />
        <p className="mt-1 text-xs text-fg-muted">
          Built from what you just saved. Change it, or ask anything else.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
          <button type="submit" disabled={!question.trim()} className={buttonClass("primary", "md")}>
            Ask your first question
          </button>
          <button
            type="button"
            onClick={onDone}
            className="text-sm font-semibold text-accent hover:underline cursor-pointer"
          >
            Open Home instead
          </button>
        </div>
      </form>

      <h2 className="mt-8 text-xs font-semibold uppercase tracking-wider text-fg-subtle">It can already</h2>
      <ul className="mt-2 rounded-2xl border border-line bg-surface-elevated px-4">
        <ExecEmailRow />
        <li className="flex items-baseline gap-3 border-b border-line py-3 last:border-0">
          <span aria-hidden className="w-5 flex-shrink-0 text-center">
            ☀
          </span>
          <p className="min-w-0 text-[15px] text-fg-muted leading-snug">
            <span className="font-semibold text-fg">Brief you.</span> Your first brief is on Home now, and a new one
            is written every morning.
          </p>
        </li>
        <li className="flex items-baseline gap-3 border-b border-line py-3 last:border-0">
          <span aria-hidden className="w-5 flex-shrink-0 text-center">
            ✎
          </span>
          <p className="min-w-0 text-[15px] text-fg-muted leading-snug">
            <span className="font-semibold text-fg">Draft replies as you.</span>{" "}
            <Link href={FIRST_RUN_LINKS.ownMailbox} className="font-semibold text-accent hover:underline">
              Let it read your own mailbox
            </Link>
            . {OWN_MAILBOX_SENDS}
          </p>
        </li>
      </ul>
    </div>
  );
}
