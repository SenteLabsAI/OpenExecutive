"use client";

import { useEffect, useState, type ReactNode } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import {
  getDelegation,
  setReach,
  setTraining,
  type DelegationSettings,
  type OutsideContentMode,
  type ReachStep,
} from "@/lib/api";
import { REACH_ORDER, reachLine, reachOf } from "@/lib/reach";

// Delegate → As you: how far it goes as you, from your own mailbox. One
// setting on the server (PUT /delegation/reach): Off, When you ask, Your
// inbox. What goes on its own from the inbox is the Approvals level's call,
// held back while In training is on; there is no switch for it. Moving down
// says what stops before it saves.

type Step<T extends string> = { value: T; label: string; line: string; reason?: string | null };

export function useReachSave(onSettings: (next: DelegationSettings) => void) {
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

// Linked from another page (AS_YOU_HREF, AS_ME_HREF): the block appears
// after the settings load, so the browser's own jump to the anchor has
// passed it.
export function useJumpTo(anchor: string) {
  useEffect(() => {
    if (window.location.hash === `#${anchor}`) {
      document.getElementById(anchor)?.scrollIntoView({ block: "start" });
    }
  }, [anchor]);
}

export function StepList<T extends string>({
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

export function ConfirmDown({
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

export function Check({
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

// The As you picker. `inbox` (the inbox watcher's status, Check now and Also
// save in Drafts) shows under Your inbox; `level` is the Approvals level,
// for the "Now:" line.
export function ReachCard({
  settings,
  onSettings,
  inbox,
  level,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
  inbox?: ReactNode;
  level: OutsideContentMode | null;
}) {
  const { busy, error, save } = useReachSave(onSettings);
  const [confirming, setConfirming] = useState<ReachStep | null>(null);

  const mailbox = settings.gmail?.provider === "microsoft" ? "Outlook" : "Gmail";
  const connected = settings.gmail?.status === "connected";
  const current = reachOf(settings);
  const inTraining = Boolean(settings.training?.training);
  const needsMailbox = !connected && current === "off" ? "Connect your mailbox first." : null;
  const steps: Step<ReachStep>[] = [
    { value: "off", label: "Off", line: "It never writes as you." },
    {
      value: "when_asked",
      label: "When you ask",
      line: `Ask in chat, “reply to Dana as me: yes to the 5th”, and the draft waits in your ${mailbox} Drafts. You send it.`,
      reason: needsMailbox,
    },
    {
      value: "inbox",
      label: "Your inbox",
      line: "It also drafts a reply to each email that needs you and puts it on Home. Approvals decides which go on their own.",
      reason: needsMailbox,
    },
  ];

  const pick = (to: ReachStep) => {
    if (to === current) return;
    if (REACH_ORDER.indexOf(to) < REACH_ORDER.indexOf(current)) {
      setConfirming(to);
      return;
    }
    void save(() => setReach(to));
  };

  const stops = (to: ReachStep): string =>
    to === "when_asked"
      ? "It stops drafting replies to your inbox, and nothing more goes on its own as you."
      : "It stops writing anything as you, and turns Read my calendar off too.";

  return (
    <SettingsCard title="How far it goes as you" titleId="reach-label">
      <StepList name="reach" labelledBy="reach-label" steps={steps} current={current} busy={busy} onPick={pick} />
      <p className="mt-3 text-[15px] text-fg">
        <span className="text-fg-muted">Now: </span>
        {reachLine(current, inTraining, level)}
      </p>
      {confirming && (
        <ConfirmDown
          text={`Move to ${steps.find((s) => s.value === confirming)?.label}? ${stops(confirming)}`}
          busy={busy}
          onConfirm={() => void save(() => setReach(confirming)).then((ok) => ok && setConfirming(null))}
          onCancel={() => setConfirming(null)}
        />
      )}
      {current === "inbox" && inbox && <div className="mt-4">{inbox}</div>}
      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}

// In training for As you (PUT /delegation/training {training}): while it is
// on, nothing goes out as you on its own, on any Approvals level, until you
// tick "Send to them on its own next time" on a card. Learn from my edits
// (drafts) sits with it. Shown once it writes as you at all.
export function AsYouTrainingCard({
  settings,
  onSettings,
  children,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
  children?: ReactNode;
}) {
  const { busy, error, save } = useReachSave(onSettings);
  const training = settings.training;
  if (!training) return null;
  const on = Boolean(training.training);
  return (
    <SettingsCard
      title="In training"
      titleId="as-you-training-label"
      description={
        on
          ? "Every reply, follow-up, invite answer and suggested action waits for you, whatever Approvals says. Tick “on its own next time” when you send one and that person goes on their own from then on."
          : "Off: Approvals alone decides what goes on its own as you."
      }
      action={
        <Switch
          checked={on}
          disabled={busy}
          labelledBy="as-you-training-label"
          onChange={(next) => void save(() => setTraining({ training: next }))}
        />
      }
    >
      <Check
        checked={Boolean(training.drafts)}
        disabled={busy}
        onChange={(learn) => void save(() => setTraining({ drafts: learn }))}
        label="Learn from my edits"
        help="When you change a draft before sending, tick Do it like this next time and it learns how you write to that person. Nothing more is sent."
      />
      {children}
      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
