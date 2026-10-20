"use client";

import { useEffect, useState } from "react";

import { SendsCard } from "@/components/settings/ReachCard";
import SettingsCard from "@/components/settings/SettingsCard";
import {
  getDelegation,
  getHandledReplies,
  type DelegationSettings,
  type HandledReply,
  type HandleIt,
} from "@/lib/api";
import { handledLine } from "@/lib/inviteCards";
import { sentTodayLine } from "@/lib/reach";
import { formatAgo } from "@/lib/setupStatus";

// Handle it for me, its own tab under Delegate: replies the inbox watcher
// sends from your mailbox on its own. Plain code decides each one
// (delegation/handle_it.py). This tab holds What it sends on its own
// (SendsCard) and what it sent in the last week (GET /delegation/handled),
// meeting invites it accepted on its own included.
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
// as me; otherwise What it sends on its own and what it sent.
export default function HandleItCard({ load }: { load: DelegationLoad }) {
  const { settings, state, setSettings } = load;
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
      <SendsCard settings={settings} onSettings={setSettings} />
      {settings.handle_it.enabled && <HandleItSection handleIt={settings.handle_it} />}
    </>
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
      ) : (
        handled && <p className="text-sm text-fg-muted">Nothing sent in the last week.</p>
      )}
    </SettingsCard>
  );
}
