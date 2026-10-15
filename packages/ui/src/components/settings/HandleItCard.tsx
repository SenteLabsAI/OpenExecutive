"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import {
  getDelegation,
  getHandledReplies,
  type DelegationSettings,
  type HandledReply,
  type HandleIt,
} from "@/lib/api";
import { REACH_HREF, reachSummary, sentTodayLine } from "@/lib/reach";
import { formatAgo } from "@/lib/setupStatus";

// Handle it for me, its own tab under Delegate: replies the inbox watcher
// sends from your mailbox on its own. Plain code decides each one
// (delegation/handle_it.py). Its levels are the sending steps of How far it
// goes as you, which lives on Act as me only (ReachCard): this tab shows
// where the dial is, with a link there, and what it sent in the last week
// (GET /delegation/handled).
export type DelegationLoad = {
  settings: DelegationSettings | null;
  state: "loading" | "hidden" | "ready" | "error";
  setSettings: (next: DelegationSettings) => void;
};

// GET /delegation once for a Delegate tab's Training and Handle it
// cards, so a change on one shows on the other. "hidden" for someone who
// can't have Act as me (GET /delegation answers null).
export function useDelegation(): DelegationLoad {
  const [settings, setSettings] = useState<DelegationSettings | null>(null);
  const [state, setState] = useState<DelegationLoad["state"]>("loading");

  useEffect(() => {
    const controller = new AbortController();
    getDelegation(controller.signal)
      .then((next) => {
        if (!next || !next.handle_it || !next.inbox) {
          setState("hidden");
          return;
        }
        setSettings(next);
        setState("ready");
      })
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setState("error");
      });
    return () => controller.abort();
  }, []);

  return { settings, state, setSettings };
}

// On Delegate → Handle it for me: nothing for someone who can't have Act
// as me; otherwise where the dial is and what it sent.
export default function HandleItCard({ load }: { load: DelegationLoad }) {
  const { settings, state } = load;
  if (state === "loading") return <p className="text-[15px] text-fg-muted">Loading…</p>;
  if (state === "error") {
    return (
      <SettingsCard>
        <p className="text-sm text-fg-muted">Couldn&apos;t load Handle it for me.</p>
      </SettingsCard>
    );
  }
  if (state === "hidden" || !settings?.handle_it || !settings.inbox) return null;
  return (
    <>
      <ReachNow settings={settings} />
      {settings.handle_it.enabled && <HandleItSection handleIt={settings.handle_it} />}
    </>
  );
}

// Where How far it goes as you is, read-only, with a link to change it on
// Act as me.
function ReachNow({ settings }: { settings: DelegationSettings }) {
  const now = reachSummary(settings);
  return (
    <SettingsCard>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm text-fg-muted">How far it goes as you</p>
          <p className="mt-0.5 flex flex-wrap items-center gap-2 text-base font-semibold text-fg">
            {now.label}
            {now.training && (
              <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300">
                In training
              </span>
            )}
          </p>
          <p className="mt-1 text-sm text-fg-muted leading-snug">{now.line}</p>
        </div>
        <Link
          href={REACH_HREF}
          className="inline-flex h-10 items-center rounded-lg border border-line bg-surface px-4 text-sm font-semibold text-accent hover:border-line-strong"
        >
          {now.sends ? "Change in Act as me" : "Turn it on in Act as me"}
        </Link>
      </div>
    </SettingsCard>
  );
}

// What Handle it for me sent this week.
function HandleItSection({ handleIt }: { handleIt: HandleIt }) {
  const [handled, setHandled] = useState<HandledReply[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getHandledReplies(controller.signal)
      .then(setHandled)
      .catch(() => setHandled(null));
    return () => controller.abort();
  }, [handleIt.sent_today]);

  return (
    <SettingsCard title="What it sent" titleId="handle-it-label" description={sentTodayLine(handleIt.sent_today)}>
      {handled && handled.length > 0 ? (
        <ul className="flex flex-col gap-2" aria-label="Handled for you this week">
          {handled.map((h) => (
            <li key={h.decision_id} className="rounded-md border border-border px-3 py-2 text-sm">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="min-w-0 font-medium">
                  {h.source === "follow_up" ? "Followed up with" : "Replied to"} {h.to_name || h.to_email}:{" "}
                  {h.subject}
                </span>
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
      ) : (
        handled && <p className="text-sm text-fg-muted">Nothing sent in the last week.</p>
      )}
    </SettingsCard>
  );
}
