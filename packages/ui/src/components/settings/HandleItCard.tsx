"use client";

import { useEffect, useState } from "react";

import LeadRulesEditor from "@/components/settings/LeadRulesEditor";
import ReachCard from "@/components/settings/ReachCard";
import SettingsCard from "@/components/settings/SettingsCard";
import {
  addMyLeadRule,
  deleteMyLeadRule,
  getDelegation,
  getHandledReplies,
  getMyLeadRules,
  type DelegationSettings,
  type HandledReply,
  type HandleIt,
  type HandleItMode,
  type LeadRule,
} from "@/lib/api";
import { formatAgo } from "@/lib/setupStatus";

// Handle it for me, its own tab under Delegate: replies the inbox watcher
// sends from your mailbox on its own. Plain code decides each one
// (delegation/handle_it.py). Its levels are the sending steps of How far it
// goes as you (ReachCard, PUT /delegation/reach): Easy ones, People I know,
// Most mail, and for the owner Everything, which uses Take the lead as you,
// where links, the topics that always wait and the added rules hold a reply
// back. In training (Replies and Follow-ups, delegation/training.py) every
// reply waits on Today unless you allowed that person with Send + allow.
// Below the dial, what it sent in the last week (GET /delegation/handled).
export const HANDLE_IT_MODES: { mode: HandleItMode; label: string; replies: string; followUps: string }[] = [
  {
    mode: "careful",
    label: "Easy ones",
    replies: "Only short replies to people you know, when it's very sure.",
    followUps: "Written for you, and they wait on Today for you to send.",
  },
  {
    mode: "balanced",
    label: "People I know",
    replies: "Replies to people you know. Strangers, links and amounts wait for you.",
    followUps: "Sent to your team and contacts when they haven't answered a question of yours in a few days.",
  },
  {
    mode: "bold",
    label: "Most mail",
    replies: "Also strangers and longer replies, and links or amounts when it's very sure.",
    followUps: "Sent to anyone you wrote to who hasn't answered a question of yours in a few days.",
  },
];

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

// On Delegate → Handle it for me: nothing for
// someone who can't have Act as me; until the inbox watcher is on, the card
// says to turn it on, on the Act as me tab.
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
      <ReachCard settings={settings} onSettings={setSettings} />
      <HandleItSection handleIt={settings.handle_it} inboxOn={settings.inbox.enabled} />
    </>
  );
}

// What Handle it for me sent, and, at Everything, your own rules. The dial
// that turns it on and sets its level is How far it goes as you (ReachCard).
export function HandleItSection({ handleIt, inboxOn }: { handleIt: HandleIt; inboxOn: boolean }) {
  const [handled, setHandled] = useState<HandledReply[] | null>(null);
  const [rules, setRules] = useState<LeadRule[] | null>(null);
  const on = handleIt.enabled;
  const lead = Boolean(on && handleIt.lead);

  useEffect(() => {
    if (!on) return;
    const controller = new AbortController();
    getHandledReplies(controller.signal)
      .then(setHandled)
      .catch(() => setHandled(null));
    return () => controller.abort();
  }, [on, handleIt.sent_today]);

  useEffect(() => {
    if (!lead) return;
    const controller = new AbortController();
    getMyLeadRules(controller.signal)
      .then(setRules)
      .catch(() => setRules(null));
    return () => controller.abort();
  }, [lead]);

  return (
    <SettingsCard
      title="What it sent"
      titleId="handle-it-label"
      description={
        !inboxOn
          ? "Move How far it goes as you to a Handle it for me step to have it send replies as you."
          : !on
            ? "Handle it for me is off: every reply waits for you to tap Send."
            : "Replies and follow-ups it sent as you on its own."
      }
    >
      <div className="flex flex-col gap-4">
        {lead && (
          <div>
            <h3 className="text-[15px] font-semibold text-fg">Your rules</h3>
            <p className="mt-1 mb-3 text-sm text-fg-muted">
              Replies matching one of these wait for you, on top of your company&apos;s rules.
            </p>
            <LeadRulesEditor
              rules={rules ?? []}
              emptyText="No rules of your own yet."
              onAdd={async (kind, value) => setRules(await addMyLeadRule(kind, value))}
              onDelete={async (id) => setRules(await deleteMyLeadRule(id))}
            />
          </div>
        )}
        {on && (
          <p className="text-sm text-fg-muted">
            {handleIt.sent_today === 1
              ? "Sent 1 reply on its own today."
              : `Sent ${handleIt.sent_today} replies on its own today.`}
          </p>
        )}
        {on && handled && handled.length > 0 && (
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
        )}
      </div>
    </SettingsCard>
  );
}
