"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import LeadRulesEditor from "@/components/settings/LeadRulesEditor";
import SettingsCard from "@/components/settings/SettingsCard";
import {
  addMyLeadRule,
  deleteMyLeadRule,
  getDelegation,
  getMyLeadRules,
  setReach,
  setTraining,
  type DelegationSettings,
  type LeadRule,
  type ReachStep,
} from "@/lib/api";
import { HANDLE_IT_MODES, LEAD_LINE, REACH_ANCHOR, reachOf, sentTodayLine } from "@/lib/reach";

// How far it goes as you: the one dial for everything Act as me does from
// your own mailbox (PUT /delegation/reach). It replaces three switches that
// used to depend on each other out of sight: Write drafts as me, Draft
// replies to my inbox and Handle it for me with its level. The first steps
// write and never send; the rest are Handle it for me, which sends. Moving
// down says what stops before it saves. In training (Replies and Follow-ups,
// delegation/training.py) applies to the sending steps. It is shown on
// Delegate → Act as me only; Handle it for me shows where it is and what it
// sent, and links back here (REACH_HREF).

type Step = {
  value: ReachStep;
  label: string;
  line: string;
  sends: boolean;
};

function stepsFor(settings: DelegationSettings, mailbox: string): Step[] {
  const sending: Step[] = HANDLE_IT_MODES.map((m) => ({
    value: m.mode,
    label: m.label,
    line: `${m.replies} Follow-ups: ${m.followUps}`,
    sends: true,
  }));
  return [
    { value: "off", label: "Off", line: "The Executive only ever writes as itself.", sends: false },
    {
      value: "when_asked",
      label: "Drafts when I ask",
      line: `Ask in chat, “reply to Dana as me: yes to the 5th”, and the draft waits in your ${mailbox} Drafts.`,
      sends: false,
    },
    {
      value: "inbox",
      label: "Drafts for my inbox",
      line: "It also writes a first reply to mail that needs you and puts it on Today. You tap Send.",
      sends: false,
    },
    ...sending,
    ...(settings.handle_it?.lead_available
      ? [{ value: "everything" as ReachStep, label: "Everything", line: LEAD_LINE, sends: true }]
      : []),
  ];
}

// What moving from `from` down to `to` stops, in a sentence.
function whatStops(steps: Step[], from: ReachStep, to: ReachStep): string {
  const i = steps.findIndex((s) => s.value === from);
  const j = steps.findIndex((s) => s.value === to);
  if (j >= i) return "";
  const target = steps[j];
  if (from === "everything" && target.sends) return "It goes back to sending only what this level allows.";
  if (steps[i].sends && target.sends) return "It sends less on its own: only what this level allows.";
  if (steps[i].sends && to === "inbox") return "It stops sending replies and follow-ups on its own. Every reply waits for you.";
  if (to === "when_asked") return "It stops writing replies to your inbox, and sends nothing on its own.";
  return "It stops writing anything as you, and turns Read my calendar off too.";
}

