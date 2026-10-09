"use client";

import { useEffect, useState } from "react";

import LeadRulesEditor from "@/components/settings/LeadRulesEditor";
import ModePicker from "@/components/settings/ModePicker";
import SettingsCard from "@/components/settings/SettingsCard";
import {
  addMyLeadRule,
  deleteMyLeadRule,
  getDelegation,
  getHandledReplies,
  getMyLeadRules,
  setHandleIt,
  setLeadAsYou,
  setTraining,
  type DelegationSettings,
  type HandledReply,
  type HandleIt,
  type HandleItMode,
  type LeadRule,
} from "@/lib/api";
import { formatAgo } from "@/lib/setupStatus";

// Handle it for me (PUT /delegation/handle-it) on Settings → Act as me:
// replies the inbox watcher sends from your mailbox on its own. Plain code
// decides each one (delegation/handle_it.py). Off / In training / On, as on
// Take the lead: In training (Replies in training, delegation/training.py,
// PUT /delegation/training) every reply waits on Today unless you allowed that
// person with Send + allow, and "Train follow-ups too" does the same for
// follow-ups. Who it handles is the level: Easy ones, People I know, Most mail,
// and for the owner, when it's On, Everything, which uses Take the lead
// (PUT /delegation/take-the-lead), where links, the topics that always wait
// and the added rules hold a reply back. Below, what it sent in the last week
// (GET /delegation/handled).
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

// GET /delegation once for the Act as me page's Training and Handle it
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

// Under Draft replies to my inbox on Settings → Act as me: nothing for
// someone who can't have Act as me; until the inbox watcher is on, the card
// says to turn it on above.
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
    <HandleItSection
      handleIt={settings.handle_it}
      inboxOn={settings.inbox.enabled}
      repliesInTraining={Boolean(settings.training?.replies)}
      followUpsInTraining={Boolean(settings.training?.follow_ups)}
      onSettings={setSettings}
    />
  );
}

type Mode = "off" | "training" | "on";
type Level = HandleItMode | "lead";

const LEAD_LEVEL = {
  label: "Everything",
  replies: "Uses Take the lead. It decides what to send as you. Replies with a link, the topics that always wait and your rules still hold it back.",
};

export function HandleItSection({
  handleIt,
  inboxOn,
  repliesInTraining = false,
  followUpsInTraining = false,
  onSettings,
}: {
  handleIt: HandleIt;
  inboxOn: boolean;
  repliesInTraining?: boolean;
  followUpsInTraining?: boolean;
  onSettings: (next: DelegationSettings) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [handled, setHandled] = useState<HandledReply[] | null>(null);
  const [rules, setRules] = useState<LeadRule[] | null>(null);
  const on = handleIt.enabled;
  const lead = Boolean(on && handleIt.lead);
  const mode: Mode = !on ? "off" : repliesInTraining ? "training" : "on";
  const level: Level = lead ? "lead" : handleIt.mode;
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

  const save = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  // Training first, then the level, so turning it on never sends anything
  // before the training is in place. Putting it in training turns Take the
  // lead as you off on the server.
  const pickMode = (next: Mode) =>
    save(async () => {
      if (next === "off") {
        if (lead) await setLeadAsYou(false);
        onSettings(await setHandleIt({ enabled: false }));
        return;
      }
      const train = next === "training";
      if (train !== repliesInTraining || train !== followUpsInTraining) {
        onSettings(await setTraining({ replies: train, follow_ups: train }));
      }
      if (!on) onSettings(await setHandleIt({ enabled: true, mode: handleIt.mode }));
    });

  // Take the lead as you is its own switch on the server, so leaving
  // Everything turns it off first.
  const pickLevel = (next: Level) =>
    save(async () => {
      if (next === "lead") {
        onSettings(await setLeadAsYou(true));
        return;
      }
      if (lead) await setLeadAsYou(false);
      onSettings(await setHandleIt({ enabled: true, mode: next }));
    });

  const levels: { value: Level; label: string; replies: string; followUps?: string; disabled?: boolean }[] = [
    ...HANDLE_IT_MODES.map((m) => ({ value: m.mode as Level, label: m.label, replies: m.replies, followUps: m.followUps })),
    ...(handleIt.lead_available
      ? [{ value: "lead" as Level, label: LEAD_LEVEL.label, replies: LEAD_LEVEL.replies, disabled: mode === "training" }]
      : []),
  ];
  const picked = levels.find((l) => l.value === level);

  return (
    <SettingsCard
      title="Handle it for me"
      titleId="handle-it-label"
      description={
        !handleIt.available
          ? "Needs signed sign-ins on this server before it can send anything as you."
          : !inboxOn
            ? "Turn on Draft replies to my inbox above first."
            : "Replies and follow-ups it sends as you."
      }
    >
      <div className="flex flex-col gap-4">
        <div>
          <ModePicker<Mode>
            labelledBy="handle-it-label"
            value={mode}
            disabled={busy || (!on && !canTurnOn)}
            options={[
              { value: "off", label: "Off" },
              { value: "training", label: "In training" },
              { value: "on", label: "On" },
            ]}
            onPick={(next) => void pickMode(next)}
          />
          <p className="mt-3 text-sm text-fg-muted leading-relaxed">
            {mode === "training"
              ? "Every reply comes to you on Today first. Tap Send + allow and it handles that person on its own from then on."
              : mode === "on"
                ? "It sends replies and follow-ups on its own, to the people below. Whatever it doesn't send waits for you on Today."
                : "Every reply waits for you to tap Send."}
          </p>
        </div>
        {on && (
          <div className="border-t border-line pt-4">
            <h3 id="handle-it-level-label" className="text-[15px] font-semibold text-fg">
              Who it handles
            </h3>
            <div className="mt-2">
              <ModePicker<Level>
                labelledBy="handle-it-level-label"
                value={level}
                disabled={busy}
                compact
                options={levels.map((l) => ({ value: l.value, label: l.label, disabled: l.disabled }))}
                onPick={(next) => void pickLevel(next)}
              />
            </div>
            {picked && (
              <p className="mt-2 text-sm text-fg-muted leading-snug">
                {picked.followUps ? `${picked.replies} Follow-ups: ${picked.followUps}` : picked.replies}
              </p>
            )}
            {mode === "training" && handleIt.lead_available && (
              <p className="mt-1 text-sm text-fg-muted">Everything opens up once it&apos;s On.</p>
            )}
          </div>
        )}
        {on && (mode === "training" || followUpsInTraining) && (
          <label className="flex cursor-pointer items-start gap-3 border-t border-line pt-4">
            <input
              type="checkbox"
              checked={followUpsInTraining}
              disabled={busy}
              onChange={(e) => {
                const train = e.target.checked;
                void save(async () => onSettings(await setTraining({ follow_ups: train })));
              }}
              className="mt-0.5 h-5 w-5 flex-shrink-0 accent-indigo-500"
            />
            <span>
              <span className="block text-[15px] font-semibold text-fg">Train follow-ups too</span>
              <span className="mt-0.5 block text-sm text-fg-muted leading-snug">
                Untick to let follow-ups go out at this level while replies stay in training.
              </span>
            </span>
          </label>
        )}
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
