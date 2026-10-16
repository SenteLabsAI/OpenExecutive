"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import LeadRulesEditor from "@/components/settings/LeadRulesEditor";
import SettingsCard from "@/components/settings/SettingsCard";
import Button from "@/components/ui/Button";
import {
  addMyLeadRule,
  deleteMyLeadRule,
  getDelegation,
  getMyLeadRules,
  setReach,
  setTraining,
  type DelegationSettings,
  type LeadRule,
} from "@/lib/api";
import {
  HANDLE_IT_MODES,
  LEAD_LINE,
  SENDS_ANCHOR,
  WRITES_ANCHOR,
  reachForSend,
  sendOf,
  writeOf,
  type SendStep,
  type WriteStep,
} from "@/lib/reach";

// What it writes and sends as you, from your own mailbox: one setting on the
// server (PUT /delegation/reach) shown as two pickers. WritesCard, on
// Delegate → Act as me, is the steps that write and never send. SendsCard,
// on Delegate → Handle it for me, is the steps that send on their own, with
// In training (delegation/training.py) and Your rules. Handle it for me
// builds on the inbox replies, so SendsCard opens only once WritesCard is at
// Draft replies automatically, and moving WritesCard below that says Handle it for me
// stops too. Moving either down says what stops before it saves.

type Step<T extends string> = { value: T; label: string; line: string; reason?: string | null };

function useReachSave(onSettings: (next: DelegationSettings) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async (work: () => Promise<DelegationSettings>): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      onSettings(await work());
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
      // A refused move up can stop part way, at a lower step: show where it is.
      try {
        const now = await getDelegation();
        if (now) onSettings(now);
      } catch {
        // Keep what's shown; the error above already says it didn't save.
      }
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, save };
}

// Linked from another tab (WRITES_HREF / SENDS_HREF): the card appears after
// the settings load, so the browser's own jump to the anchor has passed it.
function useJumpTo(anchor: string) {
  useEffect(() => {
    if (window.location.hash === `#${anchor}`) {
      document.getElementById(anchor)?.scrollIntoView({ block: "start" });
    }
  }, [anchor]);
}

