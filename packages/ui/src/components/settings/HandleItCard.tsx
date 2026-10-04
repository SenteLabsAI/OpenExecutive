"use client";

import { useEffect, useState } from "react";

import LeadRulesEditor from "@/components/settings/LeadRulesEditor";
import SettingsCard from "@/components/settings/SettingsCard";
import {
  addMyLeadRule,
  deleteMyLeadRule,
  getDelegation,
  getHandledReplies,
  getMyLeadRules,
  setHandleIt,
  setLeadAsYou,
  type DelegationSettings,
  type HandledReply,
  type HandleIt,
  type HandleItMode,
  type LeadRule,
} from "@/lib/api";
import { formatAgo } from "@/lib/setupStatus";

// Handle it for me (PUT /delegation/handle-it) on Settings → Act as me:
// replies the inbox watcher sends from your mailbox on its own. Plain code
// decides each one (delegation/handle_it.py). One dial says how much goes
// without you: Off, Careful, Balanced, Bold, and for the owner Take the lead
// (PUT /delegation/take-the-lead), where links, the topics that always wait and
// the added rules hold a reply back. Anything it won't send waits on Today as
// before. Below the dial, what it sent in the last week (GET /delegation/handled).
export const HANDLE_IT_MODES: { mode: HandleItMode; label: string; replies: string; followUps: string }[] = [
  {
    mode: "careful",
    label: "Careful",
    replies: "Only short replies to people you know, when it's very sure.",
    followUps: "Written for you, and they wait on Today for you to send.",
  },
  {
    mode: "balanced",
    label: "Balanced",
    replies: "Replies to people you know. Strangers, links and amounts wait for you.",
    followUps: "Sent to your team and contacts when they haven't answered a question of yours in a few days.",
  },
  {
    mode: "bold",
    label: "Bold",
    replies: "Also strangers and longer replies, and links or amounts when it's very sure.",
    followUps: "Sent to anyone you wrote to who hasn't answered a question of yours in a few days.",
  },
];

// Under the mailbox card on Settings → Act as me: nothing for someone who
// can't have Act as me (GET /delegation answers null); until the inbox
// watcher is on, the card says to turn it on above.
export default function HandleItCard() {
  const [settings, setSettings] = useState<DelegationSettings | null>(null);
  const [state, setState] = useState<"loading" | "hidden" | "ready" | "error">("loading");

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
    <HandleItSection handleIt={settings.handle_it} inboxOn={settings.inbox.enabled} onSettings={setSettings} />
  );
}

type Step = "off" | HandleItMode | "lead";

const OFF_STEP = { label: "Off", text: "Every reply waits for you to tap Send." };
const LEAD_STEP = {
  label: "Take the lead",
  text: "It decides what to send as you. Replies with a link, the topics that always wait and your rules still hold it back.",
};

export function HandleItSection({
  handleIt,
  inboxOn,
  onSettings,
}: {
  handleIt: HandleIt;
  inboxOn: boolean;
  onSettings: (next: DelegationSettings) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [handled, setHandled] = useState<HandledReply[] | null>(null);
  const [rules, setRules] = useState<LeadRule[] | null>(null);
  const on = handleIt.enabled;
  const lead = Boolean(on && handleIt.lead);
  const step: Step = !on ? "off" : lead ? "lead" : handleIt.mode;
  const canTurnOn = inboxOn && handleIt.available;

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

  // One dial: each step includes the one before. Take the lead as you is
  // its own switch on the server, so leaving it turns it off first.
  const pick = async (next: Step) => {
    if (next === step) return;
    setBusy(true);
    setError(null);
    try {
      if (next === "lead") {
        onSettings(await setLeadAsYou(true));
      } else {
        if (lead) await setLeadAsYou(false);
        onSettings(await setHandleIt(next === "off" ? { enabled: false } : { enabled: true, mode: next }));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  const steps: { step: Step; label: string; lines: string[] }[] = [
    { step: "off", label: OFF_STEP.label, lines: [OFF_STEP.text] },
    ...HANDLE_IT_MODES.map((m) => ({
      step: m.mode as Step,
      label: m.label,
      lines: [`Replies: ${m.replies}`, `Follow-ups: ${m.followUps}`],
    })),
    ...(handleIt.lead_available ? [{ step: "lead" as Step, label: LEAD_STEP.label, lines: [LEAD_STEP.text] }] : []),
  ];

  return (
    <SettingsCard
      title="Handle it for me"
      titleId="handle-it-label"
      description={
        !handleIt.available
          ? "Needs signed sign-ins on this server before it can send anything as you."
          : inboxOn
            ? "How much it sends from your mailbox on its own. Whatever it doesn't send waits for you on Today."
            : "Turn on Draft replies to my inbox above first."
      }
    >
      <div className="flex flex-col gap-4">
        <div role="radiogroup" aria-labelledby="handle-it-label" className="flex flex-col gap-2">
          {steps.map((s) => {
            const picked = s.step === step;
            return (
              <button
                key={s.step}
                type="button"
                role="radio"
                aria-checked={picked}
                disabled={busy || (s.step !== "off" && !canTurnOn)}
                onClick={() => void pick(s.step)}
                className={`min-h-touch rounded-xl border px-4 py-3 text-left transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                  picked ? "border-accent bg-accent/5" : "border-line hover:border-line-strong"
                }`}
              >
                <span className="flex items-center gap-2 text-[15px] font-semibold text-fg">
                  <span
                    aria-hidden="true"
                    className={`inline-block h-4 w-4 flex-shrink-0 rounded-full border-2 ${
                      picked ? "border-accent bg-accent" : "border-line-strong"
                    }`}
                  />
                  {s.label}
                </span>
                {s.lines.map((line) => (
                  <span key={line} className="mt-1 block text-sm leading-snug text-fg-muted">
                    {line}
                  </span>
                ))}
              </button>
            );
          })}
        </div>
        {on && (
          <p className="text-sm text-fg-muted">
            Money, contracts, legal, hiring, the press and passwords always wait for you, and it never writes to anyone
            the email didn&apos;t go to.
          </p>
        )}
        {lead && (
          <div>
            <h3 className="text-[15px] font-semibold text-fg">Your rules</h3>
            <p className="mt-1 mb-3 text-sm text-fg-muted">
              Replies matching one of these wait for you, on top of your company&apos;s rules.
            </p>
            <LeadRulesEditor
              rules={rules ?? []}
              emptyText="No rules of your own yet."
              disabled={busy}
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
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