export default function ReachCard({
  settings,
  onSettings,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<ReachStep | null>(null);

  // Linked from Handle it for me or What it may do (REACH_HREF): the card
  // appears after the settings load, so the browser's own jump to the
  // anchor has already passed it.
  useEffect(() => {
    if (window.location.hash === `#${REACH_ANCHOR}`) {
      document.getElementById(REACH_ANCHOR)?.scrollIntoView({ block: "start" });
    }
  }, []);

  const mailbox = settings.gmail?.provider === "microsoft" ? "Outlook" : "Gmail";
  const steps = stepsFor(settings, mailbox);
  const current = reachOf(settings);
  const currentStep = steps.find((s) => s.value === current);
  const sending = Boolean(currentStep?.sends);
  const training = Boolean(settings.training?.replies);
  const followUpsTraining = Boolean(settings.training?.follow_ups);
  const connected = settings.gmail?.status === "connected";
  const canSend = Boolean(settings.handle_it?.available);

  const save = async (work: () => Promise<DelegationSettings>) => {
    setBusy(true);
    setError(null);
    try {
      onSettings(await work());
      setConfirming(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
      // A refused move up can stop part way, at a lower step: show where it is.
      try {
        const now = await getDelegation();
        if (now) onSettings(now);
      } catch {
        // Keep what's shown; the error above already says it didn't save.
      }
    } finally {
      setBusy(false);
    }
  };

  const pick = (to: ReachStep) => {
    if (to === current) return;
    const i = steps.findIndex((s) => s.value === current);
    const j = steps.findIndex((s) => s.value === to);
    if (j < i) {
      setConfirming(to);
      return;
    }
    void save(() => setReach(to));
  };

  const disabledReason = (step: Step): string | null => {
    if (step.value === "off") return null;
    if (!connected && current === "off") return "Connect your mailbox first.";
    if (step.sends && !canSend) return "Needs signed sign-ins on this server.";
    if (step.value === "everything" && (training || followUpsTraining)) return "Opens up once it's out of training.";
    return null;
  };

  const groups: { title: string; steps: Step[] }[] = [
    { title: "Writes as you, never sends", steps: steps.filter((s) => !s.sends) },
    { title: "Handle it for me sends as you", steps: steps.filter((s) => s.sends) },
  ];

  return (
    <div id={REACH_ANCHOR} className="scroll-mt-24">
    <SettingsCard
      title="How far it goes as you"
      titleId="reach-label"
      description="One setting for everything it writes or sends from your own mailbox. Each step adds to the one above it."
    >
      <div role="radiogroup" aria-labelledby="reach-label" className="flex flex-col gap-5">
        {groups.map((group) => (
          <div key={group.title}>
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-fg-subtle">{group.title}</h3>
            <div className="flex flex-col gap-2">
              {group.steps.map((step) => {
                const checked = step.value === current;
                const reason = disabledReason(step);
                return (
                  <label
                    key={step.value}
                    className={`flex items-start gap-3 rounded-xl border p-3.5 transition-colors ${
                      checked ? "border-accent/60 bg-accent/10" : "border-line bg-surface-elevated hover:border-line-strong"
                    } ${reason && !checked ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}
                  >
                    <input
                      type="radio"
                      name="reach"
                      value={step.value}
                      checked={checked}
                      disabled={busy || (Boolean(reason) && !checked)}
                      onChange={() => pick(step.value)}
                      className="mt-1 h-4 w-4 flex-shrink-0 accent-indigo-500"
                    />
                    <span className="min-w-0">
                      <span className="block text-[15px] font-medium text-fg">{step.label}</span>
                      <span className="mt-0.5 block text-sm text-fg-muted leading-snug">
                        {reason && !checked ? reason : step.line}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {confirming && (
        <div className="mt-4 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4">
          <p className="text-sm text-fg leading-relaxed">
            Move to {steps.find((s) => s.value === confirming)?.label}? {whatStops(steps, current, confirming)}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => void save(() => setReach(confirming))}
              className="h-9 rounded-lg bg-fg px-3 text-sm font-medium text-fg-inverse disabled:opacity-50"
            >
              {busy ? "Saving…" : "Move down"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirming(null)}
              className="h-9 rounded-lg border border-line px-3 text-sm font-medium text-fg"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Shown from Drafts for my inbox up, so it can be in training before
          the first move to a step that sends: training stays across moves. */}
      {(sending || current === "inbox") && (
        <div className="mt-5 flex flex-col gap-3 border-t border-line pt-4">
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              checked={training}
              disabled={busy || current === "everything"}
              onChange={(e) => {
                const train = e.target.checked;
                void save(() => setTraining({ replies: train, follow_ups: train }));
              }}
              className="mt-0.5 h-5 w-5 flex-shrink-0 accent-indigo-500"
            />
            <span>
              <span className="block text-[15px] font-semibold text-fg">In training</span>
              <span className="mt-0.5 block text-sm text-fg-muted leading-snug">
                {current === "everything"
                  ? "Move to Most mail or below to train it."
                  : !sending
                    ? "Tick this before moving to a step that sends, and every reply comes to you first until you allow that person."
                    : "Every reply comes to you first. Tap Send + allow and it handles that person on its own from then on, within this step."}
              </span>
            </span>
          </label>
          {(training || followUpsTraining) && (
            <label className="ml-8 flex cursor-pointer items-start gap-3">
              <input
                type="checkbox"
                checked={followUpsTraining}
                disabled={busy}
                onChange={(e) => {
                  const train = e.target.checked;
                  void save(() => setTraining({ follow_ups: train }));
                }}
                className="mt-0.5 h-5 w-5 flex-shrink-0 accent-indigo-500"
              />
              <span>
                <span className="block text-[15px] font-semibold text-fg">Train follow-ups too</span>
                <span className="mt-0.5 block text-sm text-fg-muted leading-snug">
                  Untick to let follow-ups go out at this step while replies stay in training.
                </span>
              </span>
            </label>
          )}
          <p className="text-sm text-fg-muted">
            Money, contracts, legal, hiring, the press and passwords always wait for you, and it never writes to anyone
            the email didn&apos;t go to.
          </p>
        </div>
      )}
      {current !== "off" && (
        <label className="mt-5 flex cursor-pointer items-start gap-3 border-t border-line pt-4">
          <input
            type="checkbox"
            checked={Boolean(settings.training?.drafts)}
            disabled={busy}
            onChange={(e) => {
              const learn = e.target.checked;
              void save(() => setTraining({ drafts: learn }));
            }}
            className="mt-0.5 h-5 w-5 flex-shrink-0 accent-indigo-500"
          />
          <span>
            <span className="block text-[15px] font-semibold text-fg">Learn from my edits</span>
            <span className="mt-0.5 block text-sm text-fg-muted leading-snug">
              When you change a draft before sending, tick Do it like this next time and it learns how you write to
              that person. Nothing more is sent.
            </span>
          </span>
        </label>
      )}
      {current === "everything" && <YourRules />}
      {sending && (
        <p className="mt-5 border-t border-line pt-4 text-sm text-fg-muted">
          {sentTodayLine(settings.handle_it?.sent_today ?? 0)}{" "}
          <Link href="/delegate/handle-it" className="font-medium text-accent underline-offset-2 hover:underline">
            What it sent
          </Link>
        </p>
      )}
      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
    </SettingsCard>
    </div>
  );
}

// At Everything, your own rules: a reply matching one waits for you, on top
// of your company's rules (GET/POST/DELETE /delegation/lead-rules).
function YourRules() {
  const [rules, setRules] = useState<LeadRule[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getMyLeadRules(controller.signal)
      .then(setRules)
      .catch(() => setRules(null));
    return () => controller.abort();
  }, []);

  return (
    <div className="mt-5 border-t border-line pt-4">
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
  );
}