function StepList<T extends string>({
  name,
  labelledBy,
  steps,
  current,
  busy,
  onPick,
}: {
  name: string;
  labelledBy: string;
  steps: Step<T>[];
  current: T;
  busy: boolean;
  onPick: (step: T) => void;
}) {
  return (
    <div role="radiogroup" aria-labelledby={labelledBy} className="flex flex-col gap-2">
      {steps.map((step) => {
        const checked = step.value === current;
        const reason = step.reason;
        return (
          <label
            key={step.value}
            className={`flex items-start gap-3 rounded-xl border p-3.5 transition-colors ${
              checked ? "border-accent/60 bg-accent/10" : "border-line bg-surface-elevated hover:border-line-strong"
            } ${reason && !checked ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}
          >
            <input
              type="radio"
              name={name}
              value={step.value}
              checked={checked}
              disabled={busy || (Boolean(reason) && !checked)}
              onChange={() => onPick(step.value)}
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
  );
}

function ConfirmDown({
  text,
  busy,
  onConfirm,
  onCancel,
}: {
  text: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="mt-4 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4">
      <p className="text-sm text-fg leading-relaxed">{text}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={onConfirm}
          className="h-9 rounded-lg bg-fg px-3 text-sm font-medium text-fg-inverse disabled:opacity-50"
        >
          {busy ? "Saving…" : "Move down"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          className="h-9 rounded-lg border border-line px-3 text-sm font-medium text-fg"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function Check({
  checked,
  disabled,
  onChange,
  label,
  help,
  className = "",
}: {
  checked: boolean;
  disabled: boolean;
  onChange: (next: boolean) => void;
  label: string;
  help: string;
  className?: string;
}) {
  return (
    <label className={`flex cursor-pointer items-start gap-3 ${className}`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-5 w-5 flex-shrink-0 accent-indigo-500"
      />
      <span>
        <span className="block text-[15px] font-semibold text-fg">{label}</span>
        <span className="mt-0.5 block text-sm text-fg-muted leading-snug">{help}</span>
      </span>
    </label>
  );
}

const WRITE_ORDER: WriteStep[] = ["off", "when_asked", "inbox"];

// Delegate → Act as me: what it writes as you. `inbox` (the inbox watcher's
// status, Check now and Also save in Drafts) shows under Draft replies automatically.
export function WritesCard({
  settings,
  onSettings,
  inbox,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
  inbox?: ReactNode;
}) {
  const { busy, error, save } = useReachSave(onSettings);
  const [confirming, setConfirming] = useState<WriteStep | null>(null);
  useJumpTo(WRITES_ANCHOR);

  const mailbox = settings.gmail?.provider === "microsoft" ? "Outlook" : "Gmail";
  const connected = settings.gmail?.status === "connected";
  const current = writeOf(settings);
  const sending = sendOf(settings) !== "off";
  const needsMailbox = !connected && current === "off" ? "Connect your mailbox first." : null;
  const steps: Step<WriteStep>[] = [
    { value: "off", label: "Off", line: "The Executive only writes as itself." },
    {
      value: "when_asked",
      label: "When I ask",
      line: `Ask in chat, “reply to Dana as me: yes to the 5th”, and the draft waits in your ${mailbox} Drafts.`,
      reason: needsMailbox,
    },
    {
      value: "inbox",
      label: "Draft replies automatically",
      line: "It also writes a first reply to mail that needs you and puts it on Home.",
      reason: needsMailbox,
    },
  ];

  const pick = (to: WriteStep) => {
    if (to === current) return;
    if (WRITE_ORDER.indexOf(to) < WRITE_ORDER.indexOf(current)) {
      setConfirming(to);
      return;
    }
    void save(() => setReach(to));
  };

  const stops = (to: WriteStep): string => {
    const handleIt = sending ? " Handle it for me stops sending too." : "";
    if (to === "when_asked") return `It stops writing replies to your inbox.${handleIt}`;
    return `It stops writing anything as you, and turns Read my calendar off too.${handleIt}`;
  };

  return (
    <div id={WRITES_ANCHOR} className="scroll-mt-24">
      <SettingsCard title="What it writes as you" titleId="writes-label" description="Nothing goes out until you send it.">
        <StepList name="writes" labelledBy="writes-label" steps={steps} current={current} busy={busy} onPick={pick} />
        {confirming && (
          <ConfirmDown
            text={`Move to ${steps.find((s) => s.value === confirming)?.label}? ${stops(confirming)}`}
            busy={busy}
            onConfirm={() => void save(() => setReach(confirming)).then((ok) => ok && setConfirming(null))}
            onCancel={() => setConfirming(null)}
          />
        )}
        {current === "inbox" && inbox && <div className="mt-4">{inbox}</div>}
        {current !== "off" && (
          <Check
            className="mt-5 border-t border-line pt-4"
            checked={Boolean(settings.training?.drafts)}
            disabled={busy}
            onChange={(learn) => void save(() => setTraining({ drafts: learn }))}
            label="Learn from my edits"
            help="When you change a draft before sending, tick Do it like this next time and it learns how you write to that person. Nothing more is sent."
          />
        )}
        {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
      </SettingsCard>
    </div>
  );
}

// Delegate → Handle it for me: what it sends as you on its own.
export function SendsCard({
  settings,
  onSettings,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
}) {
  const { busy, error, save } = useReachSave(onSettings);
  const [confirming, setConfirming] = useState<SendStep | null>(null);
  useJumpTo(SENDS_ANCHOR);

  const connected = settings.gmail?.status === "connected";
  const writesInbox = writeOf(settings) === "inbox";
  const current = sendOf(settings);
  const training = Boolean(settings.training?.replies);
  const followUpsTraining = Boolean(settings.training?.follow_ups);
  const canSend = Boolean(settings.handle_it?.available);
  const noSigning = canSend ? null : "Needs signed sign-ins on this server.";

  const steps: Step<SendStep>[] = [
    { value: "off", label: "Off", line: "Every reply waits for you to send." },
    ...HANDLE_IT_MODES.map((m) => ({
      value: m.mode as SendStep,
      label: m.label,
      line: `${m.replies} Follow-ups: ${m.followUps}`,
      reason: noSigning,
    })),
    ...(settings.handle_it?.lead_available
      ? [
          {
            value: "everything" as SendStep,
            label: "Everything",
            line: LEAD_LINE,
            reason: noSigning ?? (training || followUpsTraining ? "Opens up once it's out of training." : null),
          },
        ]
      : []),
  ];
  const order = steps.map((s) => s.value);

  const pick = (to: SendStep) => {
    if (to === current) return;
    if (order.indexOf(to) < order.indexOf(current)) {
      setConfirming(to);
      return;
    }
    void save(() => setReach(reachForSend(to)));
  };

  const stops = (to: SendStep): string => {
    if (to === "off") return "It stops sending replies and follow-ups on its own. Every reply waits for you.";
    if (current === "everything") return "It goes back to sending only what this level allows.";
    return "It sends less on its own: only what this level allows.";
  };

  return (
    <div id={SENDS_ANCHOR} className="scroll-mt-24">
      <SettingsCard
        title="What it sends on its own"
        titleId="sends-label"
        description="It answers from the replies Act as me writes for your inbox."
      >
        {!writesInbox ? (
          <div className="flex flex-col items-start gap-3">
            <p className="text-sm text-fg-muted leading-relaxed">
              It needs Act as me to write replies for your inbox first.
            </p>
            {connected ? (
              <Button variant="primary" disabled={busy} onClick={() => void save(() => setReach("inbox"))}>
                {busy ? "Turning on…" : "Turn on Draft replies automatically"}
              </Button>
            ) : (
              <Link
                href="/delegate/act-as-me"
                className="inline-flex h-10 items-center rounded-lg border border-line bg-surface px-4 text-sm font-semibold text-accent hover:border-line-strong"
              >
                Connect your mailbox in Act as me
              </Link>
            )}
          </div>
        ) : (
          <>
            <StepList name="sends" labelledBy="sends-label" steps={steps} current={current} busy={busy} onPick={pick} />
            {confirming && (
              <ConfirmDown
                text={`Move to ${steps.find((s) => s.value === confirming)?.label}? ${stops(confirming)}`}
                busy={busy}
                onConfirm={() =>
                  void save(() => setReach(reachForSend(confirming))).then((ok) => ok && setConfirming(null))
                }
                onCancel={() => setConfirming(null)}
              />
            )}
            {/* Shown at Off too, so it can be in training before the first
                move to a step that sends: training stays across moves. */}
            <div className="mt-5 flex flex-col gap-3 border-t border-line pt-4">
              <Check
                checked={training}
                disabled={busy || current === "everything"}
                onChange={(train) => void save(() => setTraining({ replies: train, follow_ups: train }))}
                label="In training"
                help={
                  current === "everything"
                    ? "Move to Most mail or below to train it."
                    : current === "off"
                      ? "Tick this before moving up, and every reply comes to you first until you allow that person."
                      : "Every reply comes to you first. Tap Send + allow and it handles that person on its own from then on, within this step."
                }
              />
              {(training || followUpsTraining) && (
                <Check
                  className="ml-8"
                  checked={followUpsTraining}
                  disabled={busy}
                  onChange={(train) => void save(() => setTraining({ follow_ups: train }))}
                  label="Train follow-ups too"
                  help="Untick to let follow-ups go out at this step while replies stay in training."
                />
              )}
              <p className="text-sm text-fg-muted">
                Money, contracts, legal, hiring, the press and passwords always wait for you, and it never writes to
                anyone the email didn&apos;t go to.
              </p>
            </div>
            {current === "everything" && <YourRules />}
          </>
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
